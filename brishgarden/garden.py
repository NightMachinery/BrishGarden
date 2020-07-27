from typing import Optional

from fastapi import FastAPI, Response, Request
from pydantic import BaseSettings


class Settings(BaseSettings):
    # disabling the docs
    openapi_url: str = "" #"/openapi.json"


settings = Settings()

app = FastAPI(openapi_url=settings.openapi_url)

import time
import brish
brishes = [brish.Brish() for i in range(4)]
for b in brishes:
    b.z('export GARDEN_ZSH=y')

@app.get("/")
def read_root():
    return {"Hello": "World"}

@app.post("/test/")
def test(body: dict):
    return body

@app.get("/ip/")
async def get_ip(request: Request):
    return str(request.client)

@app.post("/zsh/")
def cmd_zsh(body: dict):
    # GET Method: cmd: str, verbose: Optional[int] = 0
    # body: cmd [verbose: int=0,1] [stdin: str]
    cmd = body.get('cmd', '')
    if cmd == '':
        return Response(content="Empty command received.", media_type="text/plain")
    stdin = body.get('stdin', '')
    verbose = int(body.get('verbose', 0))
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
