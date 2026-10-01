"""The garden's mode: binary by default, `BRISH_BINARY=0` for legacy, and
legacy with a log line when brish is too old for binary mode."""

import ast
import os
import types

import pytest

from brish import Brish, bool_from_str
from brishgarden.mode import (
    GardenMode,
    brish_accepts_binary,
    garden_binary_from_env,
    garden_mode_get,
)
from tests.conftest import BINARY, ROOT, garden_binary

FALSE_VALUES = ["0", "n", "no", "false", "N", "No", "FALSE"]
TRUE_VALUES = ["1", "y", "yes", "true", "on", "2", " 0"]


def env(value):
    """A read-only environment with BRISH_BINARY set to `value` (None: unset)."""
    return types.MappingProxyType({} if value is None else {"BRISH_BINARY": value})


class OldBrish:
    """The constructor of brish releases without binary mode: `binary=` lands
    in `**kwargs`, which `init()` rejects."""

    def __init__(self, defaultShell=None, boot_cmd=None, server_count=1, delayed_init=False, **kwargs):
        raise AssertionError("garden_mode_get must not construct a Brish")


@pytest.mark.parametrize("value", [None, ""])
def test_default_is_binary(value):
    assert garden_binary_from_env(env(value)) is True


@pytest.mark.parametrize("value", FALSE_VALUES)
def test_false_value_is_the_kill_switch(value):
    assert garden_binary_from_env(env(value)) is False


@pytest.mark.parametrize("value", TRUE_VALUES)
def test_true_value_is_binary(value):
    assert garden_binary_from_env(env(value)) is True


@pytest.mark.parametrize("value", FALSE_VALUES + TRUE_VALUES)
def test_set_values_parse_as_brish_does(value):
    #: Only unset and empty differ from brish's library default.
    assert garden_binary_from_env(env(value)) == bool_from_str(value)
    assert garden_binary(env(value)) == bool_from_str(value)
    assert garden_binary(env(None)) is garden_binary(env("")) is True


def test_harness_mode_is_the_garden_mode():
    assert BINARY == garden_binary_from_env(os.environ)


def test_mode_with_current_brish():
    assert brish_accepts_binary(Brish)
    assert garden_mode_get(env(None), Brish) == GardenMode(True, {"binary": True})
    assert garden_mode_get(env("0"), Brish) == GardenMode(False, {"binary": False})
    assert garden_mode_get(env("1"), Brish).name == "binary"
    assert garden_mode_get(env("no"), Brish).name == "legacy"


@pytest.mark.parametrize("value", [None, "1", "0"])
def test_mode_with_old_brish(value):
    #: No `binary=` is passed, so the old constructor does not raise, and
    #: the note says why the garden runs in legacy mode.
    assert not brish_accepts_binary(OldBrish)
    mode = garden_mode_get(env(value), OldBrish)
    assert (mode.binary, mode.brish_kwargs, mode.name) == (False, {}, "legacy")
    assert "too old for binary mode" in mode.note and "\n" not in mode.note


def test_uninspectable_brish_is_treated_as_old():
    class Uninspectable:
        __init__ = None

    assert not brish_accepts_binary(Uninspectable)
    assert garden_mode_get(env(None), Uninspectable).brish_kwargs == {}


###
#: `garden.py` cannot be imported here (it starts workers), so these read it.

GARDEN = ast.parse((ROOT / "brishgarden" / "garden.py").read_text())


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return None


def test_garden_passes_the_mode_to_every_brish():
    calls = [
        n for n in ast.walk(GARDEN)
        if isinstance(n, ast.Call) and _dotted(n.func) in ("Brish", "brish.Brish")
    ]
    assert calls, "garden.py creates its Brishes elsewhere; update this test"
    for call in calls:
        splats = [_dotted(k.value) for k in call.keywords if k.arg is None]
        assert "garden_mode.brish_kwargs" in splats, ast.dump(call)
        assert "binary" not in [k.arg for k in call.keywords]

    #: The pool, the sessions and the %GARDEN_ALL rebuild all use newBrish.
    news = [n for n in ast.walk(GARDEN) if isinstance(n, ast.Call) and _dotted(n.func) == "newBrish"]
    assert len(news) >= 2


def test_garden_never_writes_its_environment():
    #: Its commands inherit the garden's environment, so setting BRISH_BINARY
    #: there would flip brish's library default for scripts they start.
    for n in ast.walk(GARDEN):
        if isinstance(n, ast.Subscript) and isinstance(n.ctx, (ast.Store, ast.Del)):
            assert _dotted(n.value) != "os.environ", ast.dump(n)
        if isinstance(n, ast.Call):
            assert _dotted(n.func) not in (
                "os.putenv", "os.unsetenv", "os.environ.update",
                "os.environ.setdefault", "os.environ.pop",
            ), ast.dump(n)
