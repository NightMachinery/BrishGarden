from typing import Optional

from fastapi import FastAPI, Response
app = FastAPI()


import time
import brish
brishes = [brish.Brish() for i in range(4)]


@app.get("/")
def read_root():
    return {"Hello": "World"}

@app.post("/test/")
def test(body: dict):
    return body

@app.post("/zsh/")
def cmd_zsh(body: dict):
    # GET Method: cmd: str, verbose: Optional[int] = 0
    cmd = body.get('cmd', '')
    if cmd == '':
        return Response(content="Empty command received.", media_type="text/plain")
    verbose = int(body.get('verbose', 0))
    while len(brishes) <= 0:
        time.sleep(1)
    myBrish = brishes.pop()
    res = myBrish.z(cmd)
    brishes.append(myBrish)
    if verbose == 0:
        return Response(content=res.outerr, media_type="text/plain")
    else:
        return {"cmd": cmd, "brishes": len(brishes), "out": res.out, "err": res.err, "retcode": res.retcode}

# Use `pass: str`, hash it a lot along BRISHGARDEN_SALT, and compare to BRISHGARDEN_PASS. Abort if any of the two vars are empty.
