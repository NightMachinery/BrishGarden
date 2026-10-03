"""The streaming API (`/zsh/stream/`): a command's output while it runs.

The request is exactly the raw API's (`raw_request_parse`, with
`stream=True`), and the garden logs and runs it through the same steps as
the other routes. Only the reply differs: HTTP 200, `application/octet-stream`,
sent chunked while the command runs. Its headers are decided before the
body: `X-Brish-Binary` (`1` or `0`), `X-Brish-Stream: 1`, and, for a
request that runs nothing, `X-Brish-Refused: 1` (a refusal, as on the raw
API) or `X-Brish-Notice: 1` (an empty command, a magic command).

The body is a sequence of frames. A frame is a 1-byte type, a 4-byte
big-endian unsigned length, and that many bytes of payload:

- `FRAME_STDOUT` (1): bytes the command wrote to stdout, as Brish read them;
- `FRAME_STDERR` (2): bytes it wrote to stderr, or a message of the garden;
- `FRAME_EXIT` (3): the command's retcode, in decimal ASCII.

Every complete reply ends with exactly one exit frame, and nothing follows
it; a body that ends without one was cut short. Output frames are never
empty. A client skips frames of any other type, which later gardens may add.

Threads. A BrishPopen is read in the thread that created it, and reading it
blocks, so each streamed command runs in a thread of its own (its owner,
from a `DaemonPool`), which puts frames on a bounded `FrameChannel`; the
event loop reads them in `StreamResponse` and sends them. A full channel
blocks the owner, so a slow client slows the command down (backpressure)
instead of growing the garden's memory. When the response ends before the
command does (the client disconnected, a send failed), the channel is
cancelled: that kills the command at once (`BrishPopen.kill`), and once
the kill's first signal is out, the owner drains it, which frees its
worker.

Nothing here imports the garden, so it can be tested without starting its
workers.
"""

import asyncio
import collections
import queue
import struct
import threading
import time
import traceback

from brish import CmdResult, UninitializedBrishException

try:
    from brish import BrishCancelledException

    #: Brish 0.4.1 and later: popen takes `cancelled=`, and a read after
    #: kill() waits for the kill's first signal by itself.
    POPEN_CANCELS = True
except ImportError:
    POPEN_CANCELS = False

    class BrishCancelledException(Exception):
        """Never raised: this Brish's popen takes no `cancelled=`."""
from starlette.responses import StreamingResponse

from brishgarden.reply import (
    BINARY_HEADER,
    NOTICE_HEADER,
    REFUSED_HEADER,
    RETCODE_GARDEN_ERROR,
    brish_cmd,
    text_safe,
)

#: Marks a reply of the streaming API.
STREAM_HEADER = "X-Brish-Stream"
MEDIA_TYPE = "application/octet-stream"

FRAME_STDOUT = 1
FRAME_STDERR = 2
FRAME_EXIT = 3
#: The type byte and the payload's length, big-endian.
FRAME_HEADER = struct.Struct(">BI")
FRAME_MAX = 0xFFFFFFFF

#: How many bytes of frames the owner thread may queue ahead of the event
#: loop before it blocks.
CHANNEL_BYTES = 256 << 10
#: How many bytes of queued frames the event loop sends at once at most.
SEND_BYTES = 256 << 10
#: The bytes of each stream that the garden's failure log shows: the last
#: ones, since a stream is never held whole.
LOG_TAIL_BYTES = 4 << 10


def frame(kind, payload):
    """One frame: the type byte, the payload's length, the payload."""
    payload = bytes(payload)
    if len(payload) > FRAME_MAX:
        raise ValueError(f"a frame's payload is at most {FRAME_MAX} bytes")
    return FRAME_HEADER.pack(kind, len(payload)) + payload


def exit_frame(retcode):
    """The frame that ends a reply: the retcode in decimal ASCII."""
    return frame(FRAME_EXIT, str(int(retcode)).encode("ascii"))


class FrameDecoder:
    """Decodes a reply body incrementally, however it is split: `feed`
    returns the frames that are complete so far, as (type, payload) pairs.
    The reference decoder for clients, and the tests'."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data):
        self._buf += data
        frames = []
        size = FRAME_HEADER.size
        while len(self._buf) >= size:
            kind, n = FRAME_HEADER.unpack_from(self._buf)
            if len(self._buf) < size + n:
                break
            frames.append((kind, bytes(self._buf[size : size + n])))
            del self._buf[: size + n]
        return frames

    @property
    def pending(self):
        """The bytes of a frame not yet complete."""
        return len(self._buf)


def stream_headers(binary, *, notice=False, refused=False):
    """The headers of a streamed reply. `binary` is whether the garden runs
    in binary mode, that is, whether the bytes are exact."""
    headers = {BINARY_HEADER: "1" if binary else "0", STREAM_HEADER: "1"}
    if notice:
        headers[NOTICE_HEADER] = "1"
    if refused:
        headers[REFUSED_HEADER] = "1"
    return headers


class StreamResult(CmdResult):
    """The result of a streamed command. Its output went out in frames
    while it ran; `out` and `err` hold only the last bytes of each stream,
    for the garden's failure log."""


