"""The raw API (`/zsh/raw/`): request decoding and reply building, then round
trips through real zsh workers in a child process."""

import textwrap

import pytest
from starlette.datastructures import Headers, QueryParams

from brish import CmdResult
from brishgarden.reply import (
    BINARY_HEADER,
    CMD_LENGTH_HEADER,
    NOTICE_HEADER,
    OUT_LENGTH_HEADER,
    RETCODE_HEADER,
    STDIN_LOG_LIMIT,
    ZshOutcome,
    raw_notice_reply,
    raw_reply,
    raw_reply_build,
    raw_request_parse,
)
from tests.conftest import binary_only, legacy_only, run_py

MODES = [True, False]
ALL_BYTES = bytes(range(256))


def parse(cmd, stdin=b"", query=None, binary_mode=True, **headers):
    hdrs = {CMD_LENGTH_HEADER: str(len(cmd))}
    hdrs.update({k.replace("_", "-"): v for k, v in headers.items()})
    hdrs = {k: v for k, v in hdrs.items() if v is not None}
    return raw_request_parse(cmd + stdin, hdrs, query or {}, binary_mode=binary_mode)


def test_parse_splits_the_body():
    req = parse(b"cat \xff", ALL_BYTES)
    assert req.error is None
    assert (req.cmd, req.stdin) == (b"cat \xff", ALL_BYTES)
    assert req.cmd_display == "cat \\xff"
    assert len(req.stdin_display.encode()) >= STDIN_LOG_LIMIT
    assert req.raw and req.binary_reply and not req.binary_requested
    req.stdin_display.encode("utf-8")


def test_parse_defaults():
    req = parse(b"true")
    assert (req.session, req.merge, req.nolog, req.log_level, req.failure_expected) == (
        "", False, False, 1, False,
    )


def test_parse_empty_body():
    req = parse(b"")
    assert req.error is None and (req.cmd, req.stdin, req.cmd_display) == (b"", b"", "")


def test_parse_starlette_mappings():
    #: The garden passes Starlette's own mappings; header names are
    #: case-insensitive.
    headers = Headers({"x-brish-cmd-length": "3", "X-BRISH-STDIN": "NULL"})
    query = QueryParams("session=s1&merge=1&nolog=y&log_level=2&failure_expected=true")
    req = raw_request_parse(b"cat", headers, query, binary_mode=True)
    assert req.error is None, req.error
    assert (req.cmd, req.stdin) == (b"cat", None)
    assert (req.session, req.merge, req.nolog, req.log_level, req.failure_expected) == (
        "s1", True, True, 2, True,
    )


@pytest.mark.parametrize("value, want", [("0", False), ("", False), ("no", False), ("1", True), ("y", True)])
def test_parse_bool_options(value, want):
    req = parse(b"x", query={"merge": value, "nolog": value, "failure_expected": value})
    assert (req.merge, req.nolog, req.failure_expected) == (want, want, want)


def test_parse_null_stdin():
    req = parse(b"cat", x_brish_stdin="null")
    assert req.error is None and req.stdin is None and req.stdin_display == ""
    #: Legacy mode has no /dev/null stdin; it runs with empty stdin instead.
    req = parse(b"cat", x_brish_stdin="null", binary_mode=False)
    assert req.error is None and (req.cmd, req.stdin) == ("cat", "")


@pytest.mark.parametrize("binary_mode", MODES)
@pytest.mark.parametrize(
    "body, headers, query, needle",
    [
        (b"cat", {}, {}, "needs the header X-Brish-Cmd-Length"),
        (b"cat", {"x-brish-cmd-length": "abc"}, {}, "decimal"),
        (b"cat", {"x-brish-cmd-length": "-1"}, {}, "decimal"),
        (b"cat", {"x-brish-cmd-length": "1.0"}, {}, "decimal"),
        (b"cat", {"x-brish-cmd-length": ""}, {}, "decimal"),
        (b"cat", {"x-brish-cmd-length": "\u0663"}, {}, "decimal"),
        (b"cat", {"x-brish-cmd-length": "4"}, {}, "has only 3 bytes"),
        (b"", {"x-brish-cmd-length": "1"}, {}, "has only 0 bytes"),
        (b"catx", {"x-brish-cmd-length": "3", "x-brish-stdin": "null"}, {}, "1 bytes of stdin"),
        (b"cat", {"x-brish-cmd-length": "3", "x-brish-stdin": "empty"}, {}, "only takes the value null"),
        (b"cat", {"x-brish-cmd-length": "3"}, {"log_level": "high"}, "log_level must be an integer"),
    ],
)
def test_parse_malformed(binary_mode, body, headers, query, needle):
    req = raw_request_parse(body, headers, query, binary_mode=binary_mode)
    assert req.error is not None and needle in req.error, req
    assert (req.cmd, req.stdin) == ("", "")


