"""The suite must test the trees it is pointed at, never an installed copy."""

from pathlib import Path

import pytest

from tests.conftest import BRISH_ROOT, BRISH_ROOT_VAR, ROOT, run_py


def test_brishgarden_is_this_tree():
    import brishgarden
    import brishgarden.reply

    for mod in (brishgarden, brishgarden.reply):
        assert Path(mod.__file__).resolve().is_relative_to(ROOT), mod.__file__


@pytest.mark.skipif(not BRISH_ROOT, reason=f"{BRISH_ROOT_VAR} is not set")
def test_brish_is_the_named_tree():
    import brish
    import brish.brishmod

    for mod in (brish, brish.brishmod):
        assert Path(mod.__file__).resolve().is_relative_to(Path(BRISH_ROOT).resolve()), mod.__file__


def test_guard_in_child():
    #: The child prelude asserts both before running the snippet.
    res = run_py("print(brish.__file__); print(brishgarden.__file__)")
    brish_file, garden_file = res.out.split()
    assert Path(garden_file).resolve().is_relative_to(ROOT)
    if BRISH_ROOT:
        assert Path(brish_file).resolve().is_relative_to(Path(BRISH_ROOT).resolve())


def test_garden_app_is_not_imported():
    #: Importing `brishgarden.garden` starts workers and runs startup commands.
    import sys

    assert "brishgarden.garden" not in sys.modules
