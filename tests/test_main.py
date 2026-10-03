"""The garden's entry point: it listens on GARDEN_PORT (default 7230), and
refuses to start while something else still listens there. `main()` is
called in-process with uvicorn.run replaced, so no garden starts."""

import socket
import threading
import time

import pytest

import brishgarden


def listener():
    """A socket listening on a free port of 127.0.0.1."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def started(monkeypatch):
    """Records the uvicorn.run calls main() makes, instead of starting a garden."""
    calls = []
    monkeypatch.setattr(brishgarden.uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(brishgarden, "logging_config_setup", lambda config: None)
    monkeypatch.setattr(brishgarden.sys, "argv", ["brishgarden"])
    return calls


def test_a_free_port_is_free_at_once():
    t = time.monotonic()
    assert brishgarden.port_free_wait("127.0.0.1", free_port(), 5)
    assert time.monotonic() - t < 1


def test_a_listening_port_is_not_free():
    with listener() as s:
        t = time.monotonic()
        assert not brishgarden.port_free_wait("127.0.0.1", s.getsockname()[1], 0.3)
        assert 0.3 <= time.monotonic() - t < 2


def test_a_port_is_free_once_its_listener_closes():
    s = listener()
    port = s.getsockname()[1]
    threading.Timer(0.3, s.close).start()
    t = time.monotonic()
    assert brishgarden.port_free_wait("127.0.0.1", port, 5)
    assert 0.2 <= time.monotonic() - t < 2


def test_a_port_with_a_closed_connection_is_free():
    #: The connection the listener accepted ends in TIME_WAIT on one side;
    #: uvicorn can listen there anyway, so the wait must not refuse.
    s = listener()
    port = s.getsockname()[1]
    c = socket.create_connection(("127.0.0.1", port))
    a, _ = s.accept()
    a.close()
    c.close()
    s.close()
    assert brishgarden.port_free_wait("127.0.0.1", port, 1)


def test_main_listens_on_garden_port(monkeypatch, started):
    port = free_port()
    monkeypatch.setenv("GARDEN_PORT", str(port))
    assert brishgarden.main() is None
    [(args, kwargs)] = started
    assert (args, kwargs["host"], kwargs["port"]) == (("brishgarden.garden:app",), "127.0.0.1", port)


@pytest.mark.parametrize("value", [None, ""])
def test_main_defaults_to_7230(monkeypatch, started, value):
    if value is None:
        monkeypatch.delenv("GARDEN_PORT", raising=False)
    else:
        monkeypatch.setenv("GARDEN_PORT", value)
    #: Whatever listens on 7230 here (the live garden, say) must not matter.
    monkeypatch.setattr(brishgarden, "port_free_wait", lambda host, port, timeout: True)
    brishgarden.main()
    [(_, kwargs)] = started
    assert kwargs["port"] == 7230


def test_main_refuses_a_port_still_in_use(monkeypatch, started, capsys):
    monkeypatch.setattr(brishgarden, "PORT_WAIT", 0.3)
    with listener() as s:
        port = s.getsockname()[1]
        monkeypatch.setenv("GARDEN_PORT", str(port))
        assert brishgarden.main() == 1
    assert started == []
    assert f"127.0.0.1:{port} is still in use after 0.3 s" in capsys.readouterr().err