def test_parse_malformed_keeps_failure_expected():
    req = parse(b"x", query={"log_level": "?", "failure_expected": "1"})
    assert req.error is not None and req.failure_expected


def test_parse_legacy_mode_decodes_text():
    req = parse("print -r -- café".encode(), b"a\0b", binary_mode=False)
    assert req.error is None
    #: A NUL reaches brish, which refuses it in legacy mode.
    assert (req.cmd, req.stdin) == ("print -r -- café", "a\0b")
    assert not req.binary_reply


@pytest.mark.parametrize("cmd, stdin, what", [(b"\xff", b"", "the command"), (b"cat", b"\xff", "stdin")])
def test_parse_legacy_mode_refuses_invalid_utf8(cmd, stdin, what):
    req = parse(cmd, stdin, binary_mode=False)
    assert req.error is not None and req.error.startswith(f"brishgarden: {what} is not valid utf-8")


def headers_of(reply):
    return {k: reply.headers.get(k) for k in (RETCODE_HEADER, OUT_LENGTH_HEADER, BINARY_HEADER, NOTICE_HEADER)}


@pytest.mark.parametrize("binary", MODES)
def test_reply_build(binary):
    res = CmdResult.from_bytes(3, ALL_BYTES + b"\r\n", b"\0err\xff", b"cmd", b"")
    reply = raw_reply_build(res, binary)
    assert reply.status_code == 200
    assert reply.body == ALL_BYTES + b"\r\n" + b"\0err\xff"
    assert reply.headers["content-type"] == "application/octet-stream"
    assert headers_of(reply) == {
        RETCODE_HEADER: "3",
        OUT_LENGTH_HEADER: str(len(ALL_BYTES) + 2),
        BINARY_HEADER: "1" if binary else "0",
        NOTICE_HEADER: None,
    }
    assert reply.headers["content-length"] == str(len(reply.body))


def test_reply_build_text_result():
    #: A legacy-mode result, and the garden's own error results, are text.
    res = CmdResult(9000, "", "Traceback ...\nboom\n", "cmd", "")
    reply = raw_reply_build(res, False)
    assert reply.body == b"Traceback ...\nboom\n"
    assert headers_of(reply)[RETCODE_HEADER] == "9000"
    assert headers_of(reply)[OUT_LENGTH_HEADER] == "0"
    res = CmdResult(0, "café \udcff", "", "cmd", "")
    assert raw_reply_build(res, False).body == "café ".encode() + b"\xff"


def test_notice_reply():
    reply = raw_notice_reply("Empty command received.", True)
    assert reply.body == b"Empty command received."
    assert headers_of(reply) == {
        RETCODE_HEADER: "0",
        OUT_LENGTH_HEADER: str(len(b"Empty command received.")),
        BINARY_HEADER: "1",
        NOTICE_HEADER: "1",
    }
    assert raw_notice_reply("x \udcff", False).body == b"x \\udcff"


def test_raw_reply_dispatch():
    res = CmdResult.from_bytes(0, b"o", b"e", b"cmd", b"")
    assert raw_reply(ZshOutcome(res=res), True).body == b"oe"
    reply = raw_reply(ZshOutcome(notice="n"), False)
    assert reply.body == b"n" and reply.headers[NOTICE_HEADER] == "1"


###
#: Round trips through real workers.

#: The garden's raw route minus the pool, sessions, magic and logging.
HANDLE = r'''
from brishgarden.reply import ZshOutcome, brish_run, raw_reply, raw_request_parse

def handle(b, cmd, stdin=b"", query=None, **headers):
    hdrs = {"x-brish-cmd-length": str(len(cmd))}
    hdrs.update({k.replace("_", "-"): v for k, v in headers.items()})
    req = raw_request_parse(cmd + stdin, hdrs, query or {}, binary_mode=b.binary, encoding=b.encoding)
    if req.error is not None:
        outcome = ZshOutcome(res=CmdResult(9000, "", req.error, req.cmd_display, req.stdin_display))
    elif req.cmd_display == "":
        outcome = ZshOutcome(notice="Empty command received.")
    else:
        outcome = ZshOutcome(res=brish_run(b, req.cmd, req.stdin, merge=req.merge, server_index=0))
    reply = raw_reply(outcome, req.binary_reply)
    h = reply.headers
    n = int(h["x-brish-out-length"])
    return int(h["x-brish-retcode"]), reply.body[:n], reply.body[n:], h
'''


