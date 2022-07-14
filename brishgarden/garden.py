import logging
import os
import time
import brish
from brish import CmdResult, z, zp, UninitializedBrishException, zn
from pynight.common_async import force_async, async_max_workers_set
from pynight.common_fastapi import FastAPISettings, EndpointLoggingFilter1, request_path_get, check_ip
from pynight.common_telegram import log_tlg

import traceback
import re
from typing import Optional
from collections.abc import Iterable

from fastapi import FastAPI, Response, Request

settings = FastAPISettings()
app = FastAPI(openapi_url=settings.openapi_url)

logger = logging.getLogger("uvicorn")  # alt: from uvicorn.config import logger

isDbg = os.environ.get(
    "BRISHGARDEN_DEBUGME", False
)  # we can't reuse 'DEBUGME' or it will pollute all the brishes
if isDbg:
    logger.info("Debug mode enabled")

skip_paths=("/zsh/nolog/", "/api/v1/zsh/nolog/")
logging.getLogger("uvicorn.access").addFilter(EndpointLoggingFilter1(isDbg=isDbg, logger=logger, skip_paths=skip_paths))
###
brishes_n_default = 16
try:
    brishes_n = int(os.environ.get("BRISHGARDEN_N", brishes_n_default))
except:
    brishes_n = brishes_n_default

executor = async_max_workers_set(brishes_n + 16)
###
def newBrish(session="", **kwargs):
    return brish.Brish(
        #: FORCE_INTERACTIVE is set by tmuxnewsh2
        boot_cmd="export GARDEN_ZSH=y ; export GARDEN_SESSION={session} ; unset FORCE_INTERACTIVE ; garden_root=~/tmp/garden/ ; mkdir -p $garden_root ; cd $garden_root ",
        **kwargs,
    )

brish_server = None
def brish_server_cleanup(brish_server):
    try:
        if brish_server:
            if isinstance(brish_server, Iterable):
                for b, _ in brish_server:
                    try:
                        b.cleanup()
                    except:
                        logger.error(traceback.format_exc())
            else:
                brish_server.cleanup()
    except:
        logger.error(traceback.format_exc())


def init_brishes(erase_sessions=True):
    global brish_server, brishes, allBrishes

    brishes = []  # helps avoid UninitializedBrishException
    if erase_sessions:
        if allBrishes:  # @noflycheck
            executor.submit(lambda: brish_server_cleanup(allBrishes.values()))
            # https://docs.python.org/3/library/concurrent.futures.html
    else:
        executor.submit(lambda: brish_server_cleanup(brish_server))

    brish_server = newBrish(server_count=brishes_n)
    brishes = [i for i in range(brishes_n)]
    new_brishes = {i: (brish_server, i) for i in range(brishes_n)}
    if erase_sessions:
        allBrishes = new_brishes
    else:
        allBrishes.update(new_brishes)


allBrishes = None
brish_server = None
logger.info(f"Initializing {brishes_n} brishes ...")
init_brishes()
zn("bell_awaysh=no bell-sc2-nav_online || true")

###
@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.post("/test/")
def test(body: dict):
    return body


@app.get("/request/")
async def get_req(request: Request):
    ans = "### Your Request:\n" + str(request.__dict__)
    return Response(content=ans, media_type="text/plain")


@app.get("/request/ip/")
async def get_ip(request: Request):
    ans = request.client.host
    return Response(content=ans, media_type="text/plain")
###
pattern_magic = re.compile(r"(?im)^%GARDEN_(\S+)\s+((?:.|\n)*)$") # @duplicateCode/86da52eced14bf6baa394f50a9601812

