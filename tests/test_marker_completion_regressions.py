"""Regressions for marker-based completion and the PTY plumbing around it."""

import asyncio
import os
import shutil

import pytest

from mcp_ssh_reloaded.session_manager import SSHSessionManager

pytestmark = pytest.mark.skipif(
    not os.environ.get("SSH_TEST_HOST"),
    reason="Skipping integration tests: SSH_TEST_HOST not set",
)

CFG = {
    "host": os.environ.get("SSH_TEST_HOST", ""),
    "username": os.environ.get("SSH_TEST_USER"),
    "password": os.environ.get("SSH_TEST_PASSWORD"),
    "port": int(os.environ.get("SSH_TEST_PORT", "22")),
}


def test_sentinel_completion_strips_pty_plumbing(monkeypatch):
    """Without OSC 633 the sentinel path must still clean up after itself."""
    sm = SSHSessionManager()
    monkeypatch.setattr(sm, "is_osc633", lambda key: False)

    async def run():
        try:
            return await sm.execute_result(
                command="echo plumbing-check", timeout=20, **CFG
            )
        finally:
            await sm.close_all_sessions()

    result = asyncio.run(run())

    assert result.status.value == "completed", result.stderr
    assert result.exit_code == 0
    assert "__MCP_CMD_" not in result.stdout
    assert "__mcp_status" not in result.stdout
    assert result.stdout.strip() == "plumbing-check"


def test_osc633_nonce_survives_a_reinstall():
    """Re-installing must keep the nonce an in-flight command is waiting for."""
    sm = SSHSessionManager()

    async def run():
        try:
            client = await sm.get_or_create_session(**CFG)
            _, _, _, _, key = sm._resolve_connection(
                CFG["host"], CFG["username"], CFG["port"]
            )
            shell = sm._get_or_create_shell(key, client)
            first = sm.osc633_nonce(key)
            sm._install_osc633(key, client, shell)
            return first, sm.osc633_nonce(key)
        finally:
            await sm.close_all_sessions()

    first, second = asyncio.run(run())
    if not first:
        pytest.skip("OSC 633 is unavailable on this target")
    assert second == first


def test_long_output_completes_and_spills():
    """A long stream must still reach its completion marker."""
    sm = SSHSessionManager()

    async def run():
        try:
            return await sm.execute_result(command="seq 1 10000", timeout=60, **CFG)
        finally:
            await sm.close_all_sessions()

    result = asyncio.run(run())

    assert result.status.value == "completed", result.stderr
    assert result.exit_code == 0
    assert result.truncated is True
    assert result.spilled_path, "a truncated run must report its spill file"
    try:
        with open(result.spilled_path, encoding="utf-8") as handle:
            body = handle.read()
        assert "9999" in body
        assert "10000" in body
        assert len(result.stdout.encode()) < 8000, "long output must not be echoed"
        assert f"[full output saved to {result.spilled_path}]" in result.stdout
    finally:
        shutil.rmtree(os.path.dirname(result.spilled_path), ignore_errors=True)
