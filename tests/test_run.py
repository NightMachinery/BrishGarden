"""`brish_run` against real zsh workers, in a child process with a timeout."""

from tests.conftest import run_py


def test_plain_path_merges_stderr():
    run_py(
        r"""
        from brishgarden.reply import brish_run
        b = Brish(server_count=1)
        try:
            res = brish_run(b, "print -r -- out; print -r -- err >&2; print -r -- out2",
                            "", json_output=0, server_index=0)
            assert res.retcode == 0, res
            assert res.out == "out\nerr\nout2\n", res
            assert res.err == "", res
        finally:
            b.cleanup()
        """
    )


def test_json_path_keeps_streams_apart():
    run_py(
        r"""
        from brishgarden.reply import brish_run
        b = Brish(server_count=1)
        try:
            res = brish_run(b, "print -r -- out; print -r -- err >&2; return 3",
                            "", json_output=1, server_index=0)
            assert (res.retcode, res.out, res.err) == (3, "out\n", "err\n"), res
        finally:
            b.cleanup()
        """
    )


def test_cmd_and_stdin_arrive_as_sent():
    run_py(
        r"""
        from brishgarden.reply import brish_run
        b = Brish(server_count=1)
        cmd = "x='a  b' ; print -r -- \"$x\" '$HOME' ; cat"
        try:
            for json_output in (0, 1):
                res = brish_run(b, cmd, "line 1\nline 2\n", json_output=json_output, server_index=0)
                assert res.retcode == 0, res
                assert res.out == "a  b $HOME\nline 1\nline 2\n", res
        finally:
            b.cleanup()
        """
    )
