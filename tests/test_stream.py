"""The streaming API (`/zsh/stream/`): frames, the shared request path, the
channel between the owner thread and the event loop, and the response,
then round trips through real zsh workers in a child process.

The round trips serve the response to a fake ASGI client (no TestClient,
no garden import): it records what the response sends, and can disconnect
the way uvicorn reports it, with an `http.disconnect` message.
"""

import asyncio
import dataclasses
import struct
import textwrap
import threading
import time

import pytest
from starlette.datastructures import Headers, QueryParams

from brish import CmdResult
from brishgarden.reply import (
    BINARY_HEADER,
    CMD_LENGTH_HEADER,
    NOTICE_HEADER,
    REFUSED_HEADER,
    ZshOutcome,
    raw_request_parse,
)
from brishgarden.stream import (
    CHANNEL_BYTES,
    FRAME_EXIT,
    FRAME_STDERR,
    FRAME_STDOUT,
    STREAM_HEADER,
    DaemonPool,
    FrameChannel,
    FrameDecoder,
    StreamResponse,
    StreamResult,
    end_frames,
    exit_frame,
    frame,
    outcome_frames,
    outcome_reply,
    stream_headers,
)
from tests.conftest import binary_only, legacy_only, run_py

MODES = [True, False]


###
#: Frames


def test_frame_layout():
    assert frame(FRAME_STDOUT, b"ab") == b"\x01\x00\x00\x00\x02ab"
    assert frame(FRAME_STDERR, b"") == b"\x02\x00\x00\x00\x00"
    big = bytes(70000)
    assert frame(FRAME_STDOUT, big)[:5] == b"\x01" + struct.pack(">I", 70000)
    assert frame(FRAME_STDOUT, memoryview(b"xy")) == b"\x01\x00\x00\x00\x02xy"
    assert exit_frame(0) == b"\x03\x00\x00\x00\x010"
    assert exit_frame(300) == b"\x03\x00\x00\x00\x03300"
    assert exit_frame(9001)[5:] == b"9001"
    assert exit_frame(-1)[5:] == b"-1"
    assert (FRAME_STDOUT, FRAME_STDERR, FRAME_EXIT) == (1, 2, 3)


SAMPLE = [
    (FRAME_STDOUT, bytes(range(256))),
    (FRAME_STDERR, b"\0"),
    (FRAME_STDOUT, b"\r\n" * 3),
    (7, b"a frame of a type a later garden may add"),
    (FRAME_STDOUT, b"x" * 70000),
    (FRAME_EXIT, b"3"),
]


def test_decoder_at_every_split():
    body = b"".join(frame(k, p) for k, p in SAMPLE)
    for cut in range(len(body) + 1):
        d = FrameDecoder()
        got = d.feed(body[:cut]) + d.feed(body[cut:])
        assert got == SAMPLE, cut
        assert d.pending == 0
    d = FrameDecoder()
    got = []
    for i in range(len(body)):
        got += d.feed(body[i : i + 1])
    assert got == SAMPLE


def test_decoder_reports_a_partial_frame():
    body = frame(FRAME_STDOUT, b"hello") + exit_frame(0)
    for cut in range(1, len(body)):
        d = FrameDecoder()
        frames = d.feed(body[:cut])
        #: Only whole frames come out; the rest is pending.
        assert sum(5 + len(p) for _, p in frames) + d.pending == cut


def test_stream_headers():
    assert stream_headers(True) == {BINARY_HEADER: "1", STREAM_HEADER: "1"}
    assert stream_headers(False, notice=True) == {BINARY_HEADER: "0", STREAM_HEADER: "1", NOTICE_HEADER: "1"}
    assert stream_headers(True, refused=True)[REFUSED_HEADER] == "1"


def test_outcome_frames():
    d = FrameDecoder()
    assert d.feed(outcome_frames(ZshOutcome(notice="Empty command received."))) == [
        (FRAME_STDOUT, b"Empty command received."),
        (FRAME_EXIT, b"0"),
    ]
    #: A lone surrogate is escaped as text, as on the raw API.
    assert d.feed(outcome_frames(ZshOutcome(notice="x \udcff")))[0] == (FRAME_STDOUT, b"x \\udcff")
    refusal = CmdResult(9000, "", "brishgarden: no\n", "", "")
    assert d.feed(outcome_frames(ZshOutcome(res=refusal, refused=True))) == [
        (FRAME_STDERR, b"brishgarden: no\n"),
        (FRAME_EXIT, b"9000"),
    ]
    assert d.pending == 0


