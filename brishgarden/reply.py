"""The `/zsh/` endpoint's request decoding, command run and reply building.

`garden.py` starts its Brish workers when it is imported, so the logic that
decides what a request means and what its reply looks like lives here, where
it can be imported (and tested) without side effects.

A request goes through three steps. Decoding turns it into a `ZshRequest`;
the garden's `zsh_handle` logs it and runs it, giving a `ZshOutcome`; and a
reply builder turns that outcome into the endpoint's reply. There are three
APIs, which share the middle step:
- the JSON API (`/zsh/`): `request_parse` and `json_reply`;
- the raw API (`/zsh/raw/`), which carries bytes as bytes in both
  directions: `raw_request_parse` and `raw_reply`;
- the streaming API (`/zsh/stream/`), which takes the raw API's request
  (`raw_request_parse` with `stream=True`) and sends the output while the
  command runs, in frames: see `brishgarden/stream.py`.

Binary transport is opt-in per request (see the readme):
- `cmd_b64` and `stdin_b64` carry the command and its stdin as base64 of raw
  bytes, and take precedence over `cmd` and `stdin`;
- `binary: 1` asks for exact output: the plain reply path answers with the
  raw bytes, and the JSON reply path adds `out_b64` and `err_b64`; with
  `b64_only: 1` too, they replace the text fields `out` and `err`.

Every reply to a `binary: 1` request carries the header `X-Brish-Binary: 1`,
but only when the garden's Brish runs in binary mode (the default, see
`brishgarden/mode.py`). A garden in legacy mode (`BRISH_BINARY=0`, or a brish
too old for binary mode) refuses such a request without running it, so a
client that does not see the header knows that nothing ran.
"""

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Any, Optional

from brish import CmdResult, bool_from_str
from fastapi import Response
from fastapi.responses import JSONResponse

BINARY_HEADER = "X-Brish-Binary"

#: The retcode of a request the garden could not run, as for an exception
#: raised while running it.
RETCODE_GARDEN_ERROR = 9000

#: How to leave legacy mode, for the refusals below.
LEGACY_FIX = (
    "To run in binary mode (the default), restart the garden process without"
    " BRISH_BINARY=0, and upgrade brish if it is too old for binary mode."
)

LEGACY_REFUSAL = (
    "brishgarden: this garden's Brish runs in legacy (text) mode, so it cannot"
    f" serve a binary request. {LEGACY_FIX} Nothing was run.\n"
)

_BYTES_LIKE = (bytes, bytearray, memoryview)
_SURROGATE = re.compile("[\ud800-\udfff]")
#: Whitespace is ignored in base64 fields, so line-wrapped encoder output works.
_B64_SPACE = re.compile(r"[ \t\r\n]+")

#: Characters of stdin that the access log shows.
STDIN_LOG_LIMIT = 100

#: The raw API's headers. Starlette sends header names lowercased; they are
#: case-insensitive.
CMD_LENGTH_HEADER = "X-Brish-Cmd-Length"
STDIN_HEADER = "X-Brish-Stdin"
RETCODE_HEADER = "X-Brish-Retcode"
OUT_LENGTH_HEADER = "X-Brish-Out-Length"
NOTICE_HEADER = "X-Brish-Notice"
#: Set on a reply for a request that ran nothing (a malformed request, or
#: input that legacy mode cannot carry), so a client can retry it elsewhere
#: without running it twice. A command that ran never gets it, whatever its
#: retcode and stderr.
REFUSED_HEADER = "X-Brish-Refused"
#: ASCII digits only (`\d` would also match other scripts' digits).
_DECIMAL = re.compile("[0-9]+")


class RequestError(ValueError):
    """A request that cannot be run as sent. Nothing has been run."""


def text_safe(s):
    """`s` with any lone surrogate escaped as text (`\\udcff`).

    JSON replies and `text/plain` bodies are encoded as strict UTF-8, which
    refuses lone surrogates. Surrogate-free text is returned as is.
    """
    if _SURROGATE.search(s) is None:
        return s
    return s.encode("utf-8", "backslashreplace").decode("utf-8")


