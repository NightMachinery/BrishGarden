"""The raw API (`/zsh/raw/`): request decoding and reply building, then round
trips through real zsh workers in a child process."""

import textwrap

import pytest
from starlette.datastructures import Headers, QueryParams

from brish import CmdResult
from brishgarden.reply import (
    BINARY_HEADER,
    CMD_LENGTH_HEADER,
    LEGACY_REFUSAL,
    NOTICE_HEADER,
    OUT_LENGTH_HEADER,
    REFUSED_HEADER,
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


@pytest.mark.parametrize("value, want", [("1", True), ("y", True), ("", False), ("0", False), ("n", False), (None, False)])
def test_parse_binary_option(value, want):
    query = {} if value is None else {"binary": value}
    req = parse(b"print -r -- hi", b"in", query=query, binary_mode=True)
    assert req.error is None and req.binary_requested is want and req.binary_reply
    assert (req.cmd, req.stdin) == (b"print -r -- hi", b"in")
    req = parse(b"print -r -- hi", b"in", query=query, binary_mode=False)
    assert req.binary_requested is want and not req.binary_reply
    if want:
        #: Legacy mode cannot give exact bytes, so it refuses the request, as
        #: it refuses `binary: 1` on the JSON API. The log still shows it.
        assert req.error == LEGACY_REFUSAL, req.error
        assert (req.cmd, req.stdin) == ("", "")
        assert (req.cmd_display, req.stdin_display) == ("print -r -- hi", "in")
    else:
        assert req.error is None and (req.cmd, req.stdin) == ("print -r -- hi", "in")


def test_parse_binary_refusal_keeps_failure_expected():
    req = parse(b"x", query={"binary": "1", "failure_expected": "1"}, binary_mode=False)
    assert req.error == LEGACY_REFUSAL and req.failure_expected


def test_parse_malformed_keeps_failure_expected():
    req = parse(b"x", query={"log_level": "?", "failure_expected": "1"})
    assert req.error is not None and req.failure_expected


def raw_headers(*pairs):
    """Starlette Headers that keep repeats, as a real request's do."""
    return Headers(raw=[(k.encode(), v.encode()) for k, v in pairs])


@pytest.mark.parametrize("binary_mode", MODES)
@pytest.mark.parametrize(
    "pairs, needle",
    [
        #: Either reading would run something: `print -r -- hi`, or `print`
        #: with the rest of the command as its stdin.
        ((("X-Brish-Cmd-Length", "5"), ("X-Brish-Cmd-Length", "14")), "x-brish-cmd-length ('5', '14')"),
        ((("X-Brish-Cmd-Length", "14"), ("x-brish-cmd-length", "5")), "x-brish-cmd-length ('14', '5')"),
        ((("X-Brish-Cmd-Length", "14"), ("X-Brish-Cmd-Length", "014")), "x-brish-cmd-length"),
        ((("X-Brish-Cmd-Length", "14"), ("X-Brish-Stdin", "null"), ("X-Brish-Stdin", "NULL")), "x-brish-stdin"),
    ],
)
def test_parse_refuses_conflicting_repeated_headers(binary_mode, pairs, needle):
    req = raw_request_parse(b"print -r -- hi", raw_headers(*pairs), {}, binary_mode=binary_mode)
    assert req.error is not None and needle in req.error, req.error
    assert "must be sent once" in req.error
    assert (req.cmd, req.stdin) == ("", "")


def test_parse_accepts_repeats_of_one_value():
    pairs = [("X-Brish-Cmd-Length", "3")] * 2 + [("X-Brish-Stdin", "null")] * 2
    req = raw_request_parse(b"cat", raw_headers(*pairs), QueryParams("merge=1&merge=1"), binary_mode=True)
    assert req.error is None, req.error
    assert (req.cmd, req.stdin, req.merge) == (b"cat", None, True)
    #: Other headers may repeat freely.
    pairs = [("X-Brish-Cmd-Length", "3"), ("Accept", "a"), ("Accept", "b")]
    assert raw_request_parse(b"cat", raw_headers(*pairs), {}, binary_mode=True).error is None


@pytest.mark.parametrize("name", ["session", "merge", "nolog", "log_level", "failure_expected", "binary"])
def test_parse_refuses_conflicting_repeated_options(name):
    query = QueryParams(f"{name}=1&{name}=0&failure_expected=1" if name != "failure_expected" else "failure_expected=1&failure_expected=0")
    req = raw_request_parse(b"cat", {CMD_LENGTH_HEADER: "3"}, query, binary_mode=True)
    assert req.error is not None and f"query parameter {name} ('1', '0')" in req.error, req.error
    assert (req.cmd, req.stdin) == ("", "")
    #: The refusal still honors an unambiguous failure_expected.
    assert req.failure_expected == (name != "failure_expected")
    #: Unknown parameters are ignored, repeated or not.
    query = QueryParams("other=1&other=2")
    assert raw_request_parse(b"cat", {CMD_LENGTH_HEADER: "3"}, query, binary_mode=True).error is None


def test_parse_legacy_mode_decodes_text():
    req = parse("print -r -- café".encode(), "a\x01b".encode(), binary_mode=False)
    assert req.error is None
    assert (req.cmd, req.stdin) == ("print -r -- café", "a\x01b")
    assert not req.binary_reply


@pytest.mark.parametrize("cmd, stdin, what", [(b"cat # \0", b"", "the command"), (b"cat", b"a\0b", "stdin")])
def test_parse_legacy_mode_refuses_nul(cmd, stdin, what):
    #: The garden refuses it itself, so that the reply is marked as refused;
    #: Brish would refuse it too, with a 9000 that looks like a command's own.
    req = parse(cmd, stdin, binary_mode=False)
    assert req.error is not None and req.error.startswith(f"brishgarden: {what} contains a NUL byte")
    assert parse(cmd, stdin, binary_mode=True).error is None


@pytest.mark.parametrize("cmd, stdin, what", [(b"\xff", b"", "the command"), (b"cat", b"\xff", "stdin")])
def test_parse_legacy_mode_refuses_invalid_utf8(cmd, stdin, what):
    req = parse(cmd, stdin, binary_mode=False)
    assert req.error is not None and req.error.startswith(f"brishgarden: {what} is not valid utf-8")


def headers_of(reply):
    return {k: reply.headers.get(k) for k in (RETCODE_HEADER, OUT_LENGTH_HEADER, BINARY_HEADER, NOTICE_HEADER, REFUSED_HEADER)}


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
        REFUSED_HEADER: None,
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
        REFUSED_HEADER: None,
    }
    assert raw_notice_reply("x \udcff", False).body == b"x \\udcff"


