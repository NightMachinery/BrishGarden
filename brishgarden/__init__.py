import sys
import os
import socket
import time
import uvicorn
from pynight.common_uvicorn import logging_config_setup
from uvicorn.config import LOGGING_CONFIG

try:
    from IPython import embed
except:
    pass


#: How long a new garden waits for its port, which an old garden that is
#: still stopping may hold for a moment.
PORT_WAIT = 15


def port_free_wait(host, port, timeout):
    """Wait up to `timeout` seconds until `host:port` is free to listen on;
    True if it is.

    It binds as uvicorn does (with SO_REUSEADDR), so it fails exactly when
    uvicorn would: while a socket listens there, and not merely because of
    connections in TIME_WAIT.
    """
    deadline = time.monotonic() + timeout
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return True
            except OSError:
                pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def main():
    root_path = ""
    if len(sys.argv) >= 2:
        root_path = sys.argv[1]

    host = "127.0.0.1"
    #: The same variable, and default, as the clients use.
    port = int(os.environ.get("GARDEN_PORT") or 7230)
    #: With another garden still listening, uvicorn would fail only after
    #: this one had started all its workers, and run its startup commands;
    #: refuse before any of that.
    if not port_free_wait(host, port, PORT_WAIT):
        print(
            f"brishgarden: {host}:{port} is still in use after {PORT_WAIT} s, probably by another garden; not starting.",
            file=sys.stderr,
        )
        return 1

    logging_config_setup(LOGGING_CONFIG)

    uvicorn.run(
        "brishgarden.garden:app",
        host=host,
        port=port,
        log_level="info",
        proxy_headers=True,
        root_path=root_path,
        # limit_concurrency=(int(os.environ.get("BRISHGARDEN_N", 0)) + 32),
    )
