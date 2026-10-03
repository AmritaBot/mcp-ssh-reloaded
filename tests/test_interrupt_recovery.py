"""End-to-end guard for the interrupt deadlock (stages 4b + 5).

Before those stages, interrupting a command left its worker reading the SSH
channel, so the next command was starved: it completed on the remote host but
the tool kept reporting it as "still running" until the old worker's own
timeout expired.
"""

import asyncio
import os
import time

import pytest

from mcp_ssh_reloaded.session_manager import SSHSessionManager


def _connection_kwargs() -> dict:
    return {
        "username": os.environ.get("SSH_TEST_USER"),
        "password": os.environ.get("SSH_TEST_PASSWORD"),
        "key_filename": os.environ.get("SSH_TEST_KEY_FILE"),
        "port": int(os.environ.get("SSH_TEST_PORT", "22")),
    }


@pytest.mark.skipif(
    not os.environ.get("SSH_TEST_HOST"),
    reason="Skipping integration test: SSH_TEST_HOST not set",
)
def test_command_runs_immediately_after_interrupt():
    host = os.environ["SSH_TEST_HOST"]
    kwargs = _connection_kwargs()
    manager = SSHSessionManager()
    try:
        command_id = asyncio.run(
            manager.execute_command_async(
                host=host,
                command="echo interrupted-start; sleep 30; echo interrupted-end",
                timeout=60,
                **kwargs,
            )
        )
        time.sleep(2)

        ok, message = manager.interrupt_command_by_id(command_id)
        assert ok, message

        started = time.time()
        stdout, stderr, exit_code = asyncio.run(
            manager.execute_command(
                host=host,
                command="echo after-interrupt",
                timeout=20,
                **kwargs,
            )
        )
        elapsed = time.time() - started

        assert exit_code == 0, f"exit={exit_code} stderr={stderr!r}"
        assert "after-interrupt" in stdout, stdout
        # The whole point: it must not wait for the interrupted worker.
        assert elapsed < 15, f"next command took {elapsed:.1f}s"
    finally:
        asyncio.run(manager.close_all_sessions())


@pytest.mark.skipif(
    not os.environ.get("SSH_TEST_HOST"),
    reason="Skipping integration test: SSH_TEST_HOST not set",
)
def test_consecutive_commands_do_not_wedge_the_session():
    host = os.environ["SSH_TEST_HOST"]
    kwargs = _connection_kwargs()
    manager = SSHSessionManager()
    try:
        for i in range(3):
            stdout, stderr, exit_code = asyncio.run(
                manager.execute_command(
                    host=host,
                    command=f"echo round-{i}",
                    timeout=20,
                    **kwargs,
                )
            )
            assert exit_code == 0, f"round {i}: {stderr!r}"
            assert f"round-{i}" in stdout, stdout
    finally:
        asyncio.run(manager.close_all_sessions())