@app.post("/zsh/")
@app.post("/zsh/nolog/")
def cmd_zsh(body: dict, request: Request):
    try:
        # GET Method: cmd: str, verbose: Optional[int] = 0
        # body: cmd [verbose: int=0,1] [stdin: str]
        ##
        # print(body)
        # print(request.__dict__)
        ##
        ip, first_seen = check_ip(request, logger=logger)
        req_path = request_path_get(request)

        session = body.get("session", "")
        cmd = body.get("cmd", "")
        stdin = body.get("stdin", "")
        json_output = int(
            body.get("json_output", body.get("verbose", 0))
        )  # old API had this named 'verbose'
        ##
        nolog = (
            not isDbg and ip == "127.0.0.1" and
            (bool(body.get("nolog", "")) or (req_path in skip_paths))
        )  # Use /zsh/nolog/ to hide the access logs.

        log_level = int(body.get("log_level", 1))
        if isDbg:
            log_level = max(log_level, 100)

        failure_expected = bool(body.get("failure_expected", False))
        ##

        log = f"{ip} - cmd: {cmd}, session: {session}, stdin: {stdin[0:100]}, brishes: {len(brishes)}, allBrishes: {len(allBrishes)}"
        if failure_expected:
            log+=", failure_expected"

        nolog or logger.info(log)
        first_seen and log_tlg(log)

        if cmd == "":
            return Response(content="Empty command received.", media_type="text/plain")
        magic_matches = pattern_magic.match(cmd)
        if magic_matches is not None:
            magic_head = magic_matches.group(1)
            magic_exp = magic_matches.group(2)
            log = f"Magic received: {magic_head}"
            logger.info(log)
            if magic_head == "ALL":
                init_brishes()
            else:
                log += "\nUnknown magic!"
                logger.warning("Unknown magic!")

            return Response(content=log, media_type="text/plain")

        while True:
            if session:
                # @design garbage collect
                myBrish, server_index = allBrishes.get(session, (None, None))
                if not myBrish:
                    myBrish, server_index = allBrishes.setdefault(
                        session, (newBrish(session=session, server_count=1), 0)
                    )  # is atomic https://bugs.python.org/issue13521#:~:text=setdefault()%20was%20intended%20to,()%20which%20can%20call%20arbitrary
            else:
                while len(brishes) <= 0:
                    time.sleep(1)
                myBrish = brish_server
                server_index = brishes.pop()
            ###
            res: CmdResult
            try:
                if json_output == 0:
                    # we need to output a single string, so we can't need to put stderr and stdout together
                    res = myBrish.z(
                        "{{ eval {cmd} }} 2>&1",
                        fork=False,
                        cmd_stdin=stdin,
                        server_index=server_index,
                    )
                else:
                    res = myBrish.send_cmd(
                        cmd, fork=False, cmd_stdin=stdin, server_index=server_index
                    )
            except UninitializedBrishException:
                if log_level >= 2:
                    logger.info("Encountered UninitializedBrishException")

                time.sleep(1)
                continue
            except:
                res = CmdResult(9000, "", traceback.format_exc(), cmd, stdin)
                log_level = max(log_level, 101)

            if not session and not (server_index in brishes):
                # duplicate brishes might be added here because of race conditions, but as brishes have their own locking, this doesn't matter, as we garbage-collect the dups here
                brishes.append(server_index)

            break
        ###
        if not failure_expected and res.retcode != 0:
            if log_level >= 1:
                nolog or logger.warning(f"Command failed:\n{res.longstr}")
                if log_level >= 1:
                    zn(
                        """
                        if isMe && isLocal ; then
                           {{ tts-glados1-cached "A command has failed." ; bello }} &>/dev/null </dev/null &
                        fi
                        """
                    )

        if json_output == 0:
            return Response(content=res.outerr, media_type="text/plain")
        else:
            return {
                "cmd": cmd,
                "session": session,
                "brishes": len(brishes),
                "allBrishes": len(allBrishes),
                "out": res.out,
                "err": res.err,
                "retcode": res.retcode,
            }
    except:
        logger.warning(traceback.format_exc())


## Old security scheme: (We now use Caddy's HTTP auth.)
# Use `pass: str`, hash it a lot along BRISHGARDEN_SALT, and compare to BRISHGARDEN_PASS. Abort if any of the two vars are empty. We probably need to answer the query right away for this security model to work, because hashing necessarily needs to be expensive.
