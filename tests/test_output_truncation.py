"""Truncation must be visible on the structured result, not just in stderr."""

import asyncio
import os

import pytest

from mcp_ssh_reloaded import ConnectionParams, ServerConfig
from mcp_ssh_reloaded.services import SSHService


@pytest.mark.skipif(
    not os.environ.get("SSH_TEST_HOST"),
    reason="Skipping integration test: SSH_TEST_HOST not set",
)
def test_truncation_reaches_command_result():
    # A tiny cap so `seq` overflows it immediately.
    service = SSHService(config=ServerConfig(max_output_bytes=2000))
    conn = ConnectionParams(
        host=os.environ["SSH_TEST_HOST"],
        port=int(os.environ.get("SSH_TEST_PORT", "22")),
        username=os.environ.get("SSH_TEST_USER"),
        password=os.environ.get("SSH_TEST_PASSWORD"),
        key_filename=os.environ.get("SSH_TEST_KEY_FILE"),
    )

    async def run():
        try:
            return await service.execute(conn, "seq 1 5000", timeout=20)
        finally:
            await service.close_all()

    result = asyncio.run(run())

    assert result.truncated is True
    assert result.spilled_path, "spill path must be reported on the result"
    assert os.path.exists(result.spilled_path)
    with open(result.spilled_path, encoding="utf-8") as handle:
        # The cap stops collection, so the file holds what was captured.
        assert handle.read().strip(), "spill file must not be empty"
    os.unlink(result.spilled_path)