def run_handle(snippet, **kw):
    return run_py(HANDLE + textwrap.dedent(snippet), **kw)


def test_round_trip_in_garden_mode():
    #: Text that both modes carry, through the garden's own mode.
    run_handle(
        r"""
        b = garden_brish(server_count=1)
        try:
            flag = "1" if BINARY else "0"
            rc, out, err, h = handle(b, b"cat; print -rn -- err >&2; return 3", "line 1\ncafé\n".encode())
            assert (rc, out, err) == (3, "line 1\ncafé\n".encode(), b"err"), (rc, out, err)
            assert h["x-brish-binary"] == flag and "x-brish-notice" not in h

            rc, out, err, h = handle(b, b"print -r -- out; print -r -- err >&2; print -r -- out2",
                                     query={"merge": "1"})
            assert (rc, out, err) == (0, b"out\nerr\nout2\n", b""), (rc, out, err)

            rc, out, err, h = handle(b, b"")
            assert (rc, out, err, h["x-brish-notice"]) == (0, b"Empty command received.", b"", "1")

            rc, out, err, h = handle(b, b"cat", b"")
            assert (rc, out, err) == (0, b"", b"")

            big = b"print -rn -- " + b"x" * (100 << 10)
            rc, out, err, h = handle(b, big)
            assert (rc, out) == (0, b"x" * (100 << 10)), (rc, len(out), err)

            rc, out, err, h = handle(b, b"cat", b"tail", x_brish_cmd_length="9")
            assert rc == 9000 and out == b"" and b"has only 7 bytes" in err, err
        finally:
            b.cleanup()
        """
    )


@binary_only
def test_round_trip_exact_bytes():
    run_handle(
        r"""
        b = garden_brish(server_count=1)
        payloads = [bytes(range(256)), b"\0", b"a\n\0", b"a\r\0", b"\r", b"\r\n",
                    b"\n\0\n0\n", b"x\n\n\n", b"", os.urandom(1 << 20)]
        try:
            for data in payloads:
                rc, out, err, h = handle(b, b"cat", data)
                assert (rc, h["x-brish-binary"]) == (0, "1")
                assert out == data and err == b"", (len(out), len(data))
                rc, out, err, h = handle(b, b"cat >&2", data)
                assert out == b"" and err == data
                rc, out, err, h = handle(b, b"cat", data, query={"merge": "1"})
                assert out == data and err == b""

            #: Raw bytes in the command itself.
            rc, out, err, h = handle(b, b"print -rn -- $'\\0'\xff\xfe")
            assert out == b"\0\xff\xfe", out

            #: X-Brish-Stdin: null is /dev/null, a character device, not a pipe.
            probe = b"[[ -c /dev/stdin ]] && print -rn dev || print -rn other"
            assert handle(b, probe, x_brish_stdin="null")[1] == b"dev"
            assert handle(b, probe)[1] == b"other"
        finally:
            b.cleanup()
        """,
        timeout=120,
    )


@legacy_only
def test_round_trip_legacy_mode():
    run_handle(
        r"""
        b = garden_brish(server_count=1)
        sentinel = os.path.join(os.getcwd(), "sentinel")
        try:
            assert not b.binary
            #: brish refuses a NUL in legacy mode, before running anything.
            rc, out, err, h = handle(b, b"print -rn -- ran >> sentinel; cat", b"a\0b")
            assert (rc, h["x-brish-binary"]) == (9000, "0"), (rc, err)
            rc, out, err, h = handle(b, b"print -rn -- ran >> sentinel # \0")
            assert rc == 9000, (rc, err)
            #: Non-UTF-8 is refused by the garden, since legacy mode carries text.
            rc, out, err, h = handle(b, b"print -rn -- ran >> sentinel # \xff")
            assert rc == 9000 and b"not valid utf-8" in err, err
            assert not os.path.exists(sentinel)

            rc, out, err, h = handle(b, b"cat", x_brish_stdin="null")
            assert (rc, out, err) == (0, b"", b"")
        finally:
            b.cleanup()
        """
    )
