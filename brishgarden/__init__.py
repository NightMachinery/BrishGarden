import sys
import os
import uvicorn
from pynight.common_uvicorn import logging_config_setup
from uvicorn.config import LOGGING_CONFIG

try:
    from IPython import embed
except:
    pass


def main():
    root_path = ""
    if len(sys.argv) >= 2:
        root_path = sys.argv[1]

    logging_config_setup(LOGGING_CONFIG)

    uvicorn.run(
        "brishgarden.garden:app",
        host="127.0.0.1",
        port=7230,
        log_level="info",
        proxy_headers=True,
        root_path=root_path,
        # limit_concurrency=(int(os.environ.get("BRISHGARDEN_N", 0)) + 32),
    )