def text_display(x, encoding="utf-8", limit=None):
    """A surrogate-free str of a command or stdin, for matching, logs and
    the JSON `cmd` echo. Bytes are decoded with `backslashreplace`."""
    if isinstance(x, _BYTES_LIKE):
        b = bytes(x if limit is None else x[:limit])
        return b.decode(encoding, "backslashreplace")
    if limit is not None:
        x = x[:limit]
    return text_safe(x)


@dataclass(frozen=True)
class ZshRequest:
    """A decoded `/zsh/` request.

    `cmd` and `stdin` are what `send_cmd` gets: bytes when they came from the
    `_b64` fields and the garden runs in binary mode, else str. The display
    fields are always surrogate-free str. When `error` is set, the request
    must not be run; `error` is the message to reply with.

    The remaining fields are the request's options, as the client sent them;
    the garden combines `nolog` and `log_level` with its own settings.
    """

    cmd: Any
    stdin: Any
    cmd_display: str
    stdin_display: str
    #: The client sent `binary: 1`.
    binary_requested: bool
    #: The reply is a binary reply: requested, and the garden runs in binary mode.
    binary_reply: bool
    #: The client sent `b64_only: 1`: a binary JSON reply leaves out the
    #: text fields `out` and `err`.
    b64_only: bool = False
    error: Optional[str] = None
    session: Any = ""
    #: The shape of a JSON API reply: `0` is the plain reply path.
    json_output: int = 0
    #: Merge stderr into stdout in the shell (the plain reply path).
    merge: bool = True
    nolog: bool = False
    log_level: int = 1
    failure_expected: bool = False
    #: The request came through the raw API, or the streaming API, which
    #: takes the raw API's request.
    raw: bool = False
    #: The request came through the streaming API.
    stream: bool = False


@dataclass(frozen=True)
class ZshOutcome:
    """What running a `ZshRequest` gave: a command's result (or a refusal,
    with retcode 9000), or else a notice that is not a command's output (an
    empty command, a magic command's log)."""

    res: Optional[CmdResult] = None
    notice: Optional[str] = None
    #: The garden refused the request before running anything.
    refused: bool = False


def _payload(body, name):
    """The value of field `name`, taken from `name_b64` (as bytes) when present."""
    b64_name = name + "_b64"
    value = body.get(b64_name)
    if value is not None:
        if not isinstance(value, str):
            raise RequestError(f"brishgarden: {b64_name} must be a base64 string\n")
        try:
            return base64.b64decode(_B64_SPACE.sub("", value), validate=True)
        except (binascii.Error, ValueError) as e:
            raise RequestError(
                f"brishgarden: {b64_name} is not valid base64 ({e})\n"
            ) from None

    value = body.get(name, "")
    if not isinstance(value, str):
        raise RequestError(f"brishgarden: {name} must be a string\n")
    return value


def _legacy_text(value, what, encoding):
    """Legacy mode carries text only: decode a bytes payload strictly.
    `what` names the payload in the error message."""
    if not isinstance(value, bytes):
        return value
    try:
        return value.decode(encoding)
    except UnicodeDecodeError as e:
        raise RequestError(
            f"brishgarden: {what} is not valid {encoding} ({e}); this garden's"
            " Brish runs in legacy (text) mode, which carries text only."
            f" {LEGACY_FIX}\n"
        ) from None


def request_parse(body, *, binary_mode, encoding="utf-8"):
    """Decode a `/zsh/` request body into a `ZshRequest`.

    `binary_mode` is whether the garden's Brish runs in binary mode. The
    decoding never raises for a malformed payload; it sets `error` instead.
    It raises only where the endpoint always failed: a `json_output` (or
    `verbose`) or `log_level` that `int()` refuses. The endpoint then answers
    `null`, as it always has.
    """
    #: The old API named json_output 'verbose'.
    json_output = int(body.get("json_output", body.get("verbose", 0)))
    log_level = int(body.get("log_level", 1))

    binary_requested = bool_from_str(body.get("binary", ""))
    binary_reply = binary_requested and bool(binary_mode)

    cmd = stdin = ""
    cmd_display = stdin_display = ""
    error = None
    try:
        cmd = _payload(body, "cmd")
        cmd_display = text_display(cmd, encoding)
        stdin = _payload(body, "stdin")
        stdin_display = text_display(stdin, encoding, limit=STDIN_LOG_LIMIT)

        if not binary_mode:
            if binary_requested:
                raise RequestError(LEGACY_REFUSAL)
            cmd = _legacy_text(cmd, "cmd_b64", encoding)
            stdin = _legacy_text(stdin, "stdin_b64", encoding)
    except RequestError as e:
        error = str(e)
        cmd = stdin = ""

    return ZshRequest(
        cmd=cmd,
        stdin=stdin,
        cmd_display=cmd_display,
        stdin_display=stdin_display,
        binary_requested=binary_requested,
        binary_reply=binary_reply,
        b64_only=bool_from_str(body.get("b64_only", "")),
        error=error,
        session=body.get("session", ""),
        json_output=json_output,
        merge=json_output == 0,
        nolog=bool(body.get("nolog", "")),
        log_level=log_level,
        failure_expected=bool(body.get("failure_expected", False)),
    )


