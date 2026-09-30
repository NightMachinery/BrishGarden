"""Reply building, without workers or the garden app.

The reference replies below are the ones the endpoint built inline before
`brishgarden.reply` existed; a request that does not opt in to binary
transport must still get exactly these.
"""

import json

from fastapi import Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from brish import CmdResult
from brishgarden.reply import notice_reply, reply_build


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


def test_notice_reply():
    assert wire(notice_reply("Empty command received.")) == wire(
        Response(content="Empty command received.", media_type="text/plain")
    )