def test_end_frames():
    d = FrameDecoder()
    #: A streamed result's output went out already; its out and err are
    #: only a tail for the log.
    streamed = StreamResult.from_bytes(3, b"tail", b"err tail", "cmd", "")
    assert d.feed(b"".join(end_frames(streamed))) == [(FRAME_EXIT, b"3")]
    #: The garden's own result (an exception) goes out whole.
    own = CmdResult.from_bytes(9000, b"o\xff", b"Traceback\n", "cmd", "")
    assert d.feed(b"".join(end_frames(own))) == [
        (FRAME_STDOUT, b"o\xff"),
        (FRAME_STDERR, b"Traceback\n"),
        (FRAME_EXIT, b"9000"),
    ]


###
#: The shared request path: the streaming API decodes the raw API's request
#: with the raw API's code, and only marks it as streamed.

REQUESTS = [
    (b"cat \xffxyz", {CMD_LENGTH_HEADER: "8"}, {}),
    (b"cat", {CMD_LENGTH_HEADER: "3", "X-Brish-Stdin": "null"}, {"session": "s", "merge": "1", "log_level": "2"}),
    (b"print -r -- hi", {"x-brish-cmd-length": "14"}, QueryParams("nolog=y&failure_expected=1")),
    (b"", {CMD_LENGTH_HEADER: "0"}, {}),
    (b"cat", {}, {}),
    (b"cat", {CMD_LENGTH_HEADER: "9"}, {}),
    (b"cat", {CMD_LENGTH_HEADER: "3"}, {"log_level": "high"}),
    (b"cat\0", {CMD_LENGTH_HEADER: "4"}, {}),
    (b"catx", {CMD_LENGTH_HEADER: "3", "X-Brish-Stdin": "null"}, {}),
    (b"print -r -- hi", Headers(raw=[(b"x-brish-cmd-length", b"5"), (b"x-brish-cmd-length", b"14")]), {}),
    (b"cat", {CMD_LENGTH_HEADER: "3"}, QueryParams("merge=1&merge=0")),
]


@pytest.mark.parametrize("binary_mode", MODES)
@pytest.mark.parametrize("body, headers, query", REQUESTS)
def test_stream_request_is_the_raw_request(binary_mode, body, headers, query):
    raw = raw_request_parse(body, headers, query, binary_mode=binary_mode)
    streamed = raw_request_parse(body, headers, query, binary_mode=binary_mode, stream=True)
    assert not raw.stream and streamed.stream and streamed.raw
    assert streamed == dataclasses.replace(raw, stream=True)


###
#: The channel between an owner thread and the event loop.


class FakePopen:
    def __init__(self):
        self.retcode = None
        self.kills = 0

    def kill(self):
        self.kills += 1


def test_channel_backpressure_and_cancel():
    async def main():
        ch = FrameChannel(asyncio.get_running_loop(), max_bytes=10)
        state = {"put": 0, "results": []}

        def owner():
            for i in range(5):
                state["results"].append(ch.put(b"x" * 6))
                state["put"] += 1

        t = threading.Thread(target=owner, daemon=True)
        t.start()
        await asyncio.sleep(0.3)
        #: Two frames fill the queue (12 >= 10 bytes); the third put blocks.
        assert state["put"] == 2 and ch.queued == 12
        frames = ch.frames(limit=6)
        assert await frames.__anext__() == b"x" * 6
        await asyncio.sleep(0.2)
        #: One frame out, one more in: the owner is blocked again.
        assert state["put"] == 3 and ch.queued == 12
        #: The client goes away: the blocked put returns False, and so do
        #: all later ones; nothing is queued any more.
        p = FakePopen()
        assert ch.popen_set(p)
        ch.cancel()
        t.join(5)
        assert not t.is_alive()
        assert state["results"] == [True, True, True, False, False]
        assert ch.queued == 0 and ch.gone and ch.killed and p.kills == 1
        ch.cancel()
        assert p.kills == 1
        #: A popen published after the cancel is refused; its owner kills it.
        assert not ch.popen_set(FakePopen())

    asyncio.run(main())