#: The raw API's query parameters.
RAW_OPTIONS = ("session", "failure_expected", "nolog", "merge", "log_level")


def _single_values(mapping, names, what, *, lower=False):
    """The values of `names` in `mapping`, as a dict of one value per name.

    A name sent more than once with different values is ambiguous: it is
    left out of the dict, and the second element of the returned pair is
    the RequestError to raise (else None). Repeats of one value are fine.
    `mapping` may list repeats: Starlette's QueryParams through
    `multi_items()`, its Headers through `items()`. With `lower`, names are
    matched case-insensitively (`names` are then lowercase).
    """
    multi = getattr(mapping, "multi_items", None)
    values = {}
    for k, v in multi() if multi is not None else mapping.items():
        if lower:
            k = k.lower()
        if k in names:
            values.setdefault(k, []).append(v)
    single, conflicts = {}, []
    for k, vs in values.items():
        if len(set(vs)) > 1:
            conflicts.append(f"{k} ({', '.join(repr(v) for v in vs)})")
        else:
            single[k] = vs[0]
    error = None
    if conflicts:
        error = RequestError(
            f"brishgarden: the {what} {'; '.join(conflicts)} must be sent once,"
            " or with one value\n"
        )
    return single, error


def _raw_options(query, opts):
    """Fill the dict `opts` with the raw API's options (`ZshRequest` fields)
    from its query parameters. Raises RequestError for a bad `log_level` or
    a repeated option with different values, after setting the others, so a
    refusal still honors failure_expected (unless that is the ambiguous one)."""
    query, error = _single_values(query, RAW_OPTIONS, "query parameter")
    opts["session"] = query.get("session", "")
    opts["failure_expected"] = bool_from_str(query.get("failure_expected", ""))
    opts["nolog"] = bool_from_str(query.get("nolog", ""))
    opts["merge"] = bool_from_str(query.get("merge", ""))
    if error is not None:
        raise error
    log_level = query.get("log_level")
    if log_level is not None:
        try:
            opts["log_level"] = int(log_level)
        except ValueError:
            raise RequestError(
                f"brishgarden: log_level must be an integer, not {log_level!r}\n"
            ) from None


def _raw_payloads(body, headers):
    """Split a raw request body into (cmd, stdin) bytes; stdin is None for
    `X-Brish-Stdin: null`. Raises RequestError for a malformed request,
    including one that repeats either header with different values: which
    one is meant is ambiguous, and a wrong length would run a wrong command."""
    headers, error = _single_values(
        headers, (CMD_LENGTH_HEADER.lower(), STDIN_HEADER.lower()), "header", lower=True
    )
    if error is not None:
        raise error
    length = headers.get(CMD_LENGTH_HEADER.lower())
    if length is None:
        raise RequestError(
            f"brishgarden: a raw request needs the header {CMD_LENGTH_HEADER}:"
            " the command's length in bytes\n"
        )
    length = length.strip()
    if _DECIMAL.fullmatch(length) is None:
        raise RequestError(
            f"brishgarden: {CMD_LENGTH_HEADER} must be a decimal number of bytes,"
            f" not {length!r}\n"
        )
    n = int(length)
    if n > len(body):
        raise RequestError(
            f"brishgarden: {CMD_LENGTH_HEADER} is {n}, but the body has only"
            f" {len(body)} bytes\n"
        )
    cmd, stdin = body[:n], body[n:]

    stdin_mode = headers.get(STDIN_HEADER.lower())
    if stdin_mode is not None:
        if stdin_mode.strip().lower() != "null":
            raise RequestError(
                f"brishgarden: {STDIN_HEADER} only takes the value null, not"
                f" {stdin_mode!r}\n"
            )
        if stdin:
            raise RequestError(
                f"brishgarden: {STDIN_HEADER}: null means /dev/null, but the body"
                f" has {len(stdin)} bytes of stdin after the command\n"
            )
        stdin = None
    return cmd, stdin


