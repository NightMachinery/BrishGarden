"""Request decoding and reply building, without workers or the garden app.

The reference replies below are the ones the endpoint built inline before
`brishgarden.reply` existed; a request that does not opt in to binary
transport must still get exactly these.
"""

import base64
import json

import pytest
from fastapi import Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from brish import CmdResult
from brishgarden.reply import (
    BINARY_HEADER,
    LEGACY_REFUSAL,
    STDIN_LOG_LIMIT,
    ZshOutcome,
    json_reply,
    notice_reply,
    reply_build,
    request_parse,
    text_display,
    text_safe,
)


def legacy_plain(res):
    return Response(content=res.outerr, media_type="text/plain")


def legacy_json(res, cmd, session, brishes, all_brishes):
    return {
        "cmd": cmd,
        "session": session,
        "brishes": brishes,
        "allBrishes": all_brishes,
        "out": res.out,
        "err": res.err,
        "retcode": res.retcode,
    }


def wire(reply):
    """(status, raw headers, body) as FastAPI would send `reply`."""
    if not isinstance(reply, Response):
        reply = JSONResponse(content=jsonable_encoder(reply))
    return reply.status_code, reply.raw_headers, reply.body


RESULTS = [
    CmdResult(0, "hi\n", "", "echo hi", ""),
    CmdResult(1, "", "boom\n", "false", ""),
    CmdResult(0, "café ×\n", "wärn\n", "print -r -- café", "in\n"),
    CmdResult(0, "", "", "true", ""),
    #: The endpoint's own error result, built positionally.
    CmdResult(9000, "", "Traceback (most recent call last):\n  ...\n", "cmd", "stdin"),
]


def test_plain_reply_is_unchanged():
    for res in RESULTS:
        assert wire(reply_build(res, 0)) == wire(legacy_plain(res))


def test_json_reply_is_unchanged():
    for res in RESULTS:
        got = reply_build(res, 1, cmd=res.cmd, session="s1", brishes=3, all_brishes=5)
        want = legacy_json(res, res.cmd, "s1", 3, 5)
        assert got == want
        assert wire(got) == wire(want)


def test_json_output_values():
    #: Any nonzero json_output is the JSON path, as before.
    res = RESULTS[0]
    for json_output in (1, 2, -1):
        assert isinstance(reply_build(res, json_output, cmd="echo hi"), dict)


def test_json_reply_serializes():
    for res in RESULTS:
        _, _, body = wire(reply_build(res, 1, cmd=res.cmd))
        assert json.loads(body)["out"] == res.out


def test_parse_options():
    #: The options keep the endpoint's old readings: `bool()` of the raw
    #: value, so even the string "0" sets nolog and failure_expected.
    req = request_parse({"cmd": "x"}, binary_mode=True)
    assert (req.session, req.json_output, req.merge) == ("", 0, True)
    assert (req.nolog, req.log_level, req.failure_expected) == (False, 1, False)

    body = {"cmd": "x", "session": "s1", "json_output": "1", "nolog": "0",
            "log_level": "3", "failure_expected": "0"}
    req = request_parse(body, binary_mode=True)
    assert (req.session, req.json_output, req.merge) == ("s1", 1, False)
    assert (req.nolog, req.log_level, req.failure_expected) == (True, 3, True)

    #: The old name of json_output.
    assert request_parse({"cmd": "x", "verbose": 2}, binary_mode=True).json_output == 2
    assert request_parse({"cmd": "x", "verbose": 2, "json_output": 0}, binary_mode=True).merge


@pytest.mark.parametrize("body", [{"json_output": "x"}, {"verbose": None}, {"log_level": "high"}])
def test_parse_options_raise_as_before(body):
    #: The endpoint used to fail here too, answering null; it still does.
    with pytest.raises((ValueError, TypeError)):
        request_parse(dict(body, cmd="x"), binary_mode=True)


