"""The endpoint's steps against real zsh workers, in a child process with a
timeout."""

import textwrap

import pytest

from tests.conftest import run_py


def test_plain_path_merges_stderr():
    run_py(
        r"""
        from brishgarden.reply import brish_run
        b = garden_brish(server_count=1)
        try:
            res = brish_run(b, "print -r -- out; print -r -- err >&2; print -r -- out2",
                            "", merge=True, server_index=0)
            assert res.retcode == 0, res
            assert res.out == "out\nerr\nout2\n", res
            assert res.err == "", res
        finally:
            b.cleanup()
        """
    )


def test_json_path_keeps_streams_apart():
    run_py(
        r"""
        from brishgarden.reply import brish_run
        b = garden_brish(server_count=1)
        try:
            res = brish_run(b, "print -r -- out; print -r -- err >&2; return 3",
                            "", merge=False, server_index=0)
            assert (res.retcode, res.out, res.err) == (3, "out\n", "err\n"), res
        finally:
            b.cleanup()
        """
    )


def test_cmd_and_stdin_arrive_as_sent():
    run_py(
        r"""
        from brishgarden.reply import brish_run
        b = garden_brish(server_count=1)
        cmd = "x='a  b' ; print -r -- \"$x\" '$HOME' ; cat"
        try:
            for json_output in (0, 1):
                res = brish_run(b, cmd, "line 1\nline 2\n", merge=json_output == 0, server_index=0)
                assert res.retcode == 0, res
                assert res.out == "a  b $HOME\nline 1\nline 2\n", res
        finally:
            b.cleanup()
        """
    )


###
#: Binary transport, through the endpoint's decode, run and reply steps.

#: What the endpoint does with one request, minus the worker pool, sessions
#: and logging. `handle` returns the reply as a Starlette Response.
HANDLE = r'''
import base64, itertools, json
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from brishgarden.reply import (
    BINARY_HEADER, ZshOutcome, brish_run, json_reply, request_parse,
)

def b64(data):
    return base64.b64encode(data).decode("ascii")

def outcome_get(b, req):
    #: The garden's `zsh_handle`, minus the pool, sessions, magic and logging.
    if req.error is not None:
        return ZshOutcome(res=CmdResult(9000, "", req.error, req.cmd_display, req.stdin_display))
    if req.cmd_display == "":
        return ZshOutcome(notice="Empty command received.")
    return ZshOutcome(res=brish_run(b, req.cmd, req.stdin, merge=req.merge, server_index=0))

def handle(b, body):
    req = request_parse(body, binary_mode=b.binary, encoding=b.encoding)
    reply = json_reply(outcome_get(b, req), req)
    if not isinstance(reply, Response):
        reply = JSONResponse(content=jsonable_encoder(reply))
    return reply

def wire(reply):
    return reply.status_code, reply.raw_headers, reply.body
'''


def run_handle(snippet, **kw):
    return run_py(HANDLE + textwrap.dedent(snippet), **kw)


def test_binary_round_trip():
    run_handle(
        r"""
        b = Brish(binary=True, server_count=1)
        payloads = [bytes(range(256)), b"\0", b"a\n\0", b"a\r\0", b"\r", b"\r\n",
                    b"\n\0\n0\n", b"", os.urandom(1 << 20)]
        try:
            for data in payloads:
                plain = handle(b, {"cmd_b64": b64(b"cat"), "stdin_b64": b64(data), "binary": 1})
                assert plain.headers[BINARY_HEADER] == "1"
                assert plain.media_type == "application/octet-stream"
                assert plain.body == data, (len(plain.body), len(data))

                for (cmd, field), b64_only in itertools.product(
                        ((b"cat", "out_b64"), (b"cat >&2", "err_b64")), (0, 1)):
                    reply = handle(b, {"cmd_b64": b64(cmd), "stdin_b64": b64(data),
                                       "binary": 1, "json_output": 1, "b64_only": b64_only})
                    assert reply.headers[BINARY_HEADER] == "1"
                    got = json.loads(reply.body)
                    assert got["retcode"] == 0, got
                    assert base64.b64decode(got[field]) == data
                    #: b64_only drops the text fields, which duplicate the
                    #: base64 ones; without it they stay.
                    assert ("out" in got and "err" in got) == (not b64_only), sorted(got)
        finally:
            b.cleanup()
        """,
        timeout=120,
    )


