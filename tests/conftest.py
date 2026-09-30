"""Test harness for BrishGarden.

The tests never import `brishgarden.garden`: importing it starts the garden's
Brish workers and runs its startup commands. They never use FastAPI's
TestClient either, whose client address is new to the garden and would make
it send a "new IP" notification. They test `brishgarden.reply`, which holds
the endpoint's request decoding, command run and reply building.

Tests that run real zsh workers do so in a child Python process through
`run_py`, which enforces a timeout. On timeout, or when the child leaves
processes behind, it kills them by explicit PID, found through
`ps -Ao pid,ppid,pgid` (each child runs in its own session).

The suite runs in two modes, selected by `BRISH_BINARY` exactly as brish
reads it. Run it twice:

    python -m pytest -q
    BRISH_BINARY=1 python -m pytest -q

To test an unreleased brish checkout, put it first on PYTHONPATH and name it
in BRISHGARDEN_TEST_BRISH_ROOT; the guard tests then check that it is the
brish being imported.
"""

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRISH_ROOT_VAR = "BRISHGARDEN_TEST_BRISH_ROOT"
BRISH_ROOT = os.environ.get(BRISH_ROOT_VAR) or None


def _bool_from_str(value):
    #: Same rule as `brish.brishmod.bool_from_str`, duplicated so that this
    #: module does not import brish before the guard tests run.
    if isinstance(value, str) and value.lower() in ("", "0", "n", "no", "false"):
        return False
    return bool(value)


BINARY = _bool_from_str(os.environ.get("BRISH_BINARY", ""))
MODE = "binary" if BINARY else "legacy"

binary_only = pytest.mark.skipif(not BINARY, reason="binary mode only")
legacy_only = pytest.mark.skipif(BINARY, reason="legacy mode only")

_SCRATCH = Path(tempfile.mkdtemp(prefix="brishgarden-tests-"))
EMPTY_ZDOTDIR = _SCRATCH / "zdotdir"
EMPTY_ZDOTDIR.mkdir()


def pytest_report_header(config):
    return (
        f"brish mode: {MODE} (BRISH_BINARY={os.environ.get('BRISH_BINARY', '')!r});"
        f" root: {ROOT}; {BRISH_ROOT_VAR}: {BRISH_ROOT!r}"
    )


def ps_rows():
    """[(pid, ppid, pgid)] for every process."""
    out = subprocess.run(
        ["ps", "-Ao", "pid=,ppid=,pgid="], capture_output=True, text=True
    ).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            rows.append(tuple(int(x) for x in parts))
    return rows


def descendants(root):
    children = {}
    for pid, ppid, _ in ps_rows():
        children.setdefault(ppid, []).append(pid)
    found, stack = [], [root]
    while stack:
        for kid in children.get(stack.pop(), []):
            found.append(kid)
            stack.append(kid)
    return found


def group_members(pgid):
    return [pid for pid, _, g in ps_rows() if g == pgid]


def kill_pids(pids, why):
    pids = [p for p in pids if p > 1 and p != os.getpid()]
    if not pids:
        return
    print(f"[conftest] {why}: SIGKILL {pids}", file=sys.stderr)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class ChildResult:
    def __init__(self, rc, out, err, orphans):
        self.rc = rc
        self.out = out
        self.err = err
        self.orphans = orphans

    def __repr__(self):
        return (
            f"ChildResult(rc={self.rc!r}, orphans={self.orphans})"
            f"\n--- stdout ---\n{self.out}\n--- stderr ---\n{self.err}"
        )


#: Prepended to every child snippet: the child checks that it imports the
#: trees under test before doing anything else.
PRELUDE = """\
import os, sys
from pathlib import Path
ROOT = Path({root!r})
BRISH_ROOT = {brish_root!r}
import brish, brishgarden
assert Path(brishgarden.__file__).resolve().is_relative_to(ROOT), brishgarden.__file__
if BRISH_ROOT:
    assert Path(brish.__file__).resolve().is_relative_to(Path(BRISH_ROOT).resolve()), brish.__file__
from brish import Brish, CmdResult
BINARY = {binary!r}
"""


def run_py(code, timeout=60):
    """Run `code` in a child python with a timeout; fail the test unless the
    child exits 0 without leaving processes behind. Returns a ChildResult.

    The child's zsh workers start with ZDOTDIR pointing at an empty
    directory, so they start fast and ignore the user's startup files.
    """
    scratch = tempfile.mkdtemp(prefix="child-", dir=_SCRATCH)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT)] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    )
    env["TMPDIR"] = scratch
    env["ZDOTDIR"] = str(EMPTY_ZDOTDIR)
    src = PRELUDE.format(root=str(ROOT), brish_root=BRISH_ROOT, binary=BINARY) + textwrap.dedent(code)
    p = subprocess.Popen(
        [sys.executable, "-c", src],
        env=env,
        cwd=scratch,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    rc = None
    try:
        out, err = p.communicate(timeout=timeout)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        rc = "TIMEOUT"
        tree = descendants(p.pid)
        group = [pid for pid in group_members(p.pid) if pid not in tree]
        kill_pids(list(reversed(tree)) + group + [p.pid], f"timeout after {timeout}s in child {p.pid}")
        out, err = p.communicate(timeout=10)
    #: Whatever is left in the child's process group outlived the child.
    deadline = time.time() + 3
    while True:
        orphans = group_members(p.pid)
        if not orphans or time.time() > deadline:
            break
        time.sleep(0.05)
    if orphans:
        kill_pids(orphans, f"orphans of child {p.pid}")
    res = ChildResult(
        rc, out.decode("utf-8", "backslashreplace"), err.decode("utf-8", "backslashreplace"), orphans
    )
    assert rc == 0 and not orphans, repr(res)
    return res


def pytest_sessionfinish(session, exitstatus):
    import shutil

    shutil.rmtree(_SCRATCH, ignore_errors=True)
