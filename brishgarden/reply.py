"""The `/zsh/` endpoint's request decoding, command run and reply building.

`garden.py` starts its Brish workers when it is imported, so the logic that
decides what a request means and what its reply looks like lives here, where
it can be imported (and tested) without side effects.

Binary transport is opt-in per request (see the readme):
- `cmd_b64` and `stdin_b64` carry the command and its stdin as base64 of raw
  bytes, and take precedence over `cmd` and `stdin`;
- `binary: 1` asks for exact output: the plain reply path answers with the
  raw bytes, and the JSON reply path adds `out_b64` and `err_b64`.

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

from brish import bool_from_str
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
    """

    cmd: Any
    stdin: Any
    cmd_display: str
    stdin_display: str
    #: The client sent `binary: 1`.
    binary_requested: bool
    #: The reply is a binary reply: requested, and the garden runs in binary mode.
    binary_reply: bool
    error: Optional[str] = None


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


def _legacy_text(value, name, encoding):
    """Legacy mode carries text only: decode a bytes payload strictly."""
    if not isinstance(value, bytes):
        return value
    try:
        return value.decode(encoding)
    except UnicodeDecodeError as e:
        raise RequestError(
            f"brishgarden: {name}_b64 is not valid {encoding} ({e}); this garden's"
            " Brish runs in legacy (text) mode, which carries text only."
            f" {LEGACY_FIX}\n"
        ) from None


def request_parse(body, *, binary_mode, encoding="utf-8"):
    """Decode a `/zsh/` request body into a `ZshRequest`.

    `binary_mode` is whether the garden's Brish runs in binary mode. The
    decoding never raises for a malformed payload; it sets `error` instead.
    """
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
            cmd = _legacy_text(cmd, "cmd", encoding)
            stdin = _legacy_text(stdin, "stdin", encoding)
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
        error=error,
    )


def brish_run(brish, cmd, stdin, *, json_output, server_index):
    """Run a request's command on worker `server_index` of `brish`.

    The plain reply path (`json_output == 0`) has a single body, so it merges
    stderr into stdout in the shell. In binary mode a bytes `cmd` is quoted
    byte-exactly into the `eval` wrapper.
    """
    if json_output == 0:
        return brish.z(
            "{{ eval {cmd} }} 2>&1",
            locals_={"cmd": cmd},
            fork=False,
            cmd_stdin=stdin,
            server_index=server_index,
        )
    return brish.send_cmd(cmd, fork=False, cmd_stdin=stdin, server_index=server_index)


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
    res, json_output, binary=False, *, cmd="", session="", brishes=0, all_brishes=0
):
    """The reply to a request whose result is the CmdResult `res`.

    - `json_output == 0` (the plain reply path): the body is the output. With
      `binary`, it is `res.outb + res.errb` as `application/octet-stream`;
      otherwise the text `res.outerr` as `text/plain`.
    - Otherwise (the JSON reply path): a dict with the command echo, the pool
      sizes, `out`, `err` and `retcode`; with `binary`, also `out_b64` and
      `err_b64`, the base64 of the exact bytes.

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
        "out": text_safe(res.out),
        "err": text_safe(res.err),
        "retcode": res.retcode,
    }
    if not binary:
        #: FastAPI serializes a returned dict, as it always has.
        return reply

    reply["out_b64"] = base64.b64encode(res.outb).decode("ascii")
    reply["err_b64"] = base64.b64encode(res.errb).decode("ascii")
    return JSONResponse(content=reply, headers=_binary_headers(True))
