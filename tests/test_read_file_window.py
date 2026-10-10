"""read_file must stay UTF-8 safe, report one truncation notice, and resume."""

import asyncio
import base64
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_ssh_reloaded import ConnectionParams, ServerConfig
from mcp_ssh_reloaded.file_manager import (
    FileManager,
    _newline_is_single_byte,
    _trim_partial_char,
)
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
    assert _trim_partial_char(b"abc", "utf-8", "replace") == b"abc"


def test_newline_compatibility_detection():
    assert _newline_is_single_byte("utf-8")
    assert _newline_is_single_byte("latin-1")
    assert not _newline_is_single_byte("utf-16-le")
    assert not _newline_is_single_byte("utf-16")


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


#  sudo fallback (mocked executor)


def _sudo_manager(result):
    manager = MagicMock()
    manager.MAX_FILE_TRANSFER_SIZE = 2 * 1024 * 1024
    manager.execute_result = AsyncMock(return_value=result)
    return manager


def _sudo_read(manager, **overrides):
    kwargs = {
        "host": "h",
        "remote_path": "/root/secret",
        "username": None,
        "password": None,
        "key_filename": None,
        "port": None,
        "encoding": "utf-8",
        "errors": "replace",
        "byte_limit": 40,
        "start_line": 1,
        "offset": 0,
        "line_mode": True,
        "sudo_password": None,
        "timeout": 30,
    }
    kwargs.update(overrides)
    return asyncio.run(FileManager(manager)._read_via_sudo(**kwargs))


def test_sudo_read_surfaces_failures():
    manager = _sudo_manager(
        SimpleNamespace(exit_code=1, stdout="", stderr="permission denied")
    )
    fc = _sudo_read(manager)
    assert fc.error is not None
    assert "sudo failed" in fc.error


def test_sudo_read_rejects_truncated_output():
    manager = _sudo_manager(
        SimpleNamespace(exit_code=0, stdout="", stderr="", truncated=True)
    )
    fc = _sudo_read(manager)
    assert fc.error is not None
    assert "output cap" in fc.error


def test_sudo_line_window_is_line_aligned_and_resumable():
    payload = ("跳跃的狐狸跳来跳去 1\n跳跃的狐狸跳来跳去 2\n").encode()
    manager = _sudo_manager(
        SimpleNamespace(
            exit_code=0,
            stdout=base64.b64encode(payload).decode(),
            stderr="",
            truncated=False,
        )
    )
    fc = _sudo_read(manager, byte_limit=40)
    # 40 bytes cut inside the second line, which is dropped rather than split.
    assert fc.truncated is True
    assert "\ufffd" not in fc.content
    assert fc.content == "跳跃的狐狸跳来跳去 1\n"
    assert fc.start_line == 1
    assert fc.end_line == 1
    assert fc.next_start_line == 2


#  real SSH (integration)


requires_ssh = pytest.mark.skipif(
    not os.environ.get("SSH_TEST_HOST"),
    reason="Skipping integration test: SSH_TEST_HOST not set",
)

LINES = [f"跳跃的狐狸跳来跳去 {i}" for i in range(1, 21)]
PAYLOAD = "\n".join(LINES) + "\n"


def _conn() -> ConnectionParams:
    return ConnectionParams(
        host=os.environ["SSH_TEST_HOST"],
        port=int(os.environ.get("SSH_TEST_PORT", "22")),
        username=os.environ.get("SSH_TEST_USER"),
        password=os.environ.get("SSH_TEST_PASSWORD"),
        key_filename=os.environ.get("SSH_TEST_KEY_FILE"),
    )


def _service() -> SSHService:
    return SSHService(config=ServerConfig(max_file_bytes=200000))


@requires_ssh
def test_line_window_is_resumable_without_gaps():
    service = _service()
    conn = _conn()
    remote = "/tmp/mcp_ssh_read_window_test.txt"

    async def run():
        try:
            await service.write_file(conn, remote, PAYLOAD)
            first = await service.read_file(conn, remote, max_bytes=45)
            assert first.next_start_line is not None
            second = await service.read_file(
                conn, remote, max_bytes=45, start_line=first.next_start_line
            )
            whole = await service.read_file(conn, remote, max_bytes=200000)
            return first, second, whole
        finally:
            await service.execute(conn, f"rm -f {remote}")
            await service.close_all()

    first, second, whole = asyncio.run(run())

    # A line window returns whole lines only, so nothing is ever split.
    assert first.truncated is True
    assert "\ufffd" not in first.content
    assert first.content == LINES[0] + "\n"
    assert first.start_line == 1
    assert first.end_line == 1
    assert first.next_start_line == 2
    assert first.next_offset == len((LINES[0] + "\n").encode())
    # The resumed window starts exactly where the first one stopped.
    assert second.start_line == 2
    assert second.content == LINES[1] + "\n"
    # A window wide enough for the whole file reports the full content.
    assert whole.truncated is False
    assert whole.total_lines == len(LINES)
    assert whole.content == PAYLOAD


@requires_ssh
def test_byte_window_never_splits_a_character():
    service = _service()
    conn = _conn()
    remote = "/tmp/mcp_ssh_read_byte_test.txt"

    async def run():
        try:
            await service.write_file(conn, remote, "跳跃的狐狸跳来跳去\n")
            # Start one character in and cut inside the following character.
            return await service.read_file(conn, remote, offset=3, max_bytes=7)
        finally:
            await service.execute(conn, f"rm -f {remote}")
            await service.close_all()

    window = asyncio.run(run())

    assert "\ufffd" not in window.content
    assert window.content == "跃的"
    assert window.truncated is True
    assert window.next_offset == 9


@requires_ssh
def test_read_file_tool_renders_one_truncation_notice():
    from mcp_ssh_reloaded import server

    conn = _conn()
    remote = "/tmp/mcp_ssh_read_window_tool_test.txt"
    payload = "跳跃的狐狸跳来跳去\n" * 40

    async def run():
        service = _service()
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