def end_frames(res):
    """The frames that end a reply whose run gave the CmdResult `res`.

    A `StreamResult`'s output has gone out already, so only its exit frame
    is left. Any other result is the garden's own (a refusal, an exception
    before or while the command ran), and its output goes out first: stdout,
    then stderr, as the raw API orders them.
    """
    frames = []
    if not isinstance(res, StreamResult):
        if res.outb:
            frames.append(frame(FRAME_STDOUT, res.outb))
        if res.errb:
            frames.append(frame(FRAME_STDERR, res.errb))
    frames.append(exit_frame(res.retcode))
    return frames


def outcome_frames(outcome):
    """The whole body for a ZshOutcome that ran nothing: a notice (a stdout
    frame with its text, and retcode 0), or a refusal or the garden's error
    (its message in a stderr frame, and retcode 9000)."""
    if outcome.notice is not None:
        return frame(FRAME_STDOUT, text_safe(outcome.notice).encode("utf-8")) + exit_frame(0)
    return b"".join(end_frames(outcome.res))


async def _once(data):
    yield data


def outcome_reply(outcome, binary):
    """The streamed reply for a ZshOutcome that ran nothing (see
    `outcome_frames`), with `X-Brish-Notice: 1` or `X-Brish-Refused: 1`."""
    return StreamingResponse(
        _once(outcome_frames(outcome)),
        media_type=MEDIA_TYPE,
        headers=stream_headers(
            binary, notice=outcome.notice is not None, refused=outcome.refused
        ),
    )


class FrameChannel:
    """Frames from a stream's owner thread to the event loop.

    The owner thread `put`s frames and `close`s the channel after the last
    one. `put` blocks while `max_bytes` or more are queued, so a slow client
    slows the command down, and returns False once the client is gone. The
    event loop reads the frames with `frames()`, and calls `cancel()` when
    the client is gone, which kills the command if it still runs.

    The owner publishes its BrishPopen with `popen_set` once it runs, and
    kills it when that returns False; `cancel()` sets `gone` before it reads
    the published popen. So whichever comes first, a command whose client
    is gone is killed, and `killed` says so.
    """

    def __init__(self, loop, max_bytes=CHANNEL_BYTES):
        self._loop = loop
        self._max = max_bytes
        self._cond = threading.Condition()
        self._frames = collections.deque()
        self._bytes = 0
        self._closed = False  # the owner put its last frame
        self._gone = False  # the client is gone
        self._waiting = False  # the event loop waits for a frame
        self._wakeup = asyncio.Event()
        self._popen = None
        #: The command started (popen returned), so frames may have gone out.
        self.started = False
        #: The command was killed because its client went away: by cancel()
        #: while it ran, or by its owner when popen_set() came too late.
        self.killed = False

    @property
    def gone(self):
        """Whether the client is gone (`cancel()` was called)."""
        return self._gone

    @property
    def queued(self):
        """The bytes of frames that wait for the event loop."""
        return self._bytes

    # The owner thread

    def put(self, data):
        """Queue `data` (one or more frames); block while the queue is full.
        Returns False, dropping `data`, once the client is gone."""
        with self._cond:
            while self._bytes >= self._max and not self._gone:
                self._cond.wait()
            if self._gone:
                return False
            self._frames.append(data)
            self._bytes += len(data)
            wake, self._waiting = self._waiting, False
        if wake:
            self._wake()
        return True

    def close(self):
        """No more frames: the reply ends once the queued ones are sent."""
        with self._cond:
            self._closed = True
            wake, self._waiting = self._waiting, False
        if wake:
            self._wake()

    def popen_set(self, p):
        """Publish the running BrishPopen `p`. Returns False when the client
        is gone already; the caller then kills `p`, and `killed` is set, so
        the garden reports a disconnect and not a failure."""
        with self._cond:
            self._popen = p
            if self._gone:
                self.killed = True
                return False
            return True

    def _wake(self):
        try:
            self._loop.call_soon_threadsafe(self._wakeup.set)
        except RuntimeError:
            pass  # the loop is closed: nobody reads any more

    # The event loop

    async def frames(self, limit=SEND_BYTES):
        """The queued frames, as they come, joined up to `limit` bytes when
        several wait. Ends once the owner has closed the channel and every
        frame is out."""
        while True:
            self._wakeup.clear()
            batch = None
            with self._cond:
                if self._frames:
                    batch, n = [], 0
                    while self._frames and n < limit:
                        data = self._frames.popleft()
                        batch.append(data)
                        n += len(data)
                    self._bytes -= n
                    self._cond.notify_all()
                elif self._closed:
                    return
                else:
                    self._waiting = True
            if batch is None:
                await self._wakeup.wait()
            else:
                yield b"".join(batch)

    def cancel(self):
        """The client is gone: drop the queued frames, make `put` return
        False, and kill the command if it still runs. Idempotent; returns at
        once (BrishPopen.kill does)."""
        with self._cond:
            if self._gone:
                return
            self._gone = True
            self._frames.clear()
            self._bytes = 0
            self._cond.notify_all()
            p = self._popen
            kill = p is not None and not self._closed and p.retcode is None
            if kill:
                self.killed = True
        if kill:
            p.kill()


