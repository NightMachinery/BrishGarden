import logging
import os
import time
import brish
from brish import CmdResult, z, zp, UninitializedBrishException, zn
from pynight.common_async import force_async, async_max_workers_set
from pynight.common_fastapi import FastAPISettings, EndpointLoggingFilter1, request_path_get, check_ip, api_key_dependency_make
from pynight.common_telegram import log_tlg

import traceback
import re
from typing import Optional
from collections.abc import Iterable

from fastapi import Depends, FastAPI, Response, Request
from starlette.concurrency import run_in_threadpool

from brishgarden.mode import garden_mode_get
from brishgarden.reply import (
    RETCODE_GARDEN_ERROR,
    ZshOutcome,
    brish_run,
    json_reply,
    raw_reply,
    raw_request_parse,
    request_parse,
)

settings = FastAPISettings()

logger = logging.getLogger("uvicorn")  # alt: from uvicorn.config import logger

#: Every endpoint needs `X-API-Key: $(cat ~/.keys/brishgarden)`; the file is
#: generated here on first boot. Loopback binding does not keep out other local
#: users, and this endpoint runs arbitrary zsh.
app = FastAPI(
    openapi_url=settings.openapi_url,
    dependencies=[Depends(api_key_dependency_make("brishgarden", logger=logger))],
)

isDbg = os.environ.get(
    "BRISHGARDEN_DEBUGME", False
)  # we can't reuse 'DEBUGME' or it will pollute all the brishes
if isDbg:
    logger.info("Debug mode enabled")

class PathSet(tuple):
    """Paths, matched without any query string: uvicorn's access log line
    carries the query string, and a raw API request often has one."""

    def __contains__(self, path):
        return tuple.__contains__(self, str(path).partition("?")[0])


skip_paths = PathSet(
    ("/zsh/nolog/", "/api/v1/zsh/nolog/", "/zsh/raw/nolog/", "/api/v1/zsh/raw/nolog/")
)
logging.getLogger("uvicorn.access").addFilter(EndpointLoggingFilter1(isDbg=isDbg, logger=logger, skip_paths=skip_paths))
###
brishes_n_default = 16
try:
    brishes_n = int(os.environ.get("BRISHGARDEN_N", brishes_n_default))
except:
    brishes_n = brishes_n_default

executor = async_max_workers_set(brishes_n + 16)
###
#: Binary mode by default; `BRISH_BINARY=0` selects legacy mode. Every Brish
#: the garden creates gets it as `binary=`, and the garden never writes
#: BRISH_BINARY into its environment, which its commands inherit. See
#: brishgarden/mode.py.
garden_mode = garden_mode_get(os.environ, brish.Brish)
if garden_mode.note:
    logger.warning(garden_mode.note)