def test_binary_command_bytes():
    #: Raw non-UTF-8 bytes in the command itself, through both reply paths.
    run_handle(
        r"""
        b = Brish(binary=True, server_count=1)
        cmd = b"print -rn -- \xff\xfe'\x01 x'"
        try:
            plain = handle(b, {"cmd_b64": b64(cmd), "binary": 1})
            assert plain.body == b"\xff\xfe\x01 x", plain.body
            got = json.loads(handle(b, {"cmd_b64": b64(cmd), "binary": 1, "json_output": 1}).body)
            assert base64.b64decode(got["out_b64"]) == b"\xff\xfe\x01 x", got
            assert got["cmd"] == "print -rn -- \\xff\\xfe'\x01 x'", got
        finally:
            b.cleanup()
        """
    )


def test_b64_input_without_binary_output():
    #: The `_b64` fields alone carry exact input; the reply stays text.
    run_handle(
        r"""
        b = Brish(binary=True, server_count=1)
        try:
            body = {"cmd_b64": b64(b"od -An -tx1"), "stdin_b64": b64(b"\0\xff\n"), "json_output": 1}
            reply = handle(b, body)
            assert BINARY_HEADER not in reply.headers
            got = json.loads(reply.body)
            assert got["out"].split() == ["00", "ff", "0a"], got
            assert "out_b64" not in got
        finally:
            b.cleanup()
        """
    )


def test_non_opt_in_replies_match_across_modes():
    #: A request without the new fields gets the same reply from a binary-mode
    #: Brish as from a legacy-mode one, except for exact CR handling.
    run_handle(
        r"""
        bb = Brish(binary=True, server_count=1)
        bl = Brish(binary=False, server_count=1)
        same = [
            {"cmd": "print -r -- hi"},
            {"cmd": "print -r -- out; print -r -- err >&2; return 4"},
            {"cmd": "cat", "stdin": "café\nline 2\n\n"},
            {"cmd": "print -rn -- $'\\xff\\xfe' ; print"},
            {"cmd": "print -rn -- no newline"},
            {"cmd": "false"},
            {"cmd": "print -r -- $'\\e[1mbold\\e[0m'"},
        ]
        try:
            for body in same:
                for json_output in (0, 1):
                    body = dict(body, json_output=json_output)
                    assert wire(handle(bb, body)) == wire(handle(bl, body)), body

            cr = {"cmd": "print -rn -- $'a\\r\\nb\\rc\\r'"}
            assert handle(bb, cr).body == b"a\r\nb\rc\r"
            #: Legacy mode lost a CR until Brish's legacy backports (2026-10),
            #: which made its text views exact too.
            if bl.send_cmd("print -rn -- $'\\r'").out == "\r":
                assert handle(bl, cr).body == b"a\r\nb\rc\r"
            else:
                assert handle(bl, cr).body == b"a\nb\nc"
        finally:
            bb.cleanup()
            bl.cleanup()
        """,
        timeout=120,
    )


def test_legacy_mode_refuses_binary_requests():
    run_handle(
        r"""
        b = Brish(binary=False, server_count=1)
        sentinel = os.path.join(os.getcwd(), "sentinel")
        ran = b"print -rn -- ran >> sentinel"
        try:
            for json_output in (0, 1):
                reply = handle(b, {"cmd_b64": b64(ran), "binary": 1, "json_output": json_output})
                assert BINARY_HEADER not in reply.headers
                assert b"legacy (text) mode" in reply.body, reply.body
                if json_output:
                    assert json.loads(reply.body)["retcode"] == 9000
            assert not os.path.exists(sentinel)

            #: Non-UTF-8 input is refused too, since legacy mode carries text.
            reply = handle(b, {"cmd_b64": b64(ran + b" # \xff"), "json_output": 1})
            assert json.loads(reply.body)["retcode"] == 9000
            assert not os.path.exists(sentinel)
        finally:
            b.cleanup()
        """
    )


def test_legacy_mode_serves_text_b64_fields():
    run_handle(
        r"""
        b = Brish(binary=False, server_count=1)
        try:
            body = {"cmd_b64": b64("print -r -- café; cat".encode()),
                    "stdin_b64": b64(b"in\n"), "json_output": 1}
            reply = handle(b, body)
            assert BINARY_HEADER not in reply.headers
            got = json.loads(reply.body)
            assert (got["retcode"], got["out"]) == (0, "café\nin\n"), got
            assert "out_b64" not in got

            #: NUL cannot travel in legacy mode; brish refuses it before running.
            body = {"cmd": "cat", "stdin_b64": b64(b"a\0b"), "json_output": 1}
            assert json.loads(handle(b, body).body)["retcode"] == 9000
        finally:
            b.cleanup()
        """
    )