def test_json_reply():
    res = RESULTS[2]
    for json_output in (0, 1):
        req = request_parse({"cmd": res.cmd, "session": "s", "json_output": json_output}, binary_mode=True)
        got = json_reply(ZshOutcome(res=res), req, brishes=2, all_brishes=3)
        want = reply_build(res, json_output, cmd=res.cmd, session="s", brishes=2, all_brishes=3)
        assert wire(got) == wire(want)
    req = request_parse({"cmd": ""}, binary_mode=True)
    assert wire(json_reply(ZshOutcome(notice="n"), req)) == wire(notice_reply("n"))
    req = request_parse({"cmd": "", "binary": 1}, binary_mode=True)
    assert wire(json_reply(ZshOutcome(notice="n"), req)) == wire(notice_reply("n", True))


def test_notice_reply():
    assert wire(notice_reply("Empty command received.")) == wire(
        Response(content="Empty command received.", media_type="text/plain")
    )


###
#: Binary transport.

ALL_BYTES = bytes(range(256))
MODES = [True, False]


def b64(data):
    return base64.b64encode(data).decode("ascii")


def header(reply):
    if not isinstance(reply, Response):
        return None
    return reply.headers.get(BINARY_HEADER)


def assert_surrogate_free(*texts):
    for t in texts:
        t.encode("utf-8")


@pytest.mark.parametrize("binary_mode", MODES)
def test_parse_without_new_fields(binary_mode):
    body = {"cmd": "echo hi", "stdin": "x" * 300, "json_output": 1}
    req = request_parse(body, binary_mode=binary_mode)
    assert (req.cmd, req.stdin) == ("echo hi", "x" * 300)
    assert req.cmd_display == "echo hi"
    assert req.stdin_display == "x" * STDIN_LOG_LIMIT
    assert not req.binary_requested and not req.binary_reply
    assert req.error is None


@pytest.mark.parametrize("binary_mode", MODES)
def test_parse_defaults(binary_mode):
    req = request_parse({}, binary_mode=binary_mode)
    assert (req.cmd, req.stdin, req.cmd_display, req.error) == ("", "", "", None)


def test_parse_b64_fields_take_precedence_binary_mode():
    body = {
        "cmd": "not this",
        "cmd_b64": b64(b"print -rn -- \xff"),
        "stdin": "nor this",
        "stdin_b64": b64(ALL_BYTES),
    }
    req = request_parse(body, binary_mode=True)
    assert req.error is None
    assert req.cmd == b"print -rn -- \xff"
    assert req.stdin == ALL_BYTES
    assert req.cmd_display == "print -rn -- \\xff"
    assert_surrogate_free(req.cmd_display, req.stdin_display)
    assert len(req.stdin_display.encode("utf-8")) >= STDIN_LOG_LIMIT


def test_parse_b64_fields_legacy_mode_decode_to_text():
    body = {"cmd": "not this", "cmd_b64": b64("print -r -- café".encode()), "stdin_b64": b64(b"a\nb\n")}
    req = request_parse(body, binary_mode=False)
    assert req.error is None
    assert (req.cmd, req.stdin) == ("print -r -- café", "a\nb\n")


@pytest.mark.parametrize("name", ["cmd", "stdin"])
def test_parse_legacy_mode_refuses_invalid_utf8(name):
    req = request_parse({"cmd": "cat", name + "_b64": b64(b"\xff")}, binary_mode=False)
    assert req.error is not None and f"{name}_b64" in req.error
    assert (req.cmd, req.stdin) == ("", "")


def test_parse_empty_b64_still_takes_precedence():
    req = request_parse({"cmd": "echo hi", "cmd_b64": ""}, binary_mode=True)
    assert req.error is None and req.cmd == b"" and req.cmd_display == ""


def test_parse_null_b64_is_absent():
    req = request_parse({"cmd": "echo hi", "cmd_b64": None}, binary_mode=True)
    assert req.cmd == "echo hi"