class _Tail:
    """The last `limit` bytes of a stream, for the failure log."""

    def __init__(self, limit):
        self.limit = limit
        self.data = bytearray()
        self.dropped = 0

    def add(self, chunk):
        self.data += chunk
        extra = len(self.data) - self.limit
        if extra > 0:
            del self.data[:extra]
            self.dropped += extra

    def value(self):
        if not self.dropped:
            return bytes(self.data)
        return f"[{self.dropped} earlier bytes not shown]\n".encode() + bytes(self.data)


def _signal_wait(p, poll=0.01):
    """After `p.kill()`, wait (at most `p.kill_grace` seconds) until its
    first signal is out, before reading the command's output to its end.

    `kill()` sends its first SIGINT from a helper thread, after one `ps` run
    (tens of milliseconds, a second or more on a loaded machine). Reading
    meanwhile would let a command that the client's backpressure held up
    write on at full speed: `head -c 20000000 /dev/zero` blocked on a full
    pipe then ran to its end, and a command after it in the same line ran
    too, before the signal came. Brish 0.4.1 and later wait for that signal
    themselves before such a read (`BrishPopen.wait_signalled`), so this
    returns at once there; with an older Brish it polls the private
    `_ahead`, which BrishPopen sets once the signal is out.
    """
    if hasattr(p, "wait_signalled"):
        return
    deadline = time.monotonic() + p.kill_grace
    while not getattr(p, "_ahead", True) and p.retcode is None and time.monotonic() < deadline:
        time.sleep(poll)


def stream_run(brish, server_index, req, channel, *, log_tail=LOG_TAIL_BYTES, cancelled=None):
    """Run the ZshRequest `req` on worker `server_index` of `brish` with
    `popen`, in this thread, which owns the BrishPopen, and put its output
    on `channel` as frames, chunk by chunk, as Brish yields it. Returns a
    `StreamResult`; the exit frame is the caller's (see `end_frames`).

    With `req.merge`, the command runs in the raw API's merging wrapper
    (`brish_cmd`), and every chunk becomes a stdout frame, so the client
    gets one stream in the order the garden read it: also what Brish reads
    from outside the wrapper's redirection (its note on a worker that died,
    the output of an earlier command's background job), which the raw API
    puts in its stderr part.

    When the client goes away, the command is killed (by `channel.cancel()`,
    or here when that came while `popen` started); once the kill's first
    signal is out (`_signal_wait`), leaving the `with` block reads the rest
    of its output, discarded, which frees the worker.

    `cancelled` (with Brish 0.4.1 and later) goes to `popen`, which checks
    it while it waits for the worker, also while Brish replaces a worker
    that died, and once more just before it sends the command; when it is
    True, nothing runs, and popen raises BrishCancelledException.

    Exceptions from `popen` itself come before anything ran (the garden
    retries an UninitializedBrishException, and takes a
    BrishCancelledException as a request that ran nothing). An exception after that is the
    garden's: it is raised after the command was killed and drained, and
    never as an UninitializedBrishException, so it is never retried.
    """
    cmd = brish_cmd(brish, req.cmd, req.merge)
    kwargs = {}
    if cancelled is not None and POPEN_CANCELS:
        kwargs["cancelled"] = cancelled
    p = brish.popen(cmd, cmd_stdin=req.stdin, fork=False, server_index=server_index, **kwargs)
    channel.started = True
    tails = {FRAME_STDOUT: _Tail(log_tail), FRAME_STDERR: _Tail(log_tail)}
    try:
        with p:
            if not channel.popen_set(p):
                p.kill()
                _signal_wait(p)
            for stream, chunk in p:
                kind = FRAME_STDOUT if req.merge or stream == "out" else FRAME_STDERR
                tails[kind].add(chunk)
                if not channel.put(frame(kind, chunk)):
                    #: The client is gone. Leaving the block drains the
                    #: command, once the kill's first signal is out.
                    p.kill()
                    _signal_wait(p)
                    break
    except UninitializedBrishException as e:
        raise RuntimeError(f"brishgarden: the stream failed after its command started: {e!r}") from e
    retcode = p.retcode
    if retcode is None:
        retcode = RETCODE_GARDEN_ERROR
    return StreamResult.from_bytes(
        retcode, tails[FRAME_STDOUT].value(), tails[FRAME_STDERR].value(), cmd, req.stdin
    )