def raw_request_parse(body, headers, query, *, binary_mode, encoding="utf-8", stream=False):
    """Decode a raw API (`/zsh/raw/`) request into a `ZshRequest`.

    `body` is the command's bytes followed by its stdin's, split at the
    `X-Brish-Cmd-Length` header. `headers` and `query` are mappings, such
    as Starlette's `request.headers` and `request.query_params` (header
    names are case-insensitive; a header or option repeated with different
    values is refused). In binary mode the command and stdin
    stay bytes, and `X-Brish-Stdin: null` makes stdin /dev/null. In legacy
    mode they are decoded strictly as `encoding`, and null stdin is empty.

    The streaming API (`/zsh/stream/`) takes the same request, decoded here
    with `stream=True`, which only marks it as streamed.

    Never raises for a malformed request; it sets `error` instead.
    """
    body = bytes(body)
    opts = {}
    cmd = stdin = ""
    cmd_display = stdin_display = ""
    error = None
    try:
        _raw_options(query, opts)
        cmd, stdin = _raw_payloads(body, headers)
        cmd_display = text_display(cmd, encoding)
        if stdin is not None:
            stdin_display = text_display(stdin, encoding, limit=STDIN_LOG_LIMIT)

        if not binary_mode:
            cmd = _legacy_text(cmd, "the command", encoding)
            stdin = "" if stdin is None else _legacy_text(stdin, "stdin", encoding)
            #: Brish would refuse a NUL too, but with a 9000 that looks like
            #: a command's own; refusing here marks the reply as refused.
            for value, what in ((cmd, "the command"), (stdin, "stdin")):
                if "\0" in value:
                    raise RequestError(
                        f"brishgarden: {what} contains a NUL byte; this garden's"
                        " Brish runs in legacy (text) mode, which cannot carry one."
                        f" {LEGACY_FIX}\n"
                    )
    except RequestError as e:
        error = str(e)
        cmd = stdin = ""

    return ZshRequest(
        cmd=cmd,
        stdin=stdin,
        cmd_display=cmd_display,
        stdin_display=stdin_display,
        binary_requested=False,
        binary_reply=bool(binary_mode),
        error=error,
        raw=True,
        stream=bool(stream),
        **opts,
    )


#: The command that runs a request's command with stderr merged into stdout.
MERGE_TEMPLATE = "{{ eval {cmd} }} 2>&1"


def brish_cmd(brish, cmd, merge):
    """The command text that `brish` runs for a request's command `cmd`.

    With `merge` (the plain reply path, which has a single body, and the
    `merge` option of the raw and streaming APIs), `cmd` runs inside an
    `eval` whose stderr is merged into stdout in the shell. In binary mode a
    bytes `cmd` is quoted byte-exactly into that wrapper.
    """
    if merge:
        return brish.zstring(MERGE_TEMPLATE, locals_={"cmd": cmd})
    return cmd


def brish_run(brish, cmd, stdin, *, merge, server_index):
    """Run a request's command on worker `server_index` of `brish`, and
    return its CmdResult. `merge`: see `brish_cmd`."""
    return brish.send_cmd(
        brish_cmd(brish, cmd, merge),
        fork=False,
        cmd_stdin=stdin,
        server_index=server_index,
    )


def _binary_headers(binary):
    return {BINARY_HEADER: "1"} if binary else None


def notice_reply(text, binary=False):
    """A `text/plain` notice that is not a command's output (an empty command,
    a magic command's log)."""
    return Response(
        content=text_safe(text),
        media_type="text/plain",
        headers=_binary_headers(binary),
    )