@pytest.mark.parametrize("binary_mode", MODES)
@pytest.mark.parametrize(
    "body, field",
    [
        ({"cmd_b64": "not base64!"}, "cmd_b64"),
        ({"cmd_b64": "YQ"}, "cmd_b64"),  # missing padding
        ({"cmd": "cat", "stdin_b64": "@@@@"}, "stdin_b64"),
        ({"cmd_b64": 5}, "cmd_b64"),
        ({"cmd": 5}, "cmd"),
        ({"cmd": "cat", "stdin": ["a"]}, "stdin"),
        ({"cmd": "cat", "stdin": None}, "stdin"),
    ],
)
def test_parse_malformed(binary_mode, body, field):
    req = request_parse(body, binary_mode=binary_mode)
    assert req.error is not None and field in req.error, req
    assert (req.cmd, req.stdin) == ("", "")


def test_parse_b64_ignores_whitespace():
    data = ALL_BYTES * 4
    wrapped = "\n".join(b64(data)[i : i + 76] for i in range(0, len(b64(data)), 76)) + "\n"
    assert request_parse({"cmd": "cat", "stdin_b64": wrapped}, binary_mode=True).stdin == data


@pytest.mark.parametrize(
    "value, want",
    [(1, True), ("1", True), (True, True), ("y", True), (0, False), ("0", False),
     ("", False), (False, False), (None, False), ("no", False), ("false", False)],
)
def test_parse_binary_flag(value, want):
    req = request_parse({"cmd": "cat", "binary": value}, binary_mode=True)
    assert (req.binary_requested, req.binary_reply, req.error) == (want, want, None)


def test_parse_binary_flag_in_legacy_mode_is_refused():
    req = request_parse({"cmd": "touch x", "binary": 1}, binary_mode=False)
    assert req.error == LEGACY_REFUSAL
    assert req.binary_requested and not req.binary_reply
    assert (req.cmd, req.stdin) == ("", "")
    #: The display still shows what was sent, for the log.
    assert req.cmd_display == "touch x"


def test_parse_stdin_display_limit_bytes():
    req = request_parse({"cmd": "cat", "stdin_b64": b64(b"\xff" * 1000)}, binary_mode=True)
    assert req.stdin_display == "\\xff" * STDIN_LOG_LIMIT


def test_text_safe():
    assert text_safe("café") == "café"
    assert text_safe("a\udcffb\ud800") == "a\\udcffb\\ud800"
    assert text_display(b"a\xffb") == "a\\xffb"
    assert text_display(bytearray(b"ab"), limit=1) == "a"


def test_parse_surrogates_in_cmd_are_displayed_safely():
    #: A JSON string can carry lone surrogates ("\udcff"); the echo must
    #: still encode.
    req = request_parse(json.loads('{"cmd": "echo \\udcff"}'), binary_mode=True)
    assert req.cmd == "echo \udcff"
    assert req.cmd_display == "echo \\udcff"


def exact_result(outb, errb, retcode=0):
    return CmdResult.from_bytes(retcode, outb, errb, b"cmd", b"")


def test_binary_plain_reply():
    res = exact_result(ALL_BYTES + b"\r\n\r", b"\0err\xff")
    reply = reply_build(res, 0, True)
    assert reply.body == ALL_BYTES + b"\r\n\r" + b"\0err\xff"
    assert reply.media_type == "application/octet-stream"
    assert reply.headers["content-type"] == "application/octet-stream"
    assert header(reply) == "1"


