import logging, os, time, brish
import re
from typing import Optional

from fastapi import FastAPI, Response, Request
from pydantic import BaseSettings


class Settings(BaseSettings):
    # disabling the docs
    openapi_url: str = "" #"/openapi.json"


settings = Settings()

app = FastAPI(openapi_url=settings.openapi_url)
logger = logging.getLogger("uvicorn") # alt: from uvicorn.config import logger

# Our usage of internal zsh APIs will fail gracefully on foreign systems.
seenIPs = {'127.0.0.1', brish.z('myip').out.strip()}
# Brish.send_cmd is thread-safe
brishes = [brish.Brish() for i in range(4)]
allBrishes = brishes[:]
for b in brishes:
    b.z('export GARDEN_ZSH=y ; mkdir -p ~/tmp/garden/ ; cd ~/tmp/garden/ ')

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
    cmd = body.get('cmd', '')
    stdin = body.get('stdin', '')
    verbose = int(body.get('verbose', 0))

    log = f"{ip} - cmd: {cmd}, stdin: {stdin}, Brishes in standby: {len(brishes)}"
    logger.info(log)
    first_seen and brish.z("tsend -- {os.environ.get('tlogs')} {log}")

    if cmd == '':
        return Response(content="Empty command received.", media_type="text/plain")
    magic_matches = pattern_magic.match(cmd)
    if magic_matches is not None:
        magic_head = magic_matches.group(1)
        magic_exp = magic_matches.group(2)
        log = f"Magic received: {magic_head}"
        logger.info(log)
        if magic_head == 'ALL':
            for b in allBrishes:
                res = b.send_cmd(magic_exp, fork=False, cmd_stdin=stdin)
                logger.info(res.longstr)
                if verbose == 1:
                    log += f"\n{res.longstr}"
        else:
            log += "\nUnknown magic!"
            logger.warn("Unknown magic!")

        return Response(content=log, media_type="text/plain")

    while len(brishes) <= 0:
        time.sleep(1)
    myBrish = brishes.pop()
    res = myBrish.send_cmd(cmd, fork=False, cmd_stdin=stdin)
    brishes.append(myBrish)
    if verbose == 0:
        return Response(content=res.outerr, media_type="text/plain")
    else:
        return {"cmd": cmd, "brishes": len(brishes), "out": res.out, "err": res.err, "retcode": res.retcode}

# Old security scheme: (We now use Caddy's HTTP auth.)
# Use `pass: str`, hash it a lot along BRISHGARDEN_SALT, and compare to BRISHGARDEN_PASS. Abort if any of the two vars are empty. We probably need to answer the query right away for this security model to work, because hashing necessarily needs to be expensive.