def test_channel_frames_end_when_closed():
    async def main():
        ch = FrameChannel(asyncio.get_running_loop())

        def owner():
            for i in range(3):
                time.sleep(0.05)
                ch.put(frame(FRAME_STDOUT, b"%d" % i))
            ch.put(exit_frame(0))
            ch.close()

        threading.Thread(target=owner, daemon=True).start()
        d = FrameDecoder()
        got = []
        async for data in ch.frames():
            got += d.feed(data)
        assert got == [(FRAME_STDOUT, b"0"), (FRAME_STDOUT, b"1"), (FRAME_STDOUT, b"2"), (FRAME_EXIT, b"0")]
        #: Cancelling a finished channel kills nothing.
        p = FakePopen()
        ch.popen_set(p)
        ch.cancel()
        assert p.kills == 0 and not ch.killed

    asyncio.run(main())


def test_channel_cancel_spares_an_ended_command():
    ch = FrameChannel(None)
    p = FakePopen()
    p.retcode = 0
    ch.popen_set(p)
    ch.cancel()
    assert p.kills == 0 and not ch.killed and ch.gone


###
#: The response, served to a fake ASGI client.


def serve_fake(response, disconnect_after=None):
    """Serve `response`; returns [(time, message)] of what it sent. The
    client disconnects after `disconnect_after` seconds, if given."""
    sent = []

    async def main():
        gone = asyncio.Event()

        async def receive():
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append((time.monotonic(), message))

        if disconnect_after is not None:
            asyncio.get_running_loop().call_later(disconnect_after, gone.set)
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "method": "POST", "path": "/zsh/stream/"}
        await response(scope, receive, send)

    asyncio.run(main())
    return sent


def body_of(sent):
    return b"".join(m.get("body", b"") for _, m in sent if m["type"] == "http.response.body")


