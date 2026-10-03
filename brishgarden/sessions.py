"""The garden's sessions: one Brish per session name, with one worker, which
runs one request at a time.

Every request on a session holds that Brish's *session lock* while it runs,
on every route. The lock is the garden's own, one `threading.Lock` per
Brish instance, so the Brishes that `%GARDEN_ALL` makes get fresh ones. A
request that finds its session busy waits for this lock, in the garden,
where a streamed request can give up when its client goes away (see
`session_take`), and not inside Brish, which waits for its worker's lock
until it gets it. While a request holds the session lock, the worker's own
lock is free, so Brish takes it at once. A worker that died (after an
`exit`, or a kill that reached its SIGKILL) is replaced inside that call
(Brish 0.4.1 and later; an older Brish restarts the session's instance
there, which waits for no other lock, since none is held). A streamed
request also passes `cancelled=` to popen (see `stream_run`), so it can
give up during that wait too.

Nothing here imports the garden, so it can be tested without starting its
workers.
"""

import threading
import weakref

#: How often a request that waits for a busy session checks whether it is
#: still wanted (seconds).
SESSION_POLL = 0.1

_locks = weakref.WeakKeyDictionary()
_locks_guard = threading.Lock()


def session_lock(brish):
    """The session lock of the Brish `brish`, made on first use."""
    with _locks_guard:
        lock = _locks.get(brish)
        if lock is None:
            lock = _locks[brish] = threading.Lock()
        return lock


def lock_wait(lock, cancelled=None, poll=SESSION_POLL):
    """Take `lock`, and return True with it held.

    Without `cancelled`, block until it is free. With it, check
    `cancelled()` every `poll` seconds while waiting, and once more after
    taking the lock; as soon as it returns True, return False, without the
    lock.
    """
    if cancelled is None:
        lock.acquire()
        return True
    while not lock.acquire(timeout=poll):
        if cancelled():
            return False
    if cancelled():
        lock.release()
        return False
    return True


def session_take(sessions, session, make, cancelled=None, poll=SESSION_POLL):
    """Take the Brish of the session named `session` for one request.

    `sessions` maps session names to (brish, server_index); a session that
    is not there yet gets `(make(), 0)`, and `make()` boots its worker,
    which takes about a second. Then the request waits for the session lock
    (`lock_wait`, with `cancelled` and `poll`).

    Returns (brish, server_index, lock), with the lock held: the caller runs
    the request and then releases it. Returns None when `cancelled()` became
    True first, also while the worker booted; then nothing ran, and a new
    session stays in `sessions` for later requests.
    """
    entry = sessions.get(session)
    if entry is None:
        mine = make()
        #: setdefault is atomic: of two first requests, both boot a Brish,
        #: and both use the one stored first. The other one is never used:
        #: stop its worker, in the background, since that takes a moment.
        entry = sessions.setdefault(session, (mine, 0))
        if entry[0] is not mine:
            threading.Thread(
                target=mine.cleanup, name="brishgarden-session-discard", daemon=True
            ).start()
    brish, server_index = entry
    lock = session_lock(brish)
    if not lock_wait(lock, cancelled, poll):
        return None
    return brish, server_index, lock