def newBrish(session="", **kwargs):
    return brish.Brish(
        #: FORCE_INTERACTIVE is set by tmuxnewsh2
        boot_cmd="export GARDEN_ZSH=y ; export GARDEN_SESSION={session} ; unset FORCE_INTERACTIVE ; garden_root=~/tmp/garden/ ; mkdir -p $garden_root ; cd $garden_root ",
        **garden_mode.brish_kwargs,
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


def garden_binary_p():
    """Whether the garden's Brish runs in binary mode (`garden_mode`)."""
    return bool(getattr(brish_server, "binary", False))


allBrishes = None
brish_server = None
logger.info(f"Initializing {brishes_n} brishes in {garden_mode.name} mode ...")
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

class ZshContext:
    """How the garden logs and reports one request: `nolog`, `log_level`
    and `failure_expected`, from the request and the garden's settings."""

    def __init__(self, nolog, log_level, failure_expected):
        self.nolog = nolog
        self.log_level = log_level
        self.failure_expected = failure_expected


def zsh_prepare(request: Request, decode):
    """Log a request and settle it when it runs nothing: the first step of
    every `/zsh/` route.

    `decode()` gives the `ZshRequest`. It is called after `check_ip`, so a new
    IP is noticed even when its request is malformed. Returns the request,
    its `ZshContext` and, for a request that runs nothing (a refusal, an
    empty command, a magic command), its `ZshOutcome`; else None, and
    `zsh_run` runs it.
    """
    ip, first_seen = check_ip(request, logger=logger)
    req_path = request_path_get(request)

    req = decode()
    cmd = req.cmd_display  #: surrogate-free; `req.cmd` is what runs
    session = req.session
    ##
    nolog = (
        not isDbg and ip == "127.0.0.1" and
        (req.nolog or (req_path in skip_paths))
    )  # Use /zsh/nolog/ to hide the access logs.

    log_level = req.log_level
    if isDbg:
        log_level = max(log_level, 100)

    failure_expected = req.failure_expected
    ctx = ZshContext(nolog, log_level, failure_expected)
    ##

    log = f"{ip} - cmd: {cmd}, session: {session}, stdin: {req.stdin_display}, brishes: {len(brishes)}, allBrishes: {len(allBrishes)}"
    if failure_expected:
        log+=", failure_expected"
    if req.binary_requested:
        log+=", binary"
    if req.raw:
        log+=", raw"

    nolog or logger.info(log)
    first_seen and log_tlg(log)

    if req.error is not None:
        #: A malformed request, or a binary request to a legacy-mode
        #: garden: reply with the error, without running anything.
        res = CmdResult(RETCODE_GARDEN_ERROR, "", req.error, cmd, req.stdin_display)
        if not failure_expected and log_level >= 1:
            nolog or logger.warning(f"Request refused:\n{res.longstr}")

        return req, ctx, ZshOutcome(res=res, refused=True)

    if cmd == "":
        return req, ctx, ZshOutcome(notice="Empty command received.")
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

        return req, ctx, ZshOutcome(notice=log)

    return req, ctx, None


def zsh_run(req, ctx, run):
    """Run the request `req` on a worker: its session's Brish, or one taken
    from the shared pool and given back afterwards. `run(brish,
    server_index)` runs it and returns its CmdResult. An exception from it
    gives retcode 9000 with the traceback as stderr; an
    UninitializedBrishException (raised before anything ran) is retried.
    """
    while True:
        if req.session:
            session = req.session
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
            try:
                server_index = brishes.pop()
            except IndexError:
                #: Another thread took the last free worker meanwhile.
                continue
        ###
        res: CmdResult
        try:
            res = run(myBrish, server_index)
        except UninitializedBrishException:
            if ctx.log_level >= 2:
                logger.info("Encountered UninitializedBrishException")

            time.sleep(1)
            continue
        except:
            res = CmdResult(RETCODE_GARDEN_ERROR, "", traceback.format_exc(), req.cmd, req.stdin)
            ctx.log_level = max(ctx.log_level, 101)

        if not req.session and not (server_index in brishes):
            # duplicate brishes might be added here because of race conditions, but as brishes have their own locking, this doesn't matter, as we garbage-collect the dups here
            brishes.append(server_index)

        return res


def zsh_finish(res, ctx):
    """Log a command's failure and play the failure sound, after it ended
    with the CmdResult `res`: the last step of every `/zsh/` route."""
    if not ctx.failure_expected and res.retcode != 0:
        if ctx.log_level >= 1:
            ctx.nolog or logger.warning(f"Command failed:\n{res.longstr}")
            if ctx.log_level >= 1:
                zn(
                    """
                    if isMe && isLocal ; then
                       {{ tts-glados1-cached "A command has failed." ; bello }} &>/dev/null </dev/null &
                    fi
                    """
                )


def zsh_handle(request: Request, decode):
    """Log and run one request: the shared code path of every `/zsh/` route.

    `decode()` gives the `ZshRequest` (see `zsh_prepare`). Returns the
    request and its `ZshOutcome`, which each route turns into its own reply.
    """
    req, ctx, outcome = zsh_prepare(request, decode)
    if outcome is not None:
        return req, outcome

    res = zsh_run(
        req,
        ctx,
        lambda brish, server_index: brish_run(
            brish, req.cmd, req.stdin, merge=req.merge, server_index=server_index
        ),
    )
    zsh_finish(res, ctx)
    return req, ZshOutcome(res=res)


@app.post("/zsh/")
@app.post("/zsh/nolog/")
def cmd_zsh(body: dict, request: Request):
    try:
        # GET Method: cmd: str, verbose: Optional[int] = 0
        # body: cmd [verbose: int=0,1] [stdin: str]
        # binary transport: [cmd_b64: str] [stdin_b64: str] [binary: int=0,1]; see brishgarden/reply.py
        ##
        # print(body)
        # print(request.__dict__)
        ##
        req, outcome = zsh_handle(
            request,
            lambda: request_parse(
                body,
                binary_mode=garden_binary_p(),
                encoding=getattr(brish_server, "encoding", "utf-8"),
            ),
        )
        return json_reply(
            outcome, req, brishes=len(brishes), all_brishes=len(allBrishes)
        )
    except:
        logger.warning(traceback.format_exc())


def raw_decode(request: Request, body: bytes, binary):
    """The raw API's request decoder."""
    return lambda: raw_request_parse(
        body,
        request.headers,
        request.query_params,
        binary_mode=binary,
        encoding=getattr(brish_server, "encoding", "utf-8"),
    )


def cmd_zsh_raw_sync(request: Request, body: bytes):
    binary = garden_binary_p()
    try:
        req, outcome = zsh_handle(request, raw_decode(request, body, binary))
        return raw_reply(outcome, req.binary_reply)
    except:
        tb = traceback.format_exc()
        logger.warning(tb)
        return raw_reply(
            ZshOutcome(res=CmdResult(RETCODE_GARDEN_ERROR, "", tb, "", "")), binary
        )


@app.post("/zsh/raw/")
@app.post("/zsh/raw/nolog/")
async def cmd_zsh_raw(request: Request):
    """The raw API: the body is the command's bytes, then its stdin's; see
    `raw_request_parse` and the readme. Reading the body needs an async
    endpoint, so the command runs in the thread pool, as a sync endpoint's
    would, and never blocks the event loop."""
    body = await request.body()
    return await run_in_threadpool(cmd_zsh_raw_sync, request, body)


## Security: every endpoint requires the `X-API-Key` header (see the app above).
# Remote clients authenticate to Caddy with HTTP basic auth instead; Caddy then
# injects this host's key upstream via `header_up`, so the garden itself only
# ever knows about the key. See `launchers/Caddyfile` in the scripts repo.
##
# Old security scheme, never implemented:
# Use `pass: str`, hash it a lot along BRISHGARDEN_SALT, and compare to BRISHGARDEN_PASS. Abort if any of the two vars are empty. We probably need to answer the query right away for this security model to work, because hashing necessarily needs to be expensive.
