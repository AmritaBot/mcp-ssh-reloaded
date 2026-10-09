"""read_file must stay UTF-8 safe, report one truncation notice, and resume."""

import asyncio
import os
from unittest.mock import MagicMock

import pytest

from mcp_ssh_reloaded import ConnectionParams, ServerConfig
from mcp_ssh_reloaded.file_manager import _trim_partial_char
from mcp_ssh_reloaded.services import SSHService
from mcp_ssh_reloaded.session_diagnostics import (
    SessionDiagnosticsProvider,
    _summarize_command,
)


def test_trim_partial_char_drops_incomplete_tail():
    raw = "跳跃的狐狸".encode()  # 3 bytes per character
    cut = raw[:8]  # two whole characters plus two bytes of the third
    trimmed = _trim_partial_char(cut, "utf-8", "replace")
    assert trimmed == raw[:6]
    assert trimmed.decode("utf-8") == "跳跃"


def test_trim_partial_char_keeps_complete_data():
    raw = b"abc"
    assert _trim_partial_char(raw, "utf-8", "replace") == raw


def test_summarize_command_collapses_heredoc():
    heredoc = "cat <<'EOF'\nline one\nline two\nline three\nEOF"
    summary = _summarize_command(heredoc)
    assert "\n" not in summary
    assert len(summary) <= 80
    assert summary.startswith("cat <<'EOF' line one")


def test_summarize_command_truncates_long_input():
    summary = _summarize_command("x" * 500)
    assert len(summary) == 80
    assert summary.endswith("\u2026")


def test_recent_commands_are_summarized():
    manager = MagicMock()
    manager.command_executor.list_command_history.return_value = [
        {"session_key": "u@h:22", "command": "cat <<'EOF'\nsecret line\nEOF"}
    ]
    provider = SessionDiagnosticsProvider(manager)
    recent = provider._get_recent_commands("u@h:22", limit=10)
    assert len(recent) == 1
    assert "\n" not in recent[0]


requires_ssh = pytest.mark.skipif(
    not os.environ.get("SSH_TEST_HOST"),
    reason="Skipping integration test: SSH_TEST_HOST not set",
)


def _conn() -> ConnectionParams:
    return ConnectionParams(
        host=os.environ["SSH_TEST_HOST"],
        port=int(os.environ.get("SSH_TEST_PORT", "22")),
        username=os.environ.get("SSH_TEST_USER"),
        password=os.environ.get("SSH_TEST_PASSWORD"),
        key_filename=os.environ.get("SSH_TEST_KEY_FILE"),
    )


@requires_ssh
def test_read_file_window_is_utf8_safe_and_resumable():
    service = SSHService(config=ServerConfig(max_file_bytes=200000))
    conn = _conn()
    lines = [f"跳跃的狐狸跳来跳去 {i}" for i in range(1, 21)]
    payload = "\n".join(lines) + "\n"
    remote = "/tmp/mcp_ssh_read_window_test.txt"

    async def run():
        try:
            await service.write_file(conn, remote, payload)
            first = await service.read_file(conn, remote, max_bytes=60)
            assert first.next_start_line is not None
            second = await service.read_file(
                conn, remote, start_line=first.next_start_line, max_lines=5
            )
            whole = await service.read_file(conn, remote, max_lines=100)
            return first, second, whole
        finally:
            await service.execute(conn, f"rm -f {remote}")
            await service.close_all()

    first, second, whole = asyncio.run(run())

    assert first.truncated is True
    assert "\ufffd" not in first.content
    assert "CONTENT TRUNCATED" not in first.content
    assert first.next_start_line == first.end_line + 1
    assert second.start_line == first.next_start_line
    assert "\ufffd" not in second.content
    assert whole.truncated is False
    assert whole.total_lines == len(lines)
    assert whole.content == payload


@requires_ssh
def test_read_file_tool_renders_one_truncation_notice():
    from mcp_ssh_reloaded import server

    conn = _conn()
    remote = "/tmp/mcp_ssh_read_window_tool_test.txt"
    payload = "跳跃的狐狸跳来跳去\n" * 40

    async def run():
        service = SSHService(config=ServerConfig(max_file_bytes=200000))
        try:
            await service.write_file(conn, remote, payload)
            return await server.read_file(
                host=conn.host,
                remote_path=remote,
                username=conn.username,
                password=conn.password,
                port=conn.port,
                max_bytes=60,
            )
        finally:
            await service.execute(conn, f"rm -f {remote}")
            await service.close_all()

    rendered = asyncio.run(run())

    assert rendered.count("[CONTENT TRUNCATED") == 1
    assert "\ufffd" not in rendered
    assert "continue with start_line=" in rendered