def test_binary_request_errors_carry_the_header():
    #: A binary-mode garden marks every reply to a binary request, so a client
    #: can tell a bad request from a garden without binary support.
    run_handle(
        r"""
        b = Brish(binary=True, server_count=1)
        try:
            for json_output in (0, 1):
                reply = handle(b, {"cmd_b64": "not base64!", "binary": 1, "json_output": json_output})
                assert reply.headers[BINARY_HEADER] == "1"
                err = reply.body
                if json_output:
                    got = json.loads(reply.body)
                    assert got["retcode"] == 9000
                    err = base64.b64decode(got["err_b64"])
                assert b"cmd_b64 is not valid base64" in err
        finally:
            b.cleanup()
        """
    )


###
#: The garden's mode, chosen by `BRISH_BINARY` (brishgarden/mode.py).

#: (BRISH_BINARY in the garden's environment, or None for unset; binary mode?)
GARDEN_ENVS = [(None, True), ("", True), ("1", True), ("0", False), ("no", False)]


@pytest.mark.parametrize("value, binary", GARDEN_ENVS)
def test_garden_mode_follows_env(value, binary):
    #: The garden's workers run in the mode BRISH_BINARY selects, and the
    #: garden leaves BRISH_BINARY alone: its commands see the environment the
    #: garden was started with, and a Python script that a command starts
    #: keeps brish's library default (legacy when unset).
    run_py(
        r"""
        import sys
        want = {value!r}
        before = dict(os.environ)
        b = garden_brish(server_count=1)
        try:
            assert (GARDEN_MODE.binary, b.binary) == ({binary!r}, {binary!r}), GARDEN_MODE
            assert dict(os.environ) == before
            res = b.send_cmd('print -r -- "${{BRISH_BINARY-<unset>}}"')
            assert res.out == ("<unset>" if want is None else want) + "\n", res
            script = "import brish; print(brish.Brish(delayed_init=True).binary)"
            res = b.z("{{exe}} -c {{script}}", locals_={{"exe": sys.executable, "script": script}})
            library_default = want is not None and want not in ("", "0", "no")
            assert (res.retcode, res.out) == (0, f"{{library_default}}\n"), res
        finally:
            b.cleanup()
        """.format(value=value, binary=binary),
        env={"BRISH_BINARY": value},
    )


def test_default_serves_binary_requests():
    #: With BRISH_BINARY unset, a `binary: 1` request gets exact bytes.
    run_handle(
        r"""
        b = garden_brish(server_count=1)
        try:
            assert b.binary
            reply = handle(b, {"cmd_b64": b64(b"cat"), "stdin_b64": b64(b"\xff\0\r\n"), "binary": 1})
            assert reply.headers[BINARY_HEADER] == "1"
            assert reply.body == b"\xff\0\r\n", reply.body
        finally:
            b.cleanup()
        """,
        env={"BRISH_BINARY": None},
    )


def test_kill_switch_refuses_binary_requests():
    #: BRISH_BINARY=0 runs the garden in legacy mode: a `binary: 1` request
    #: is refused, and nothing runs.
    run_handle(
        r"""
        from brishgarden.reply import LEGACY_REFUSAL
        b = garden_brish(server_count=1)
        sentinel = os.path.join(os.getcwd(), "sentinel")
        try:
            assert not b.binary
            for json_output in (0, 1):
                body = {"cmd_b64": b64(b"print -rn -- ran >> sentinel"), "binary": 1,
                        "json_output": json_output}
                reply = handle(b, body)
                assert BINARY_HEADER not in reply.headers
                if json_output:
                    got = json.loads(reply.body)
                    assert (got["retcode"], got["err"]) == (9000, LEGACY_REFUSAL), got
                else:
                    assert reply.body == LEGACY_REFUSAL.encode(), reply.body
            assert "without BRISH_BINARY=0" in LEGACY_REFUSAL
            assert not os.path.exists(sentinel)

            #: Requests that do not opt in still run, as text.
            reply = handle(b, {"cmd": "print -r -- hi", "json_output": 1})
            assert json.loads(reply.body)["out"] == "hi\n"
        finally:
            b.cleanup()
        """,
        env={"BRISH_BINARY": "0"},
    )