def test_response_sends_headers_then_frames():
    p = FakePopen()
    done = threading.Event()

    def start(ch):
        def owner():
            ch.popen_set(p)
            for i in range(3):
                ch.put(frame(FRAME_STDOUT, b"%d" % i))
                time.sleep(0.05)
            p.retcode = 0
            ch.put(exit_frame(0))
            ch.close()
            done.set()

        threading.Thread(target=owner, daemon=True).start()

    response = StreamResponse(start, stream_headers(True))
    sent = serve_fake(response)
    start_msg = sent[0][1]
    assert start_msg["type"] == "http.response.start" and start_msg["status"] == 200
    headers = dict(start_msg["headers"])
    assert headers[b"x-brish-stream"] == b"1" and headers[b"x-brish-binary"] == b"1"
    assert headers[b"content-type"] == b"application/octet-stream"
    assert b"content-length" not in headers
    assert FrameDecoder().feed(body_of(sent)) == [
        (FRAME_STDOUT, b"0"), (FRAME_STDOUT, b"1"), (FRAME_STDOUT, b"2"), (FRAME_EXIT, b"0"),
    ]
    assert sent[-1][1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert done.wait(5) and p.kills == 0 and not response.channel.killed


def test_response_disconnect_kills_the_command():
    p = FakePopen()
    blocked = {}

    def start(ch):
        def owner():
            ch.popen_set(p)
            ch.put(frame(FRAME_STDOUT, b"first"))
            #: A quiet command: nothing more until it is killed.
            while not p.kills:
                time.sleep(0.01)
            blocked["put"] = ch.put(frame(FRAME_STDOUT, b"after the kill"))
            ch.close()

        threading.Thread(target=owner, daemon=True).start()

    t0 = time.monotonic()
    response = StreamResponse(start, stream_headers(False))
    sent = serve_fake(response, disconnect_after=0.3)
    assert 0.25 < time.monotonic() - t0 < 2
    assert FrameDecoder().feed(body_of(sent)) == [(FRAME_STDOUT, b"first")]
    assert p.kills == 1 and response.channel.killed and response.channel.gone
    deadline = time.monotonic() + 5
    while "put" not in blocked and time.monotonic() < deadline:
        time.sleep(0.01)
    assert blocked == {"put": False}


def test_response_that_never_starts_runs_nothing():
    started = []
    StreamResponse(started.append, stream_headers(True))
    assert started == []


def test_outcome_reply():
    sent = serve_fake(outcome_reply(ZshOutcome(notice="Empty command received."), False))
    headers = dict(sent[0][1]["headers"])
    assert headers[b"x-brish-notice"] == b"1" and headers[b"x-brish-binary"] == b"0"
    assert b"x-brish-refused" not in headers
    assert FrameDecoder().feed(body_of(sent)) == [(FRAME_STDOUT, b"Empty command received."), (FRAME_EXIT, b"0")]
    refusal = ZshOutcome(res=CmdResult(9000, "", "brishgarden: no\n", "", ""), refused=True)
    sent = serve_fake(outcome_reply(refusal, True))
    headers = dict(sent[0][1]["headers"])
    assert headers[b"x-brish-refused"] == b"1" and b"x-brish-notice" not in headers
    assert FrameDecoder().feed(body_of(sent)) == [(FRAME_STDERR, b"brishgarden: no\n"), (FRAME_EXIT, b"9000")]


###
#: The pool of owner threads.


def test_daemon_pool_is_bounded():
    pool = DaemonPool(2, "test-pool")
    gate = threading.Event()
    lock = threading.Lock()
    state = {"running": 0, "most": 0, "done": 0, "daemon": []}

    def job():
        with lock:
            state["running"] += 1
            state["most"] = max(state["most"], state["running"])
            state["daemon"].append(threading.current_thread().daemon)
        gate.wait(5)
        with lock:
            state["running"] -= 1
            state["done"] += 1

    for _ in range(5):
        pool.submit(job)
    time.sleep(0.3)
    assert state["running"] == 2
    gate.set()
    deadline = time.monotonic() + 5
    while state["done"] < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert state["done"] == 5 and state["most"] == 2 and all(state["daemon"])
    #: Idle threads take later jobs; the pool never grows past its size.
    gate.clear()
    for _ in range(3):
        pool.submit(job)
    time.sleep(0.3)
    assert state["running"] == 2 and pool._threads == 2
    gate.set()


def test_daemon_pool_survives_a_failing_job():
    logged = []

    class Logger:
        def warning(self, msg):
            logged.append(msg)

    pool = DaemonPool(1, "test-pool", logger=Logger())
    done = threading.Event()
    pool.submit(lambda: 1 / 0)
    pool.submit(done.set)
    assert done.wait(5)
    assert "ZeroDivisionError" in logged[0]


###
#: Round trips through real workers.

#: The garden's stream route minus the pool, sessions, magic and logging,
#: served to a fake ASGI client. `serve` returns the reply: its headers, its
#: frames with the time each arrived (from the start), the owner's result
#: and the channel.
HANDLE = r'''
import asyncio, threading, time
from brishgarden.reply import ZshOutcome, raw_request_parse
from brishgarden.stream import (
    FRAME_EXIT, FRAME_STDERR, FRAME_STDOUT, FrameDecoder, StreamResponse,
    outcome_reply, stream_headers, stream_own, stream_run,
)

class Reply:
    def __repr__(self):
        return f"Reply(headers={self.headers}, frames={[(k, p[:60]) for _, k, p in self.frames]}, res={self.res!r})"

    def payload(self, kind):
        return b"".join(p for _, k, p in self.frames if k == kind)

    @property
    def retcode(self):
        exits = [p for _, k, p in self.frames if k == FRAME_EXIT]
        assert len(exits) <= 1 and (not exits or self.frames[-1][1] == FRAME_EXIT), self
        return int(exits[0]) if exits else None

def serve(b, cmd, stdin=b"", query=None, disconnect_after=None, send_delay=0, block_send=False, **headers):
    hdrs = {"x-brish-cmd-length": str(len(cmd))}
    hdrs.update({k.replace("_", "-"): v for k, v in headers.items()})
    req = raw_request_parse(cmd + stdin, hdrs, query or {}, binary_mode=b.binary, encoding=b.encoding, stream=True)
    outcome = None
    if req.error is not None:
        outcome = ZshOutcome(res=CmdResult(9000, "", req.error, req.cmd_display, req.stdin_display), refused=True)
    elif req.cmd_display == "":
        outcome = ZshOutcome(notice="Empty command received.")
    reply = Reply()
    reply.res = None
    owner = {}

    def start(channel):
        def run():
            reply.res = stream_own(channel, lambda: None if channel.gone else stream_run(b, 0, req, channel))
        owner["thread"] = threading.Thread(target=run, daemon=True)
        owner["thread"].start()

    if outcome is not None:
        response = outcome_reply(outcome, req.binary_reply)
    else:
        response = StreamResponse(start, stream_headers(req.binary_reply))
    sent = []
    reply.queued = []

    async def main():
        gone = asyncio.Event()
        never = asyncio.Event()

        async def receive():
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append((time.monotonic(), message))
            if block_send and message["type"] == "http.response.body":
                await never.wait()
            if send_delay:
                await asyncio.sleep(send_delay)
            if response.__class__ is StreamResponse:
                #: What the owner queued while this send took.
                reply.queued.append(response.channel.queued)

        if disconnect_after is not None:
            asyncio.get_running_loop().call_later(disconnect_after, gone.set)
        await response({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}}, receive, send)

    t0 = time.monotonic()
    asyncio.run(main())
    reply.served = time.monotonic() - t0
    if "thread" in owner:
        owner["thread"].join(60)
        assert not owner["thread"].is_alive()
    reply.ended = time.monotonic() - t0
    reply.channel = getattr(response, "channel", None)
    reply.headers = {k.decode(): v.decode() for k, v in sent[0][1]["headers"]}
    d = FrameDecoder()
    reply.frames = []
    for t, m in sent[1:]:
        for kind, payload in d.feed(m.get("body", b"")):
            reply.frames.append((t - t0, kind, payload))
    assert d.pending == 0
    return reply
'''


def run_handle(snippet, **kw):
    return run_py(HANDLE + textwrap.dedent(snippet), **kw)


def test_stream_round_trip():
    run_handle(
        r"""
        b = garden_brish(server_count=1)
        try:
            flag = "1" if BINARY else "0"
            r = serve(b, b"cat; print -rn -- err >&2; return 3", "line 1\ncafé\n".encode())
            assert r.retcode == 3, r
            assert (r.payload(FRAME_STDOUT), r.payload(FRAME_STDERR)) == ("line 1\ncafé\n".encode(), b"err"), r
            assert r.headers["x-brish-binary"] == flag and r.headers["x-brish-stream"] == "1"
            assert "x-brish-notice" not in r.headers and "x-brish-refused" not in r.headers
            assert r.res.retcode == 3 and not r.channel.killed

            #: merge=1: one stream, all stdout, in order.
            r = serve(b, b"print -r -- out; print -r -- err >&2; print -r -- out2; return 4", query={"merge": "1"})
            assert (r.retcode, r.payload(FRAME_STDOUT), r.payload(FRAME_STDERR)) == (4, b"out\nerr\nout2\n", b""), r
            assert {k for _, k, _ in r.frames} == {FRAME_STDOUT, FRAME_EXIT}

            #: Retcodes beyond 255 come whole; zsh's `return` keeps them.
            for rc in (0, 1, 255, 300, 9000):
                assert serve(b, b"return %d" % rc).retcode == rc

            r = serve(b, b"")
            assert (r.headers["x-brish-notice"], r.retcode, r.payload(FRAME_STDOUT)) == ("1", 0, b"Empty command received."), r

            r = serve(b, b"cat", b"tail", x_brish_cmd_length="9")
            assert (r.retcode, r.headers["x-brish-refused"]) == (9000, "1"), r
            assert b"has only 7 bytes" in r.payload(FRAME_STDERR)

            #: A command that ran is never marked refused.
            r = serve(b, b"print -rnu2 -- 'brishgarden: no'; return 9000")
            assert (r.retcode, r.payload(FRAME_STDERR)) == (9000, b"brishgarden: no") and "x-brish-refused" not in r.headers

            #: Output larger than the channel, through the backpressure path.
            r = serve(b, b"print -rn -- " + b"x" * (100 << 10) + b"; for i in {1..2000}; do print -r -- line $i; done")
            want = b"x" * (100 << 10) + b"".join(b"line %d\n" % i for i in range(1, 2001))
            assert (r.retcode, r.payload(FRAME_STDOUT)) == (0, want), (r.retcode, len(r.payload(FRAME_STDOUT)))

            #: The worker keeps its state across streams (no fork).
            serve(b, b"typeset -g kept=yes")
            assert serve(b, b"print -rn -- $kept").payload(FRAME_STDOUT) == b"yes"
        finally:
            b.cleanup()
        """,
        timeout=120,
    )


def test_stream_delivers_while_running():
    #: Each line arrives while the command runs, not at its end.
    run_handle(
        r"""
        b = garden_brish(server_count=1)
        try:
            r = serve(b, b"for i in 1 2 3 4 5; do print -r -- line $i; sleep 0.2; done; return 3")
            assert r.retcode == 3, r
            assert r.payload(FRAME_STDOUT) == b"".join(b"line %d\n" % i for i in range(1, 6)), r
            out = [(t, p) for t, k, p in r.frames if k == FRAME_STDOUT]
            end = r.frames[-1][0]
            #: The first line comes about a second before the end, and the
            #: lines spread over that second (legacy mode holds a line's
            #: newline back until the next chunk).
            assert end - out[0][0] > 0.6, r.frames
            assert len(out) >= 4, out
            times = [t for t, _ in out]
            assert times[-1] - times[0] > 0.5, times
            print("frame times:", [round(t, 2) for t, _ in out], "end", round(end, 2))
        finally:
            b.cleanup()
        """
    )


def test_stream_disconnect_kills_the_command():
    #: A client that goes away 0.3 s into the command kills it: the
    #: sentinel is never written, the worker keeps its state, and the next
    #: command on it works.
    run_handle(
        r"""
        b = garden_brish(server_count=1)
        sentinel = os.path.join(os.getcwd(), "sentinel")
        try:
            b.send_cmd("typeset -g kept=yes", server_index=0)
            pid = b.send_cmd("print -rn -- $$", server_index=0).out
            t0 = time.monotonic()
            r = serve(b, b"sleep 1; print -r -- ran >> sentinel", disconnect_after=0.3)
            assert r.frames == [] and r.served < 0.9, r
            assert r.res.retcode == 130 and r.channel.killed, r.res
            #: The owner drained the command right after the kill.
            assert r.ended < 0.9, r.ended
            time.sleep(1.5)
            assert not os.path.exists(sentinel)
            res = b.send_cmd("print -rn -- $kept $$", server_index=0)
            assert (res.retcode, res.out) == (0, f"yes {pid}"), res

            #: The same while output flows: no exit frame goes out.
            r = serve(b, b"while :; do print -r -- tick; sleep 0.05; done", disconnect_after=0.4)
            assert r.res.retcode == 130 and r.channel.killed and r.payload(FRAME_STDOUT).startswith(b"tick\n"), r
            assert r.retcode is None, r
            assert b.send_cmd("print -rn -- ok", server_index=0).out == "ok"

            #: A client gone at once: the command is killed as it starts,
            #: or never runs.
            r = serve(b, b"sleep 0.5; print -r -- ran >> sentinel", disconnect_after=0)
            time.sleep(1)
            assert not os.path.exists(sentinel) and r.retcode is None, r
            assert r.res is None or r.res.retcode == 130, r.res
            assert b.send_cmd("print -rn -- ok", server_index=0).out == "ok"
        finally:
            b.cleanup()
        """
    )


def test_stream_stalled_client_then_gone():
    #: A client that stops reading blocks the command (backpressure), and
    #: once it is gone the command is killed and the worker freed, also
    #: when the command ended by itself in the meantime.
    run_handle(
        r"""
        b = garden_brish(server_count=1)
        try:
            r = serve(b, b"head -c 20000000 /dev/zero; print -r -- end", block_send=True, disconnect_after=1.5)
            assert r.res.retcode == 130 and r.channel.killed, r.res
            assert r.ended < 5, r.ended
            assert b.send_cmd("print -rn -- ok", server_index=0).out == "ok"

            #: Small output fits in the buffers: the command ends by itself,
            #: and the reply's end never goes out; the worker is freed when
            #: the client leaves.
            r = serve(b, b"print -r -- small; return 5", block_send=True, disconnect_after=0.5)
            assert r.res.retcode == 5 and not r.channel.killed, r.res
            assert b.send_cmd("print -rn -- ok", server_index=0).out == "ok"
        finally:
            b.cleanup()
        """
    )


def test_stream_backpressure_bounds_the_queue():
    #: A slow client: the garden never queues much more than the channel's
    #: bound, however much the command writes.
    run_handle(
        r"""
        from brishgarden.stream import CHANNEL_BYTES
        b = garden_brish(server_count=1)
        try:
            r = serve(b, b"head -c 8000000 /dev/zero", send_delay=0.02)
            assert (r.retcode, len(r.payload(FRAME_STDOUT))) == (0, 8000000), r.retcode
            #: The channel filled up, and never beyond its bound plus one frame.
            assert CHANNEL_BYTES // 2 <= max(r.queued) <= CHANNEL_BYTES + (64 << 10) + 5, max(r.queued)
            print("most queued:", max(r.queued), "sends:", len(r.queued))
        finally:
            b.cleanup()
        """,
        timeout=120,
    )


@binary_only
def test_stream_exact_bytes():
    run_handle(
        r"""
        b = garden_brish(server_count=1)
        payloads = [bytes(range(256)), b"\0", b"a\n\0", b"a\r\0", b"\r", b"\r\n",
                    b"\n\0\n0\n", b"x\n\n\n", b"", os.urandom(1 << 20)]
        try:
            for data in payloads:
                r = serve(b, b"cat", data)
                assert (r.retcode, r.payload(FRAME_STDOUT), r.payload(FRAME_STDERR)) == (0, data, b""), (r.retcode, len(r.payload(FRAME_STDOUT)))
                r = serve(b, b"cat >&2", data)
                assert (r.payload(FRAME_STDOUT), r.payload(FRAME_STDERR)) == (b"", data)
                r = serve(b, b"cat", data, query={"merge": "1"})
                assert r.payload(FRAME_STDOUT) == data
            #: Raw bytes in the command itself, and /dev/null stdin.
            assert serve(b, b"print -rn -- $'\\0'\xff\xfe").payload(FRAME_STDOUT) == b"\0\xff\xfe"
            probe = b"[[ -c /dev/stdin ]] && print -rn dev || print -rn other"
            assert serve(b, probe, x_brish_stdin="null").payload(FRAME_STDOUT) == b"dev"
        finally:
            b.cleanup()
        """,
        timeout=120,
    )


@legacy_only
def test_stream_legacy_mode():
    run_handle(
        r"""
        b = garden_brish(server_count=1)
        sentinel = os.path.join(os.getcwd(), "sentinel")
        try:
            assert not b.binary
            for cmd, stdin, needle in [
                (b"print -rn -- ran >> sentinel; cat", b"a\0b", b"stdin contains a NUL byte"),
                (b"print -rn -- ran >> sentinel # \0", b"", b"the command contains a NUL byte"),
                (b"print -rn -- ran >> sentinel # \xff", b"", b"not valid utf-8"),
            ]:
                r = serve(b, cmd, stdin)
                assert (r.retcode, r.headers["x-brish-binary"], r.headers["x-brish-refused"]) == (9000, "0", "1"), r
                assert needle in r.payload(FRAME_STDERR), r
            assert not os.path.exists(sentinel)
            r = serve(b, b"cat", x_brish_stdin="null")
            assert (r.retcode, r.payload(FRAME_STDOUT)) == (0, b""), r
            r = serve(b, b"print -r -- caf\xc3\xa9")
            assert r.payload(FRAME_STDOUT) == "café\n".encode(), r
        finally:
            b.cleanup()
        """
    )