def test_binary_json_reply():
    res = exact_result(ALL_BYTES, b"e\r\n\xff", retcode=3)
    reply = reply_build(res, 1, True, cmd=b"cat \xff", session="s", brishes=1, all_brishes=2)
    assert header(reply) == "1"
    got = json.loads(reply.body)
    assert base64.b64decode(got["out_b64"]) == ALL_BYTES
    assert base64.b64decode(got["err_b64"]) == b"e\r\n\xff"
    assert (got["out"], got["err"], got["retcode"]) == (res.out, res.err, 3)
    assert got["cmd"] == "cat \\xff"
    assert (got["session"], got["brishes"], got["allBrishes"]) == ("s", 1, 2)
    #: The fields of the non-binary reply, plus the two new ones, in order:
    #: clients such as brishz_para.dash read out and err from it.
    legacy = list(legacy_json(res, "", "", 0, 0))
    assert list(got) == legacy + ["out_b64", "err_b64"]

    #: b64_only drops the text fields, which only duplicate the base64 ones.
    slim = json.loads(reply_build(res, 1, True, b64_only=True, cmd=b"cat \xff", session="s", brishes=1, all_brishes=2).body)
    assert list(slim) == [k for k in legacy if k not in ("out", "err")] + ["out_b64", "err_b64"]
    assert {k: v for k, v in got.items() if k not in ("out", "err")} == slim


def test_b64_only_needs_a_binary_reply():
    res = exact_result(b"x\r\n", b"e")
    #: Without a binary reply, out and err are the only output: b64_only is
    #: ignored.
    for json_output in (1, 2):
        assert reply_build(res, json_output, b64_only=True, cmd="c") == reply_build(res, json_output, cmd="c")
    assert wire(reply_build(res, 0, True, b64_only=True)) == wire(reply_build(res, 0, True))


@pytest.mark.parametrize("value, want", [(1, True), ("1", True), ("y", True), (0, False), ("", False), ("no", False)])
def test_b64_only_request_field(value, want):
    req = request_parse({"cmd": "x", "binary": 1, "json_output": 1, "b64_only": value}, binary_mode=True)
    assert req.b64_only == want
    assert not request_parse({"cmd": "x"}, binary_mode=True).b64_only
    res = exact_result(b"o", b"e")
    got = json.loads(json_reply(ZshOutcome(res=res), req).body)
    assert ("out" in got) == (not want) and ("err" in got) == (not want)
    assert (got["out_b64"], got["err_b64"]) == (b64(b"o"), b64(b"e"))


def test_non_binary_replies_have_no_header():
    res = exact_result(b"x\r\n", b"")
    for json_output in (0, 1):
        assert header(reply_build(res, json_output)) is None
        assert header(reply_build(res, json_output, False)) is None
    assert header(notice_reply("x")) is None
    assert header(notice_reply("x", True)) == "1"


def test_text_views_with_surrogates_still_encode():
    #: The text views of brish results never hold surrogates with the default
    #: decoding errors; a result built by hand might.
    res = CmdResult(0, "out \udcff", "err \ud800", "cmd", "")
    for binary in (False, True):
        _, _, body = wire(reply_build(res, 1, binary, cmd=res.cmd))
        got = json.loads(body)
        assert (got["out"], got["err"]) == ("out \\udcff", "err \\ud800")
    got = json.loads(reply_build(res, 1, True, b64_only=True, cmd=res.cmd).body)
    assert base64.b64decode(got["out_b64"]) == b"out \xff"
    assert base64.b64decode(got["err_b64"]) == b"err \\ud800"
    assert wire(reply_build(res, 0))[2] == "out \\udcfferr \\ud800".encode()


def test_positional_error_result_with_bytes():
    #: The endpoint's error result, built from bytes-like cmd and stdin.
    tb = "Traceback (most recent call last):\n  ...\n"
    res = CmdResult(9000, "", tb, b"cmd \xff", b"stdin\x00\xff")
    assert (res.retcode, res.out, res.err) == (9000, "", tb)
    assert (res.outb, res.errb) == (b"", tb.encode())
    res.longstr.encode("utf-8")
    assert reply_build(res, 0, True).body == tb.encode()
    assert json.loads(reply_build(res, 1, True).body)["err"] == tb
    assert base64.b64decode(json.loads(reply_build(res, 1, True, b64_only=True).body)["err_b64"]) == tb.encode()
    assert wire(reply_build(res, 0)) == wire(legacy_plain(res))
