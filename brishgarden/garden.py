import logging, os, time, brish
import traceback
import re
from typing import Optional

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


class Settings(BaseSettings):
    # disabling the docs
    openapi_url: str = ""  # "/openapi.json"


settings = Settings()

app = FastAPI(openapi_url=settings.openapi_url)
logger = logging.getLogger("uvicorn")  # alt: from uvicorn.config import logger


class EndpointFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # print(record.__dict__)

            # msg: str = record.getMessage()
            # return msg.find("/zsh/nolog/") == -1
            return record.scope.get('path', '') != '/zsh/nolog/'
        except:
            logger.warn(traceback.format_exc())
            return True


logging.getLogger("uvicorn.access").addFilter(EndpointFilter())
# Our usage of internal zsh APIs will fail gracefully on foreign systems.
seenIPs = {"127.0.0.1", brish.z("myip").out.strip()}


def newBrish():
    return brish.Brish(
        boot_cmd="export GARDEN_ZSH=y ; mkdir -p ~/tmp/garden/ ; cd ~/tmp/garden/ "
    )


logger.info("Initializing brishes ...")
brishes = [newBrish() for i in range(4)]
allBrishes = {idx: i for idx, i in enumerate(brishes)}


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
        brish.z("tsend -- {os.environ.get('tlogs')} 'New IP seen by the Garden: '{ip}")
        seenIPs.add(ip)

    nolog = ip == "127.0.0.1" and bool(
        body.get("nolog", "")
    )  # Use /zsh/nolog/ to hide the access logs.
    session = body.get("session", "")
    cmd = body.get("cmd", "")
    stdin = body.get("stdin", "")
    verbose = int(body.get("verbose", 0))

    log = f"{ip} - cmd: {cmd}, session: {session}, stdin: {stdin[0:100]}, brishes: {len(brishes)}, allBrishes: {len(allBrishes)}"
    nolog or logger.info(log)
    first_seen and brish.z("tsend -- {os.environ.get('tlogs')} {log}")

    if cmd == "":
        return Response(content="Empty command received.", media_type="text/plain")
    magic_matches = pattern_magic.match(cmd)
    if magic_matches is not None:
        magic_head = magic_matches.group(1)
        magic_exp = magic_matches.group(2)
        log = f"Magic received: {magic_head}"
        logger.info(log)
        if magic_head == "ALL":
            for b in allBrishes.values():
                res = b.send_cmd(magic_exp, fork=False, cmd_stdin=stdin)
                logger.info(res.longstr)
                if verbose == 1:
                    log += f"\n{res.longstr}"
        else:
            log += "\nUnknown magic!"
            logger.warn("Unknown magic!")

        return Response(content=log, media_type="text/plain")

    if session:
        # @design garbage collect
        myBrish = allBrishes.get(session, None)
        if not myBrish:
            myBrish = allBrishes.setdefault(
                session, newBrish()
            )  # is atomic https://bugs.python.org/issue13521#:~:text=setdefault()%20was%20intended%20to,()%20which%20can%20call%20arbitrary
    else:
        while len(brishes) <= 0:
            time.sleep(1)
        myBrish = brishes.pop()
    ##
    if verbose == 0:
        res = myBrish.z("{{ eval {cmd} }} 2>&1", fork=False, cmd_stdin=stdin)
    else:
        res = myBrish.send_cmd(cmd, fork=False, cmd_stdin=stdin)
    ##
    session or brishes.append(myBrish)
    if verbose == 0:
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
