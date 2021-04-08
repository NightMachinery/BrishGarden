import logging, os, time
import functools
from concurrent.futures import ThreadPoolExecutor
import asyncio
import brish
from brish import CmdResult, z, zp


def zn(*a, getframe=3, **kw):
    # runs my personal commands
    if os.environ.get("NIGHTDIR"):
        return z(*a, **kw, getframe=getframe)
    else:
        return None


import traceback
import re
from typing import Optional
from collections.abc import Iterable

from fastapi import FastAPI, Response, Request
from pydantic import BaseSettings

##
# ix_flag = False


# def ix():
#     global ix_flag
#     if not ix_flag:
#         import nest_asyncio

#         nest_asyncio.apply()
#         ix_flag = True


def embed2():
    # from IPython import embed
    # ix()
    # embed(using='asyncio')
    # from ipydex import IPS, ip_syshook, ST, activate_ips_on_exception, dirsearch
    # IPS()
    print("None of these work at all with uvicorn's loop.")


##
def force_async(f):
    @functools.wraps(f)
    def inner(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(None, lambda: f(*args, **kwargs))

    return inner


##


class Settings(BaseSettings):
    # disabling the docs
    openapi_url: str = ""  # "/openapi.json"


settings = Settings()

app = FastAPI(openapi_url=settings.openapi_url)
logger = logging.getLogger("uvicorn")  # alt: from uvicorn.config import logger
isDbg = os.environ.get(
    "BRISHGARDEN_DEBUGME", False
)  # we can't reuse 'DEBUGME' or it will pollute all the brishes
if isDbg:
    logger.info("Debug mode enabled")


class EndpointFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isDbg:
                # logger.info(f"LogRecord:\n{record.__dict__}")
                return True
            ##
            # msg: str = record.getMessage()
            # return msg.find("/zsh/nolog/") == -1
            ##
            if hasattr(record, "scope"):
                return record.scope.get("path", "") != "/zsh/nolog/"
            else:
                return not (record.args[2] in ("/zsh/nolog/", "/api/v1/zsh/nolog/"))
        except:
            res = traceback.format_exc()
            try:
                res += f"\n\nLogRecord:\n{record.__dict__}"
                ##
                msg: str = record.getMessage()
                res += f"\n\nmsg:\n{msg}"
                res += f"\n{msg.__dict__}"
            except:
                pass

            logger.warn(res)
            return True


logging.getLogger("uvicorn.access").addFilter(EndpointFilter())
# Our usage of internal zsh APIs will fail gracefully on foreign systems.
myip = zn("myip")
seenIPs = {"127.0.0.1", myip.out.strip() if myip else ""}


def newBrish(**kwargs):
    return brish.Brish(
        # FORCE_INTERACTIVE is set by tmuxnewsh2
        boot_cmd="export GARDEN_ZSH=y ; unset FORCE_INTERACTIVE ; mkdir -p ~/tmp/garden/ ; cd ~/tmp/garden/ ",
        **kwargs,
    )


brishes_n_default = 16
try:
    brishes_n = int(os.environ.get("BRISHGARDEN_N", brishes_n_default))
except:
    brishes_n = brishes_n_default

loop = asyncio.get_running_loop()
executor = ThreadPoolExecutor(max_workers=(brishes_n + 16))
loop.set_default_executor(executor)

logger.info(f"Initializing {brishes_n} brishes ...")
brish_server = None


def brish_server_cleanup(brish_server):
    if brish_server:
        if isinstance(brish_server, Iterable):
            for b in brish_server:
                b.cleanup()
        else:
            brish_server.cleanup()


def init_brishes(erase_sessions=True):
    global brish_server, brishes, allBrishes

    if erase_sessions:
        if allBrishes:
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
init_brishes()
zn("bell-sc2-nav_online")


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


pattern_magic = re.compile(r"(?im)^%GARDEN_(\S+)\s+((?:.|\n)*)$")


@app.post("/zsh/")
@app.post("/zsh/nolog/")
def cmd_zsh(body: dict, request: Request):
    # GET Method: cmd: str, verbose: Optional[int] = 0
    # body: cmd [verbose: int=0,1] [stdin: str]
    first_seen = False
    ip = request.client.host
    if not (ip in seenIPs):
        first_seen = True
        logger.warn(f"New IP seen: {ip}")
        # We log the IP separately, to be sure that an injection attack can't stop the message.
        zn("tsend -- {os.environ.get('tlogs')} 'New IP seen by the Garden: '{ip}")
        seenIPs.add(ip)

    session = body.get("session", "")
    cmd = body.get("cmd", "")
    stdin = body.get("stdin", "")
    json_output = int(
        body.get("json_output", body.get("verbose", 0))
    )  # old API had this named 'verbose'
    ##
    nolog = (
        not isDbg and ip == "127.0.0.1" and bool(body.get("nolog", ""))
    )  # Use /zsh/nolog/ to hide the access logs.
    log_level = int(body.get("log_level", 1))
    if isDbg:
        log_level = max(log_level, 100)
    ##

    log = f"{ip} - cmd: {cmd}, session: {session}, stdin: {stdin[0:100]}, brishes: {len(brishes)}, allBrishes: {len(allBrishes)}"
    nolog or logger.info(log)
    first_seen and zn("tsend -- {os.environ.get('tlogs')} {log}")

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
            logger.warn("Unknown magic!")

        return Response(content=log, media_type="text/plain")

    if session:
        # @design garbage collect
        myBrish, server_index = allBrishes.get(session, (None, None))
        if not myBrish:
            myBrish, server_index = allBrishes.setdefault(
                session, (newBrish(server_count=1), 0)
            )  # is atomic https://bugs.python.org/issue13521#:~:text=setdefault()%20was%20intended%20to,()%20which%20can%20call%20arbitrary
    else:
        while len(brishes) <= 0:
            time.sleep(1)
        myBrish = brish_server
        server_index = brishes.pop()
    ##
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
    except:
        res = CmdResult(9000, "", traceback.format_exc(), cmd, stdin)
        log_level = max(log_level, 101)

    session or brishes.append(server_index)
    ##
    if res.retcode != 0:
        if log_level >= 1:
            nolog or logger.warn(f"Command failed:\n{res.longstr}")
            if log_level >= 1:
                zn(
                    """isLocal && {{ tts-glados1-cached "A command has failed." ; bello }} &>/dev/null </dev/null &"""
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


# Old security scheme: (We now use Caddy's HTTP auth.)
# Use `pass: str`, hash it a lot along BRISHGARDEN_SALT, and compare to BRISHGARDEN_PASS. Abort if any of the two vars are empty. We probably need to answer the query right away for this security model to work, because hashing necessarily needs to be expensive.
