"""Regression tests for command-echo stripping."""

from mcp_ssh_reloaded.session_manager import SSHSessionManager


def _sm() -> SSHSessionManager:
    return SSHSessionManager.__new__(SSHSessionManager)


def test_strips_single_line_echo():
    sm = _sm()
    out = "ls -la /tmp\ntotal 8\n-rw-r--r-- 1 root root 0 a\n"
    assert (
        sm._strip_command_echo(out, "ls -la /tmp")
        == "total 8\n-rw-r--r-- 1 root root 0 a"
    )


def test_strips_echo_with_readline_redraw_doubling():
    sm = _sm()
    out = "git branch --showw-current\nmain\n"
    assert sm._strip_command_echo(out, "git branch --show-current") == "main"


def test_keeps_output_when_echo_does_not_match():
    sm = _sm()
    out = "some unrelated output\nmore\n"
    assert sm._strip_command_echo(out, "ls -la /tmp") == out


def test_strips_multiline_heredoc_echo():
    sm = _sm()
    command = "cat > f <<'EOF'\nhello\nEOF"
    out = "cat > f <<'EOF'\nhello\nEOF\n"
    assert sm._strip_command_echo(out, command) == ""


def test_empty_inputs_are_safe():
    sm = _sm()
    assert sm._strip_command_echo("", "ls") == ""
    assert sm._strip_command_echo("out", "") == "out"
