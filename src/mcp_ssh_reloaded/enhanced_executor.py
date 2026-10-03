"""Enhanced command execution - options layer, not a second execution stack.

Historically this module re-implemented the whole send / collect / detect-prompt
loop for its "streaming" and "auto-extend" modes, which is exactly the
behaviour drift the refactor plan calls out (P1).

It is now a thin adapter: every command goes through ``CommandExecutor`` (the
one and only execution stack) and this class contributes only *options*:

* ``auto_extend_timeout`` / ``max_timeout`` - how long the caller is willing to
  keep waiting for a command that outlives its initial timeout (the executor
  already keeps such commands alive in background monitoring, so extending the
  deadline is purely a caller-side decision)
* ``streaming_mode`` - render the collected output as a streaming transcript
* ``progress_callback`` - emit periodic progress notifications while waiting
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import aiologic

from .datastructures import CommandStatus
from .error_handler import ErrorHandler, ProgressReporter
from .logging_manager import LogLevel, get_context_logger, get_logger

if TYPE_CHECKING:
    from .session_manager import SSHSessionManager


class EnhancedCommandExecutor:
    """Options layer over :class:`CommandExecutor` (no parallel stack)."""

    def __init__(self, session_manager: SSHSessionManager) -> None:
        self.session_manager = session_manager
        self.logger = get_logger("enhanced_executor")
        self.context_logger = get_context_logger("enhanced_executor")
        self._commands: dict[str, Any] = {}
        self._lock = aiologic.Lock()

    async def execute_command_enhanced(
        self,
        host: str,
        username: str | None = None,
        command: str = "",
        password: str | None = None,
        key_filename: str | None = None,
        port: int | None = None,
        enable_password: str | None = None,
        enable_command: str = "enable",
        sudo_password: str | None = None,
        timeout: int | None = None,
        auto_extend_timeout: bool = True,
        max_timeout: int | None = None,
        streaming_mode: bool = False,
        progress_callback: str | None = None,
    ) -> str:
        """Execute a command through the shared executor, with extra options."""
        config = self.session_manager.config
        if timeout is None:
            timeout = config.default_timeout
        if max_timeout is None:
            max_timeout = config.max_timeout

        self.context_logger.log_operation_start(
            "execute_enhanced",
            f"cmd={command[:50]}..., auto_extend={auto_extend_timeout}, "
            f"streaming={streaming_mode}",
        )

        # Same validation as the plain path, so both entry points agree.
        is_valid, error_msg = self.session_manager.command_validator.validate_command(
            command,
            pty_aware=(
                self.session_manager.interactive_mode
                and self.session_manager.pty_aware_validation
            ),
        )
        if not is_valid:
            self.context_logger.log_operation_end(
                "execute_enhanced",
                success=False,
                details=f"Invalid command: {error_msg}",
            )
            return f"❌ Command validation failed: {error_msg}"

        executor = self.session_manager.command_executor
        try:
            command_id = await executor.execute_command_async(
                host,
                username,
                command,
                password,
                key_filename,
                port,
                sudo_password,
                enable_password,
                enable_command,
                timeout,
                auto_extend_timeout=auto_extend_timeout,
                max_timeout=max_timeout,
                streaming_mode=streaming_mode,
                progress_callback=progress_callback,
            )
        except Exception as e:
            info = ErrorHandler.categorize_error(str(e), e)
            self.context_logger.log_operation_end(
                "execute_enhanced", success=False, details=info.message
            )
            return ErrorHandler.format_error_for_ai(info)

        with self._lock:
            self._commands[command_id] = command

        # The executor keeps a timed-out command alive in background monitoring,
        # so "auto-extend" is simply how long we are prepared to wait for it.
        budget = max_timeout if auto_extend_timeout else timeout
        deadline = time.monotonic() + budget
        last_progress = time.monotonic()
        status: dict[str, Any] = {"status": "running"}
        while True:
            status = executor.get_command_status(command_id)
            if status.get("status") != "running":
                break
            now = time.monotonic()
            if progress_callback and now - last_progress > 5.0:
                self._send_progress_update(command_id, status, max_timeout)
                last_progress = now
            if now > deadline:
                break
            await asyncio.sleep(0.5)

        state = status.get("status")
        if state == "awaiting_input":
            reason = status.get("awaiting_input_reason", "unknown")
            self.context_logger.log_operation_end(
                "execute_enhanced", success=True, details=f"awaiting {reason}"
            )
            return (
                f"Command paused waiting for input: {reason}\n\n"
                f"Command ID: {command_id}"
            )

        stdout = status.get("stdout") or ""
        if state == "running":
            self.context_logger.log_operation_end(
                "execute_enhanced", success=False, details="still running"
            )
            return (
                f"⏰ Command still running after {budget}s "
                f"(max timeout: {max_timeout}s)\n\n"
                f"Command ID: {command_id}\n\n"
                f"Use get_command_status('{command_id}') to keep checking."
            )

        stderr = status.get("stderr") or ""
        exit_code = status.get("exit_code") or 0
        self.context_logger.log_operation_end(
            "execute_enhanced", success=(exit_code == 0), details=f"exit={exit_code}"
        )

        if streaming_mode:
            return ProgressReporter.format_streaming_output(
                stdout, command_id, len(stdout)
            )
        if exit_code == 0:
            return stdout
        return stderr or f"Command failed with exit code {exit_code}"

    def _send_progress_update(
        self, command_id: str, status: dict[str, Any], max_timeout: int
    ) -> None:
        """Emit a progress notification for a still-running command."""
        try:
            stdout = status.get("stdout") or ""
            started = status.get("start_time")
            elapsed = 0.0
            if started:
                try:
                    elapsed = (
                        datetime.now() - datetime.fromisoformat(started)
                    ).total_seconds()
                except ValueError:
                    elapsed = 0.0

            progress_msg = ProgressReporter.format_progress(
                int(elapsed),
                max_timeout or 300,
                "Command execution",
                f"{len(stdout)} bytes output",
            )
            self.context_logger.log_with_context(
                LogLevel.INFO,
                "progress",
                f"Update for {command_id}: {elapsed:.1f}s, {len(stdout)} bytes",
            )
            self.logger.info(f"PROGRESS_UPDATE: {progress_msg}")
        except Exception as e:
            self.context_logger.log_with_context(
                LogLevel.WARNING, "progress", f"Failed to send progress update: {e}"
            )

    def get_command_status_enhanced(self, command_id: str) -> dict[str, Any]:
        """Enhanced view over the shared command registry."""
        s = self.session_manager.command_executor.get_command_status(command_id)
        if "error" in s:
            return {
                "command_id": command_id,
                "status": "not_found",
                "message": s["error"],
            }

        stdout = s.get("stdout") or ""
        stderr = s.get("stderr") or ""
        data: dict[str, Any] = {
            "command_id": command_id,
            "status": s.get("status"),
            "start_time": s.get("start_time"),
            "stdout_size": len(stdout),
            "stderr_size": len(stderr),
        }
        if s.get("end_time"):
            data["end_time"] = s["end_time"]
            try:
                data["duration_seconds"] = (
                    datetime.fromisoformat(s["end_time"])
                    - datetime.fromisoformat(s["start_time"])
                ).total_seconds()
            except (KeyError, ValueError):
                pass
        if s.get("exit_code") is not None:
            data["exit_code"] = s["exit_code"]
        if s.get("awaiting_input_reason"):
            data["awaiting_input_reason"] = s["awaiting_input_reason"]

        if len(stdout) > 1000:
            data["output_preview"] = stdout[-200:]
            data["output_size_display"] = f"{len(stdout):,} bytes"
        else:
            data["output_preview"] = stdout
            data["output_size_display"] = f"{len(stdout)} bytes"
        return data


# Re-exported so ``from .enhanced_executor import CommandStatus`` style imports
# keep resolving while callers migrate to the shared model module.
__all__ = ["CommandStatus", "EnhancedCommandExecutor"]