def reply_build(
    res,
    json_output,
    binary=False,
    *,
    b64_only=False,
    cmd="",
    session="",
    brishes=0,
    all_brishes=0,
):
    """The reply to a request whose result is the CmdResult `res`.

    - `json_output == 0` (the plain reply path): the body is the output. With
      `binary`, it is `res.outb + res.errb` as `application/octet-stream`;
      otherwise the text `res.outerr` as `text/plain`.
    - Otherwise (the JSON reply path): a dict with the command echo, the pool
      sizes, `out`, `err` and `retcode`. With `binary`, also `out_b64` and
      `err_b64`, the base64 of the exact bytes. With `binary` and
      `b64_only`, these replace `out` and `err`, which only duplicate them
      (and, for binary data, are larger). Dropping them is opt-in, because
      clients such as `brishz_para.dash` read `out` and `err` even when the
      request asked for `binary: 1`.

    A binary reply carries the `X-Brish-Binary: 1` header; the caller passes
    `binary` only when the garden's Brish runs in binary mode. A non-binary
    reply is exactly what the garden returned before binary transport existed,
    except that a lone surrogate, which used to make the reply fail to encode,
    is now escaped.
    """
    if json_output == 0:
        if binary:
            return Response(
                content=res.outb + res.errb,
                media_type="application/octet-stream",
                headers=_binary_headers(True),
            )
        return Response(content=text_safe(res.outerr), media_type="text/plain")

    reply = {
        "cmd": text_display(cmd),
        "session": text_safe(session) if isinstance(session, str) else session,
        "brishes": brishes,
        "allBrishes": all_brishes,
    }
    if not (binary and b64_only):
        reply["out"] = text_safe(res.out)
        reply["err"] = text_safe(res.err)
    reply["retcode"] = res.retcode
    if not binary:
        #: FastAPI serializes a returned dict, as it always has.
        return reply

    reply["out_b64"] = base64.b64encode(res.outb).decode("ascii")
    reply["err_b64"] = base64.b64encode(res.errb).decode("ascii")
    return JSONResponse(content=reply, headers=_binary_headers(True))


def json_reply(outcome, req, *, brishes=0, all_brishes=0):
    """The JSON API's reply to `req`, whose run gave the ZshOutcome `outcome`."""
    if outcome.notice is not None:
        return notice_reply(outcome.notice, req.binary_reply)
    return reply_build(
        outcome.res,
        req.json_output,
        req.binary_reply,
        b64_only=req.b64_only,
        cmd=req.cmd_display,
        session=req.session,
        brishes=brishes,
        all_brishes=all_brishes,
    )


def _raw_headers(retcode, out_length, binary, notice=False, refused=False):
    headers = {
        RETCODE_HEADER: str(retcode),
        OUT_LENGTH_HEADER: str(out_length),
        BINARY_HEADER: "1" if binary else "0",
    }
    if notice:
        headers[NOTICE_HEADER] = "1"
    if refused:
        headers[REFUSED_HEADER] = "1"
    return headers


def raw_reply_build(res, binary, refused=False):
    """The raw API's reply to a request whose result is the CmdResult `res`:
    stdout's bytes then stderr's, with the retcode and stdout's length in
    headers. `binary` is whether the garden runs in binary mode, that is,
    whether the bytes are exact. `refused` marks a request that ran nothing
    (`X-Brish-Refused: 1`)."""
    outb, errb = bytes(res.outb), bytes(res.errb)
    return Response(
        content=outb + errb,
        media_type="application/octet-stream",
        headers=_raw_headers(res.retcode, len(outb), binary, refused=refused),
    )


def raw_notice_reply(text, binary):
    """The raw API's reply for a notice that is not a command's output: the
    text as the stdout part, retcode 0, and `X-Brish-Notice: 1`."""
    data = text_safe(text).encode("utf-8")
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers=_raw_headers(0, len(data), binary, notice=True),
    )


def raw_reply(outcome, binary):
    """The raw API's reply for the ZshOutcome `outcome`."""
    if outcome.notice is not None:
        return raw_notice_reply(outcome.notice, binary)
    return raw_reply_build(outcome.res, binary, refused=outcome.refused)