def stream_own(channel, run):
    """The body of a stream's owner thread: `run()` runs the command (it
    returns its result, or None when the client went away before anything
    ran); then the end of the reply goes on `channel` (`end_frames`), and
    the channel is closed. An exception from `run()` ends the reply with
    its traceback in a stderr frame and retcode 9000, as on the raw API, and
    is raised again. Returns `run()`'s result."""
    try:
        res = run()
        if res is not None:
            for data in end_frames(res):
                channel.put(data)
        return res
    except BaseException:
        tb = traceback.format_exc()
        channel.put(frame(FRAME_STDERR, tb.encode("utf-8", "backslashreplace")))
        channel.put(exit_frame(RETCODE_GARDEN_ERROR))
        raise
    finally:
        channel.close()


def stream_report(channel, res, finish, info):
    """Report the end of a streamed command whose run on `channel` gave
    `res` (None: nothing ran).

    A command killed because its client went away (`channel.killed`) is not
    a failure: `info` gets one line with its retcode, and `finish` (the
    garden's failure log and sound) is not called. Neither is it when
    nothing ran. Any other result goes to `finish`, also when the client
    left after the command's end.
    """
    if res is None:
        info("Stream: the client went away before the command started; nothing ran.")
    elif channel.killed:
        info(f"Stream: the client went away, so the command was killed; retcode {res.retcode}.")
    else:
        finish(res)


class StreamResponse(StreamingResponse):
    """The reply of a streamed command, whose frames come from a
    `FrameChannel`.

    `start(channel)` starts the command's owner thread, and must return at
    once. It is called when the response starts, so a response that is
    never sent runs nothing. The headers go out first, then the frames as
    they come. However the response ends before the command's end (the
    client disconnected, a send failed, the task was cancelled), the channel
    is cancelled, which kills the command.
    """

    def __init__(self, start, headers, *, channel_bytes=CHANNEL_BYTES, send_bytes=SEND_BYTES):
        self._start = start
        self._channel_bytes = channel_bytes
        self._send_bytes = send_bytes
        self.channel = None
        super().__init__(self._body(), media_type=MEDIA_TYPE, headers=headers)

    async def _body(self):
        async for data in self.channel.frames(self._send_bytes):
            yield data

    async def __call__(self, scope, receive, send):
        self.channel = FrameChannel(asyncio.get_running_loop(), self._channel_bytes)
        try:
            self._start(self.channel)
            await super().__call__(scope, receive, send)
        finally:
            self.channel.cancel()


class DaemonPool:
    """Up to `size` daemon threads that run submitted jobs, first come first
    served; a job waits in a queue while every thread is busy.

    The owners of streamed commands run here. Daemon threads, so that one
    that still streams never holds up the garden's exit: the end of Python
    stops the commands (see "When a Worker Dies" in Brish's readme).
    """

    def __init__(self, size, name="stream", logger=None):
        self._size = size
        self._name = name
        self._logger = logger
        self._jobs = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._threads = 0
        self._idle = 0

    def submit(self, fn, *args):
        """Run `fn(*args)` in a thread of the pool; returns at once."""
        with self._lock:
            self._jobs.put((fn, args))
            if self._idle:
                self._idle -= 1  # an idle thread takes it
                return
            if self._threads >= self._size:
                return  # it waits for a busy thread
            self._threads += 1
            name = f"{self._name}-{self._threads}"
        threading.Thread(target=self._work, name=name, daemon=True).start()

    def _work(self):
        while True:
            fn, args = self._jobs.get()
            try:
                fn(*args)
            except BaseException:
                if self._logger is not None:
                    self._logger.warning(traceback.format_exc())
            with self._lock:
                self._idle += 1