def test_raw_reply_dispatch():
    res = CmdResult.from_bytes(0, b"o", b"e", b"cmd", b"")
    assert raw_reply(ZshOutcome(res=res), True).body == b"oe"
    reply = raw_reply(ZshOutcome(notice="n"), False)
    assert reply.body == b"n" and reply.headers[NOTICE_HEADER] == "1"
    assert REFUSED_HEADER.lower() not in reply.headers
    refusal = CmdResult(9000, "", "brishgarden: no\n", "", "")
    reply = raw_reply(ZshOutcome(res=refusal, refused=True), True)
    assert headers_of(reply)[REFUSED_HEADER] == "1" and headers_of(reply)[RETCODE_HEADER] == "9000"
    assert headers_of(raw_reply(ZshOutcome(res=refusal), True))[REFUSED_HEADER] is None


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
        outcome = ZshOutcome(res=CmdResult(9000, "", req.error, req.cmd_display, req.stdin_display), refused=True)
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
            assert h["x-brish-refused"] == "1"

            #: A command that ran is never marked refused, even when it looks
            #: like a refusal.
            rc, out, err, h = handle(b, b"print -rnu2 -- 'brishgarden: no'; return 9000")
            assert (rc, err) == (9000, b"brishgarden: no") and "x-brish-refused" not in h
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
            #: The garden refuses a NUL in legacy mode, before running anything.
            rc, out, err, h = handle(b, b"print -rn -- ran >> sentinel; cat", b"a\0b")
            assert (rc, h["x-brish-binary"], h["x-brish-refused"]) == (9000, "0", "1"), (rc, err)
            assert b"stdin contains a NUL byte" in err, err
            rc, out, err, h = handle(b, b"print -rn -- ran >> sentinel # \0")
            assert (rc, h["x-brish-refused"]) == (9000, "1"), (rc, err)
            #: Non-UTF-8 is refused by the garden, since legacy mode carries text.
            rc, out, err, h = handle(b, b"print -rn -- ran >> sentinel # \xff")
            assert rc == 9000 and b"not valid utf-8" in err, err
            assert h["x-brish-refused"] == "1"
            #: binary=1 asks for exact bytes, which legacy mode cannot give.
            rc, out, err, h = handle(b, b"print -rn -- ran >> sentinel", query={"binary": "1"})
            assert (rc, h["x-brish-binary"], h["x-brish-refused"]) == (9000, "0", "1"), (rc, err)
            assert b"runs in legacy (text) mode" in err and b"Nothing was run" in err, err
            assert not os.path.exists(sentinel)
            rc, out, err, h = handle(b, b"print -rn -- ok", query={"binary": "0"})
            assert (rc, out, h["x-brish-binary"]) == (0, b"ok", "0"), (rc, err)

            rc, out, err, h = handle(b, b"cat", x_brish_stdin="null")
            assert (rc, out, err) == (0, b"", b"")
        finally:
            b.cleanup()
        """
    )


@binary_only
def test_round_trip_binary_option():
    #: In binary mode, binary=1 changes nothing: the reply is exact anyway.
    run_handle(
        r"""
        b = garden_brish(server_count=1)
        try:
            rc, out, err, h = handle(b, b"cat", bytes(range(256)), query={"binary": "1"})
            assert (rc, out, err, h["x-brish-binary"]) == (0, bytes(range(256)), b"", "1"), (rc, err)
            assert "x-brish-refused" not in h
        finally:
            b.cleanup()
        """
    )
