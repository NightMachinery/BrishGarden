"""The `/zsh/` endpoint's command run and reply building.

`garden.py` starts its Brish workers when it is imported, so the logic that
decides what a reply looks like lives here, where it can be imported (and
tested) without side effects.
"""

from fastapi import Response


def brish_run(brish, cmd, stdin, *, json_output, server_index):
    """Run a request's command on worker `server_index` of `brish`.

    The plain reply path (`json_output == 0`) has a single body, so it merges
    stderr into stdout in the shell.
    """
    if json_output == 0:
        return brish.z(
            "{{ eval {cmd} }} 2>&1",
            locals_={"cmd": cmd},
            fork=False,
            cmd_stdin=stdin,
            server_index=server_index,
        )
    return brish.send_cmd(cmd, fork=False, cmd_stdin=stdin, server_index=server_index)


def notice_reply(text):
    """A `text/plain` notice that is not a command's output (an empty command,
    a magic command's log)."""
    return Response(content=text, media_type="text/plain")


def reply_build(res, json_output, *, cmd="", session="", brishes=0, all_brishes=0):
    """The reply to a request whose result is the CmdResult `res`.

    - `json_output == 0` (the plain reply path): the body is the text
      `res.outerr`, as `text/plain`.
    - Otherwise (the JSON reply path): a dict with the command echo, the pool
      sizes, `out`, `err` and `retcode`, which FastAPI serializes.
    """
    if json_output == 0:
        return Response(content=res.outerr, media_type="text/plain")

    return {
        "cmd": cmd,
        "session": session,
        "brishes": brishes,
        "allBrishes": all_brishes,
        "out": res.out,
        "err": res.err,
        "retcode": res.retcode,
    }
