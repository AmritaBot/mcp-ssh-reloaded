"""Enable mode management for network devices.

Extracted from session_manager.py.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

import paramiko

if TYPE_CHECKING:
    from .session_manager import SSHSessionManager


class EnableMode:
    """Handles enable/privileged mode entry on network devices."""

    ENABLE_MODE_TIMEOUT = 10

    def __init__(self, sm: SSHSessionManager):
        self._sm = sm

    @property
    def _enable_mode(self):
        return self._sm.registry.enable_mode

    @property
    def _session_prompts(self):
        return self._sm.registry.prompts

    @property
    def logger(self):
        return self._sm.logger

    async def enter(
        self,
        session_key: str,
        client: paramiko.SSHClient,
        enable_password: str,
        enable_command: str = "enable",
        timeout: int | None = None,
    ) -> tuple[bool, str]:
        if timeout is None:
            timeout = self.ENABLE_MODE_TIMEOUT

        logger = self.logger.getChild("enable_mode")
        logger.info(f"Starting enable mode workflow for {session_key}")

        try:
            shell = self._sm._get_or_create_shell(session_key, client)
            shell.settimeout(timeout)

            await asyncio.to_thread(shell.send, b"terminal length 0\n")
            await asyncio.sleep(0.5)

            output = ""
            if shell.recv_ready():
                output = (await asyncio.to_thread(shell.recv, 4096)).decode(
                    "utf-8", errors="ignore"
                )

            await asyncio.to_thread(shell.send, f"{enable_command}\n".encode())
            await asyncio.sleep(0.5)

            password_sent = False
            start_time = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start_time < timeout:
                if shell.recv_ready():
                    chunk = (await asyncio.to_thread(shell.recv, 4096)).decode(
                        "utf-8", errors="ignore"
                    )
                    output += chunk

                    if "#" in output and output.strip().endswith("#"):
                        logger.info("Already in enable mode")
                        self._enable_mode[session_key] = True
                        if session_key in self._session_prompts:
                            old_prompt = self._session_prompts[session_key]
                            base_prompt = old_prompt.replace(">", "")
                            self._session_prompts[session_key] = base_prompt + "*[>#]"
                            logger.info(
                                f"Updated prompt from '{old_prompt}' to '{self._session_prompts[session_key]}'"
                            )
                        return True, "Already in enable mode"

                    if re.search(r"[Pp]assword:|password.*:", output):
                        logger.info("Sending enable password")
                        await asyncio.to_thread(
                            shell.send, f"{enable_password}\n".encode()
                        )
                        await asyncio.sleep(0.5)
                        password_sent = True
                        break
                await asyncio.sleep(0.1)

            if not password_sent:
                error_msg = (
                    f"Timeout waiting for enable password prompt. Output: {output}"
                )
                logger.error(error_msg)
                return False, error_msg

            output = ""
            start_time = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start_time < timeout:
                if shell.recv_ready():
                    chunk = (await asyncio.to_thread(shell.recv, 4096)).decode(
                        "utf-8", errors="ignore"
                    )
                    output += chunk
                    if "#" in output and output.strip().endswith("#"):
                        logger.info("Successfully entered enable mode")
                        self._enable_mode[session_key] = True
                        if session_key in self._session_prompts:
                            old_prompt = self._session_prompts[session_key]
                            base_prompt = old_prompt.replace(">", "")
                            self._session_prompts[session_key] = base_prompt + "*[>#]"
                        return True, "Entered enable mode successfully"
                await asyncio.sleep(0.1)

            return False, f"Timeout waiting for enable prompt. Output: {output}"

        except Exception as exc:
            logger.exception(f"Failed to enter enable mode: {exc}")
            return False, f"Failed to enter enable mode: {exc}"
