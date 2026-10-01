"""Which mode the garden's Brish workers run in: binary or legacy.

The garden runs every Brish it creates (the shared pool, per-session
instances, and the pool that `%GARDEN_ALL` rebuilds) in one mode, and passes
it to each one as `binary=`. Binary mode is the default. The env var
`BRISH_BINARY` stays the switch, read once when the garden starts:

- unset or empty: binary mode;
- a false value as brish's `bool_from_str` parses it (`0`, `n`, `no`,
  `false`, in any case): legacy mode, the kill switch;
- any other value: binary mode.

This differs from brish's library default, where unset means legacy. The
garden never writes `BRISH_BINARY` into its own environment: its commands
inherit that environment, and a Python script started from the garden must
keep brish's library default.

A brish too old to accept `binary=` has no binary mode. The garden then runs
in legacy mode and says so in one log line instead of crashing.

Nothing here has side effects, so it can be imported and tested without
starting workers.
"""

import inspect
from dataclasses import dataclass, field
from typing import Optional

from brish import bool_from_str

ENV_VAR = "BRISH_BINARY"


def garden_binary_from_env(environ):
    """Whether `environ` (a mapping like `os.environ`) asks for binary mode.

    Unset or empty means binary; otherwise brish's `bool_from_str` decides,
    so `BRISH_BINARY=0` selects legacy mode.
    """
    value = environ.get(ENV_VAR, "")
    if value == "":
        return True
    return bool_from_str(value)


def brish_accepts_binary(brish_cls):
    """Whether `brish_cls(...)` takes a `binary` argument.

    Older brish releases take `**kwargs` and pass them on to `init()`, which
    raises TypeError for `binary=`, so only a named parameter counts.
    """
    try:
        params = inspect.signature(brish_cls.__init__).parameters
    except (TypeError, ValueError):
        return False
    return "binary" in params


@dataclass(frozen=True)
class GardenMode:
    """The mode the garden's Brish workers run in."""

    binary: bool
    #: Extra arguments for every `Brish(...)` the garden creates.
    brish_kwargs: dict = field(default_factory=dict)
    #: One line for the startup log when the mode is not what was asked for.
    note: Optional[str] = None

    @property
    def name(self):
        return "binary" if self.binary else "legacy"


def garden_mode_get(environ, brish_cls):
    """The `GardenMode` for env mapping `environ` and the Brish class `brish_cls`."""
    if not brish_accepts_binary(brish_cls):
        return GardenMode(
            binary=False,
            note=(
                "brishgarden: the installed brish is too old for binary mode"
                " (Brish() takes no binary argument), so the garden runs in"
                " legacy (text) mode; upgrade brish to serve binary requests."
            ),
        )
    binary = garden_binary_from_env(environ)
    return GardenMode(binary=binary, brish_kwargs={"binary": binary})
