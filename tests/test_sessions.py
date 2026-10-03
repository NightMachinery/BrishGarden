"""The garden's sessions (`brishgarden/sessions.py`): the session lock, and
the wait for a busy or booting session that a streamed request can give up.
These tests need no workers; test_stream.py runs the same code with real
ones."""

import threading
import time

from brishgarden.sessions import lock_wait, session_lock, session_take
from brishgarden.stream import DaemonPool


class FakeBrish:
    pass


def test_session_lock_per_instance():
    a, b = FakeBrish(), FakeBrish()
    assert session_lock(a) is session_lock(a)
    assert session_lock(a) is not session_lock(b)


def test_lock_wait():
    lock = threading.Lock()
    assert lock_wait(lock) and lock.locked()
    lock.release()
    assert lock_wait(lock, lambda: False) and lock.locked()
    #: A busy lock: the wait gives up within one poll of the cancel.
    flag = threading.Event()
    threading.Timer(0.2, flag.set).start()
    t0 = time.monotonic()
    assert not lock_wait(lock, flag.is_set, poll=0.05)
    assert 0.15 < time.monotonic() - t0 < 0.5
    lock.release()
    #: Cancelled while the lock was taken: it is given back.
    assert not lock_wait(lock, lambda: True)
    assert not lock.locked()


def test_session_take_waits_for_a_busy_session():
    sessions = {"s": (FakeBrish(), 0)}
    brish = sessions["s"][0]
    taken = session_take(sessions, "s", None)
    assert taken == (brish, 0, session_lock(brish)) and session_lock(brish).locked()
    #: A second request waits until the first one releases the lock.
    got = {}

    def second():
        got["taken"] = session_take(sessions, "s", None, lambda: False, poll=0.02)

    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.2)
    assert "taken" not in got
    taken[2].release()
    t.join(5)
    assert got["taken"][0] is brish
    got["taken"][2].release()


def test_session_take_gives_up_while_a_new_session_boots():
    sessions = {}
    gone = threading.Event()
    made = []

    def make():
        #: The client leaves while the worker boots.
        gone.set()
        made.append(FakeBrish())
        return made[-1]

    assert session_take(sessions, "new", make, gone.is_set) is None
    #: The session exists now, idle, for the next request.
    assert sessions == {"new": (made[0], 0)} and not session_lock(made[0]).locked()
    taken = session_take(sessions, "new", make, lambda: False)
    assert taken[0] is made[0] and len(made) == 1
    taken[2].release()


def test_session_take_discards_the_brish_that_lost_the_race():
    class Discardable(FakeBrish):
        def __init__(self):
            self.cleaned = threading.Event()

        def cleanup(self):
            self.cleaned.set()

    sessions = {}
    first = Discardable()
    made = []

    def make():
        #: Another first request stores its Brish while this one boots.
        sessions["race"] = (first, 0)
        made.append(Discardable())
        return made[-1]

    taken = session_take(sessions, "race", make)
    assert taken[0] is first and sessions == {"race": (first, 0)}
    taken[2].release()
    assert made[0].cleaned.wait(5) and not first.cleaned.is_set()


def test_abandoned_session_waiters_free_their_threads():
    #: More streams wait for a busy session than the pool has owner threads;
    #: once their clients are gone, each gives its thread back within a
    #: poll, and a stream on the shared pool gets one at once.
    pool = DaemonPool(3, "test-sessions")
    sessions = {"busy": (FakeBrish(), 0)}
    holder = session_take(sessions, "busy", None)
    gone = threading.Event()
    results = []
    for _ in range(4):
        pool.submit(lambda: results.append(session_take(sessions, "busy", None, gone.is_set)))
    time.sleep(0.3)
    assert results == []
    gone.set()
    ran = threading.Event()
    t0 = time.monotonic()
    pool.submit(ran.set)
    assert ran.wait(5)
    assert time.monotonic() - t0 < 1, time.monotonic() - t0
    deadline = time.monotonic() + 5
    while len(results) < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert results == [None] * 4
    holder[2].release()
