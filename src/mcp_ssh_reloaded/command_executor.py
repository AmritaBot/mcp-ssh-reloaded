"""Command execution for SSH sessions."""

from __future__ import annotations

import asyncio
import atexit
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

import aiologic
import paramiko

from mcp_ssh_reloaded.api_types import ServerConfig

if TYPE_CHECKING:
    from mcp_ssh_reloaded.session_manager import SSHSessionManager

from .models import CommandStatus, ExecutionResult, RunningCommand
from .output_buffer import OutputBuffer


def _result_from_legacy(
    stdout: str,
    stderr: str,
    exit_code: int,
    awaiting_input: str | None = None,
    sentinel: str | None = None,
) -> ExecutionResult:
    """The single place that interprets the legacy exit-code conventions.

    The low-level executors still report ``124`` for "still running" and encode
    the awaiting-input reason as a ``"Command requires input: "`` stderr prefix.
    This adapter turns that into a structured :class:`ExecutionResult`, so no
    caller downstream has to look at magic numbers or string prefixes.
    """
    if awaiting_input is None and stderr.startswith("Command requires input: "):
        awaiting_input = stderr[len("Command requires input: ") :]
    if awaiting_input:
        return ExecutionResult(
            status=CommandStatus.AWAITING_INPUT,
            stdout=stdout,
            stderr=stderr,
            awaiting_input=awaiting_input,
            sentinel=sentinel,
        )
    if exit_code == 124:
        return ExecutionResult(
            status=CommandStatus.RUNNING,
            stdout=stdout,
            stderr=stderr,
            sentinel=sentinel,
        )
    return ExecutionResult(
        status=CommandStatus.COMPLETED,
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        sentinel=sentinel,
    )


def _render_legacy(result: ExecutionResult) -> tuple[str, str, int]:
    """Render an :class:`ExecutionResult` in the legacy ``(stdout, stderr, code)`` form."""
    if result.status is CommandStatus.RUNNING:
        suffix = ":long_running" if result.long_running else ""
        return result.stdout, f"ASYNC:{result.command_id}{suffix}", 124
    if result.status is CommandStatus.AWAITING_INPUT:
        return "", f"AWAITING_INPUT:{result.command_id}:{result.awaiting_input}", 124
    return result.stdout, result.stderr, result.exit_code or 0


class CommandExecutor:
    """Executes commands on SSH sessions."""

    def __init__(
        self, session_manager: SSHSessionManager, config: ServerConfig | None = None
    ):
        from .api_types import ServerConfig

        self._session_manager = session_manager
        self.config = config if config is not None else ServerConfig()
        self.logger = logging.getLogger("ssh_session.command_executor")
        self._commands: dict[str, RunningCommand] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=self._session_manager.MAX_WORKERS, thread_name_prefix="ssh_cmd"
        )
        self._lock = aiologic.Lock()
        self._interpreter_exiting = False

        # Mark when the interpreter is shutting down so we can skip late submissions
        atexit.register(self._mark_interpreter_exit)

    @property
    def _sm(self) -> SSHSessionManager:
        """The owning session manager (shared session state lives here)."""
        return self._session_manager

    # Package manager commands that need special handling
    PACKAGE_MANAGER_PATTERNS: ClassVar[list[str]] = [
        r"\bpkg\s+(install|upgrade|update|remove|delete)\b",
        r"\bapt(?:-get)?\s+(install|upgrade|update|dist-upgrade|full-upgrade|remove|purge)\b",
        r"\b(?:dnf|yum|zypper)\s+(install|upgrade|update|remove|erase)\b",
        r"\bpacman\s+(-[SsRr]\b|--sync\b|--remove\b|install|upgrade|update|remove)",
        r"\bapk\s+(add|install|upgrade|update|del|delete)\b",
        r"\bbrew\s+(install|upgrade|update|uninstall|remove)\b",
        r"\b(?:pip|pip3)\s+install\b",
        r"\bnpm\s+install\b",
        r"\bpnpm\s+install\b",
        r"\byarn\s+add\b",
    ]

    # Commands that are known to spawn interactive wizards
    INTERACTIVE_WIZARD_PATTERNS: ClassVar[list[str]] = [
        r"\bfish_config\b",
        r"fish\s+-c\s+.*fish_config",
        r"\bdconf-editor\b",
        r"\bnmtui\b",
        r"\braspi-config\b",
    ]

    def _mark_interpreter_exit(self):
        self._interpreter_exiting = True

    async def execute_result(
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
        timeout: int = 30,
    ) -> ExecutionResult:
        """Primary entry point: run a command and return a structured result."""
        logger = self.logger.getChild("execute_result")
        logger.info(
            f"[EXEC_REQ] host={host}, cmd={command[:100]}..., timeout={timeout}"
        )

        # Validate command
        is_valid, error_msg = self._session_manager.command_validator.validate_command(
            command,
            pty_aware=(
                self._session_manager.interactive_mode
                and self._session_manager.pty_aware_validation
            ),
        )
        if not is_valid:
            logger.warning(f"[EXEC_INVALID] {error_msg}")
            return ExecutionResult(
                status=CommandStatus.FAILED,
                stderr=error_msg or "Invalid command",
                exit_code=1,
            )

        # Check for interactive wizard commands that will hang
        if self._is_interactive_wizard(command):
            logger.warning(
                f"[EXEC_WIZARD] Detected interactive wizard command: {command}"
            )
            return ExecutionResult(
                status=CommandStatus.FAILED,
                stderr=(
                    f"Command '{command}' appears to spawn an interactive wizard "
                    "that requires user interaction. Interactive wizards cannot be "
                    "run via SSH session. Consider using non-interactive "
                    "alternatives:\n"
                    "- For fish_config: Use 'fish -c \"set -U fish_color_* ...\"' to set colors directly\n"
                    "- For package managers: Use with -y/--yes flags to avoid prompts"
                ),
                exit_code=1,
            )

        # Start async
        logger.debug("[EXEC_ASYNC_START] Starting async execution")
        try:
            command_id = await self.execute_command_async(
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
            )
        except Exception as e:
            return ExecutionResult(
                status=CommandStatus.FAILED, stderr=str(e), exit_code=1
            )

        # Package manager installs/upgrades commonly exceed MCP client-side call
        # timeouts, so they are handed straight back for the caller to poll.
        if self._should_start_async_immediately(command):
            logger.info(f"[EXEC_ASYNC_IMMEDIATE] command_id={command_id}")
            return ExecutionResult(
                status=CommandStatus.RUNNING,
                command_id=command_id,
                long_running=True,
            )

        logger.debug(f"[EXEC_ASYNC_ID] command_id={command_id}")

        # Poll until done or timeout
        start = time.time()
        poll_count = 0
        while time.time() - start < timeout:
            status = self.get_command_status(command_id)
            poll_count += 1

            if "error" in status:
                logger.error(f"[EXEC_ERROR] {status['error']}")
                return ExecutionResult(
                    status=CommandStatus.FAILED,
                    command_id=command_id,
                    stderr=status["error"],
                    exit_code=1,
                )

            if status["status"] == "awaiting_input":
                reason = status.get("awaiting_input_reason", "unknown")
                logger.info(
                    f"[EXEC_AWAIT] Command {command_id} waiting for input: {reason}"
                )
                return ExecutionResult(
                    status=CommandStatus.AWAITING_INPUT,
                    command_id=command_id,
                    stdout=status.get("stdout", ""),
                    awaiting_input=reason,
                )

            if status["status"] != "running":
                logger.info(
                    f"[EXEC_DONE] status={status['status']}, polls={poll_count}, "
                    f"duration={time.time() - start:.2f}s"
                )
                return ExecutionResult(
                    status=CommandStatus.COMPLETED,
                    command_id=command_id,
                    stdout=status["stdout"],
                    stderr=status["stderr"],
                    exit_code=status["exit_code"] or 0,
                )

            await asyncio.sleep(0.1)

        # The command outlived this call's timeout; the executor keeps it alive
        # in background monitoring, so report it as still running.
        logger.warning(
            f"[EXEC_TIMEOUT] Command {command_id} timed out after {timeout}s"
        )
        status_on_timeout = self.get_command_status(command_id)
        return ExecutionResult(
            status=CommandStatus.RUNNING,
            command_id=command_id,
            stdout=status_on_timeout.get("stdout", ""),
        )

    async def execute_command(
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
        timeout: int = 30,
    ) -> tuple[str, str, int]:
        """Legacy tuple API, rendered from :meth:`execute_result`.

        Kept so the MCP text output and older callers are unchanged.
        """
        return _render_legacy(
            await self.execute_result(
                host,
                username,
                command,
                password,
                key_filename,
                port,
                enable_password,
                enable_command,
                sudo_password,
                timeout,
            )
        )

    @staticmethod
    def _should_start_async_immediately(command: str) -> bool:
        """Check if command should immediately start in async mode."""
        command_lower = command.lower().strip()
        return any(
            re.search(pattern, command_lower)
            for pattern in CommandExecutor.PACKAGE_MANAGER_PATTERNS
        )

    @staticmethod
    def _is_interactive_wizard(command: str) -> bool:
        """Check if command spawns an interactive wizard that won't return to prompt."""
        command_lower = command.lower().strip()
        return any(
            re.search(pattern, command_lower)
            for pattern in CommandExecutor.INTERACTIVE_WIZARD_PATTERNS
        )

    async def execute_command_async(
        self,
        host: str,
        username: str | None = None,
        command: str = "",
        password: str | None = None,
        key_filename: str | None = None,
        port: int | None = None,
        sudo_password: str | None = None,
        enable_password: str | None = None,
        enable_command: str = "enable",
        timeout: int | None = None,
        auto_extend_timeout: bool = False,
        max_timeout: int = 300,
        streaming_mode: bool = False,
        progress_callback: str | None = None,
    ) -> str:
        """Execute a command asynchronously without blocking.

        The trailing keyword options are the former EnhancedCommandExecutor
        features, now expressed as options on the one execution stack.
        """
        if timeout is None:
            timeout = self.config.async_default_timeout
        logger = self.logger.getChild("execute_async")
        logger.info(f"[ASYNC_START] host={host}, cmd={command[:100]}...")

        _, _, _, _, session_key = self._session_manager._resolve_connection(
            host, username, port
        )

        # Prepare session and shell first (outside lock to avoid blocking)
        # Note: This avoids race conditions where two threads check for running commands
        # simultaneously before either registers one.
        client = await self._session_manager.get_or_create_session(
            host, username, password, key_filename, port
        )
        shell = self._session_manager._get_or_create_shell(session_key, client)

        command_id = str(uuid.uuid4())
        logger.debug(f"Generated command_id: {command_id}")

        running_cmd = RunningCommand(
            command_id=command_id,
            session_key=session_key,
            command=command,
            shell=shell,
            future=None,
            status=CommandStatus.RUNNING,
            stdout="",
            stderr="",
            exit_code=None,
            start_time=datetime.now(),
            end_time=None,
            auto_extend_timeout=auto_extend_timeout,
            max_timeout=max_timeout,
            streaming_mode=streaming_mode,
            progress_callback=progress_callback,
        )

        stuck_shells = []
        # Atomic check and registration
        with self._lock:
            # Check for stuck commands and auto-recover if they've been running too long
            for cmd in self._commands.values():
                if cmd.session_key == session_key and cmd.status in (
                    CommandStatus.RUNNING,
                    CommandStatus.AWAITING_INPUT,
                ):
                    # Check if command has been stuck for too long (30 seconds)
                    cmd_age = (datetime.now() - cmd.start_time).total_seconds()
                    if cmd_age > 30:  # 30 seconds threshold
                        logger.warning(
                            f"Detected stuck command {cmd.command_id} (age: {cmd_age:.0f}s, status: {cmd.status.value}). "
                            f"Auto-interrupting and allowing new command."
                        )
                        # Mark as interrupted first to stop background monitor from competing
                        cmd.status = CommandStatus.INTERRUPTED
                        cmd.end_time = datetime.now()
                        cmd.stderr += (
                            f"\n[Auto-interrupted after {cmd_age:.0f}s due to timeout]"
                        )
                        cmd.monitoring_cancelled.set()

                        # Collect shell to interrupt OUTSIDE the lock to avoid deadlock
                        stuck_shells.append(cmd.shell)
                    else:
                        error_msg = self._build_running_command_error(session_key, cmd)
                        logger.error(error_msg)
                        raise Exception(error_msg)

        # Handle any stuck shells after releasing the lock
        for s in stuck_shells:
            try:
                s.send(b"\x03")  # Send Ctrl+C
            except Exception as e:  # noqa: PERF203
                logger.error(f"Failed to auto-interrupt stuck command: {e}")

        # Atomic registration
        with self._lock:
            self._commands[command_id] = running_cmd
            logger.debug(f"Registered running command {command_id}")

        logger.debug(f"[ASYNC_SUBMIT] Submitting command {command_id} to thread pool")
        future = self._executor.submit(
            self._execute_command_async_worker,
            command_id,
            client,
            command,
            timeout,
            session_key,
            sudo_password,
            enable_password,
            enable_command,
        )
        running_cmd.future = future
        logger.info(f"[ASYNC_SUBMITTED] command_id={command_id}")

        return command_id

    @staticmethod
    def _build_running_command_error(session_key: str, cmd: RunningCommand) -> str:
        return (
            f"A command is already running or awaiting input in this session ({session_key}).\n"
            f"Active Command ID: {cmd.command_id}\n"
            f"Active Command Status: {cmd.status.value}\n"
            "This usually means a previous command timed out and continues in background monitoring.\n"
            "Next steps:\n"
            f"- Check progress: get_command_status('{cmd.command_id}')\n"
            f"- Interrupt it: interrupt_command_by_id('{cmd.command_id}')\n"
            "- Or wait until it completes before running another command."
        )

    def _execute_command_async_worker(
        self,
        command_id: str,
        client: paramiko.SSHClient,
        command: str,
        timeout: int,
        session_key: str,
        sudo_password: str | None = None,
        enable_password: str | None = None,
        enable_command: str = "enable",
    ):
        """Execute command in background thread and update running command state."""
        logger = self.logger.getChild("async_worker")
        logger.debug(f"[WORKER_START] command_id={command_id}")

        # Only one read loop may hold a session's shell at a time.  The
        # previous holder is signalled to stop by interrupt/auto-recovery,
        # so this normally returns almost immediately.
        exec_lock = self._sm.registry.execution_lock(session_key)
        exec_lock.acquire()
        output_buffer = OutputBuffer(spill_dir=self.config.log_dir)
        try:
            with self._lock:
                if command_id not in self._commands:
                    logger.error(
                        f"[WORKER_NOTFOUND] command_id={command_id} no longer in registry."
                    )
                    return
                running_cmd = self._commands[command_id]

            logger.debug(f"[WORKER_EXEC] Executing command for {command_id}")

            if sudo_password:
                logger.debug(f"Executing as sudo for {command_id}")
                stdout, stderr, exit_code = self._execute_sudo_command_internal(
                    client,
                    command,
                    sudo_password,
                    timeout,
                    cancel_event=running_cmd.monitoring_cancelled,
                    output_buffer=output_buffer,
                )
                result = _result_from_legacy(stdout, stderr, exit_code)
            elif enable_password:
                logger.debug(f"Executing in enable mode for {command_id}")
                stdout, stderr, exit_code = asyncio.run(
                    self._execute_enable_mode_command_internal(
                        client,
                        session_key,
                        command,
                        enable_password,
                        enable_command,
                        timeout,
                        cancel_event=running_cmd.monitoring_cancelled,
                        output_buffer=output_buffer,
                    )
                )
                result = _result_from_legacy(stdout, stderr, exit_code)
            else:
                logger.debug(f"Executing as standard command for {command_id}")
                (
                    stdout,
                    stderr,
                    exit_code,
                    awaiting_input_reason,
                    sentinel,
                ) = self._execute_standard_command_internal(
                    client,
                    command,
                    timeout,
                    session_key,
                    cancel_event=running_cmd.monitoring_cancelled,
                    output_buffer=output_buffer,
                )
                result = _result_from_legacy(
                    stdout, stderr, exit_code, awaiting_input_reason, sentinel
                )
                with self._lock:
                    if command_id in self._commands:
                        running_cmd.sentinel = result.sentinel

            logger.debug(
                f"[WORKER_DONE] command_id={command_id}, "
                f"status={result.status.value}, exit_code={result.exit_code}, "
                f"awaiting_input={result.awaiting_input}"
            )

            # Surface the buffer's verdict on the structured result.
            result.truncated = output_buffer.truncated
            result.spilled_path = output_buffer.spilled_path

            if running_cmd.monitoring_cancelled.is_set():
                logger.info(f"Command {command_id} was interrupted")
                with self._lock:
                    if command_id in self._commands:
                        running_cmd.stdout = result.stdout
                        running_cmd.stderr = result.stderr or "Command interrupted"
                        running_cmd.exit_code = result.exit_code
                        running_cmd.status = CommandStatus.INTERRUPTED
                        running_cmd.end_time = datetime.now()
                return

            # Handle timeout case - command is still running on remote shell
            if result.status is CommandStatus.RUNNING:
                logger.warning(
                    f"Command {command_id} timed out after {timeout}s, continuing to monitor in background"
                )
                with self._lock:
                    if command_id in self._commands:
                        running_cmd.stdout = result.stdout
                        # Preserve existing stderr if it has useful info (like Output limit exceeded)
                        timeout_msg = (
                            f"Command exceeded {timeout}s timeout, still running..."
                        )
                        if result.stderr and result.stderr != "Timed out":
                            running_cmd.stderr = f"{result.stderr}\n{timeout_msg}"
                        else:
                            running_cmd.stderr = timeout_msg
                        running_cmd.exit_code = (
                            None  # Clear exit code since it's still running
                        )
                        # Keep status as RUNNING
                        logger.info(
                            f"Command {command_id} still running after timeout, submitting background monitor"
                        )

                # Continue monitoring in background
                try:
                    # Guard against interpreter shutdown or executor shutdown during teardown
                    if self._interpreter_exiting or getattr(
                        self._executor, "_shutdown", False
                    ):
                        logger.warning(
                            f"Executor shutting down; skip background monitor for {command_id}"
                        )
                        return

                    self._executor.submit(
                        self._continue_monitoring_timeout_background,
                        command_id,
                        running_cmd,
                        session_key,
                        timeout_occurred_at=time.time(),
                    )
                except RuntimeError as submit_err:
                    logger.debug(
                        f"Skip background monitor for {command_id}: {submit_err}"
                    )
                return

            # Normal completion or awaiting input
            with self._lock:
                if command_id in self._commands:
                    running_cmd.stdout = result.stdout
                    running_cmd.stderr = result.stderr
                    running_cmd.exit_code = result.exit_code
                    running_cmd.awaiting_input_reason = result.awaiting_input
                    if result.status is CommandStatus.AWAITING_INPUT:
                        running_cmd.status = CommandStatus.AWAITING_INPUT
                        logger.info(
                            f"Command {command_id} awaiting input: {result.awaiting_input}"
                        )
                    else:
                        running_cmd.status = CommandStatus.COMPLETED
                        running_cmd.end_time = datetime.now()
                        logger.info(f"Command {command_id} completed.")
        except Exception as e:
            logger.error(
                f"[WORKER_ERROR] command_id={command_id}, error={e}", exc_info=True
            )
            with self._lock:
                if command_id in self._commands:
                    running_cmd = self._commands[command_id]
                    running_cmd.stderr = str(e)
                    running_cmd.exit_code = 1
                    running_cmd.status = CommandStatus.FAILED
                    running_cmd.end_time = datetime.now()
        finally:
            output_buffer.close()
            exec_lock.release()
            # Cleanup old commands
            self._session_manager._cleanup_old_commands()

    def get_command_status(self, command_id: str) -> dict:
        """Get the status and output of an async command."""
        logger = self.logger.getChild("get_status")
        with self._lock:
            if command_id not in self._commands:
                logger.error(f"Command ID not found: {command_id}")
                return {"error": "Command ID not found"}

            cmd = self._commands[command_id]

            # Merge any pending output chunks into stdout
            if cmd.output_chunks:
                cmd.stdout += "".join(cmd.output_chunks)
                cmd.output_chunks = []

            status_payload = {
                "command_id": cmd.command_id,
                "session_key": cmd.session_key,
                "command": cmd.command,
                "status": cmd.status.value,
                "stdout": cmd.stdout,
                "stderr": cmd.stderr,
                "exit_code": cmd.exit_code,
                "start_time": cmd.start_time.isoformat(),
                "end_time": cmd.end_time.isoformat() if cmd.end_time else None,
                "awaiting_input_reason": cmd.awaiting_input_reason,
            }
            return status_payload

    def interrupt_command_by_id(self, command_id: str) -> tuple[bool, str]:
        """Interrupt a running async command by its ID."""
        logger = self.logger.getChild("interrupt")
        logger.info(f"Attempting to interrupt command_id: {command_id}")

        shell_to_interrupt = None
        with self._lock:
            if command_id not in self._commands:
                logger.error(f"Command ID not found for interrupt: {command_id}")
                return False, f"Command ID {command_id} not found"

            cmd = self._commands[command_id]
            if cmd.status != CommandStatus.RUNNING:
                logger.warning(
                    f"Command {command_id} is not running (status: {cmd.status.value})"
                )
                return (
                    False,
                    f"Command {command_id} is not running (status: {cmd.status.value})",
                )

            # Prepare for interrupt
            shell_to_interrupt = cmd.shell
            cmd.status = CommandStatus.INTERRUPTED
            cmd.end_time = datetime.now()
            # Stop the worker's read loop too, otherwise it keeps draining
            # the channel and starves the next command.
            cmd.monitoring_cancelled.set()

        if shell_to_interrupt:
            try:
                logger.debug(f"Sending Ctrl+C to shell for command {command_id}")
                shell_to_interrupt.send(b"\x03")  # Send Ctrl+C
                logger.info(
                    f"Successfully sent interrupt signal to command {command_id}"
                )
                return True, f"Sent interrupt signal to command {command_id}"
            except Exception as e:
                logger.error(
                    f"Failed to interrupt command {command_id}: {e}", exc_info=True
                )
                return False, f"Failed to interrupt command {command_id}: {e}"

        return False, "Internal error: could not identify shell to interrupt"

    async def send_input(
        self, command_id: str, input_text: str
    ) -> tuple[bool, str, str]:
        """Send input to a running command and return any new output."""
        logger = self.logger.getChild("send_input")
        logger.info(f"Sending input to command_id: {command_id}")

        shell_to_use = None
        cmd_to_update = None
        with self._lock:
            if command_id not in self._commands:
                logger.error(f"Command ID not found: {command_id}")
                return False, "", "Command ID not found"

            cmd = self._commands[command_id]
            # Allow sending input to commands that are RUNNING or AWAITING_INPUT
            if cmd.status not in (CommandStatus.RUNNING, CommandStatus.AWAITING_INPUT):
                logger.warning(f"Command is not active (status: {cmd.status.value})")
                return False, "", f"Command is not active (status: {cmd.status.value})"

            shell_to_use = cmd.shell
            cmd_to_update = cmd

        try:
            # Handle escaped newlines - convert literal \n to actual newlines
            processed_input = input_text.replace("\\n", "\n").replace("\\r", "\r")
            logger.debug(f"Original input: {input_text!r}")
            logger.debug(f"Processed input: {processed_input!r}")

            # Send OUTSIDE the lock
            bytes_sent = shell_to_use.send(processed_input.encode("utf-8"))
            logger.debug(f"Sent {bytes_sent} bytes to shell")
            await asyncio.sleep(0.2)

            with self._lock:
                # If command was awaiting input, transition back to RUNNING and continue monitoring
                if cmd_to_update.status == CommandStatus.AWAITING_INPUT:
                    cmd_to_update.status = CommandStatus.RUNNING
                    cmd_to_update.awaiting_input_reason = (
                        None  # Clear the awaiting input reason
                    )
                    logger.info(
                        f"Command {command_id} transitioned from AWAITING_INPUT to RUNNING after input sent"
                    )

                    # Submit a background task to continue monitoring for command completion
                    # We don't wait for it - that would block the MCP server
                    logger.debug(
                        f"Submitting background monitoring task for {command_id}"
                    )
                    self._executor.submit(
                        self._continue_monitoring_shell_background,
                        command_id,
                        cmd_to_update,
                    )
                    logger.debug(f"Background monitoring submitted for {command_id}")
                    return True, "", ""

                # Read any new output (for commands that were already RUNNING)
                output = ""
                if shell_to_use.recv_ready():
                    output = shell_to_use.recv(65535).decode("utf-8", errors="replace")
                    cmd_to_update.stdout += output
                    logger.debug(f"Received {len(output)} bytes of new output.")

                return True, output, ""
        except Exception as e:
            logger.error(f"Failed to send input: {e}", exc_info=True)
            return False, "", f"Failed to send input: {e}"

    def _retrieve_exit_code(self, shell: paramiko.Channel, session_key: str) -> int:
        """Attempt to retrieve the exit code of the last command executed in the shell."""
        logger = self.logger.getChild("retrieve_exit_code")
        try:
            # Determine correct syntax for exit code check
            shell_type = self._session_manager.registry.shell_types.get(
                session_key, "unknown"
            ).lower()
            if "fish" in shell_type:
                cmd = " echo $status\n"
            elif "csh" in shell_type or "tcsh" in shell_type:
                cmd = " echo $status\n"
            else:
                cmd = " echo $?\n"

            # Send command
            shell.send(cmd.encode("utf-8"))
            time.sleep(0.2)

            # Read output
            output: str = ""
            start_time = time.time()
            # Wait up to 2 seconds for response
            while time.time() - start_time < 2.0:
                if shell.recv_ready():
                    chunk = shell.recv(4096).decode("utf-8", errors="ignore")
                    output += chunk
                    if "\n" in output.strip():
                        break
                time.sleep(0.1)

            # Parse output
            # Output will contain the command echo (maybe) and the result number
            clean_output = self._session_manager._strip_ansi(output)

            # Look for the last number in the output
            matches = re.findall(r"\b(\d+)\b", clean_output)
            if matches:
                # The last number is likely the exit code
                # But we need to be careful about the command echo "echo 0"
                # If we send "echo $?", we might get:
                # echo $?
                # 0
                # prompt

                # If we have multiple numbers, the last one before the prompt (if any) or just the last line number
                # A safer bet is searching for a line that is just digits
                lines = [
                    line.strip() for line in clean_output.splitlines() if line.strip()
                ]
                for line in reversed(lines):
                    if line.isdigit():
                        code = int(line)
                        logger.debug(f"Retrieved exit code: {code}")
                        return code

            logger.warning(f"Could not parse exit code from output: {output!r}")
            return 0  # Default to 0 if we can't determine

        except Exception as e:
            logger.error(f"Error retrieving exit code: {e}")
            return 0

    def _continue_monitoring_timeout_background(
        self,
        command_id: str,
        cmd: RunningCommand,
        session_key: str,
        timeout_occurred_at: float,
    ) -> None:
        """Background task to monitor shell output after a timeout occurred.

        Continues monitoring the shell until command actually completes.
        This prevents premature completion when commands take longer than the timeout.
        """
        logger = self.logger.getChild("timeout_monitor_bg")
        logger.info(
            f"[TIMEOUT_MONITOR_START] command_id={command_id}, "
            f"delay_since_timeout={time.time() - timeout_occurred_at:.2f}s"
        )

        max_additional_timeout = self.config.background_monitor_max_timeout
        idle_timeout = self.config.normal_idle_timeout
        last_recv_time = time.time()
        start_time = time.time()

        # Seed the buffer with output collected before monitoring started,
        # so a spill file really does hold the whole stream.
        output_buffer = OutputBuffer(spill_dir=self.config.log_dir)
        output_buffer.add_chunk(cmd.stdout)

        last_log_time = 0.0
        poll_count = 0

        try:
            while time.time() - start_time < max_additional_timeout:
                poll_count += 1
                # Check cancellation signal
                if cmd.monitoring_cancelled.is_set():
                    logger.info(
                        f"[TIMEOUT_MONITOR_CANCELLED] Monitoring cancelled for {command_id}"
                    )
                    return

                try:
                    # Check if command was interrupted/cancelled
                    with self._lock:
                        if command_id not in self._commands:
                            logger.info(
                                f"[TIMEOUT_MONITOR_CANCELLED] Command {command_id} removed from registry"
                            )
                            return
                        if cmd.status == CommandStatus.INTERRUPTED:
                            logger.info(
                                f"[TIMEOUT_MONITOR_INTERRUPTED] Command {command_id} was interrupted"
                            )
                            return

                    if cmd.shell.recv_ready():
                        chunk_bytes = cmd.shell.recv(65535)
                        if not chunk_bytes:
                            # EOF/Channel closed
                            logger.info(
                                f"[TIMEOUT_MONITOR_EOF] Channel closed for {command_id}"
                            )
                            with self._lock:
                                if command_id in self._commands:
                                    cmd.status = CommandStatus.COMPLETED
                                    cmd.exit_code = 0
                                    cmd.end_time = datetime.now()
                            return

                        chunk = chunk_bytes.decode("utf-8", errors="replace")
                        if chunk:
                            # Feed to terminal emulator
                            self._session_manager._feed_emulator(session_key, chunk)

                            # Apply output limiting
                            chunk_to_add, should_continue = output_buffer.add_chunk(
                                chunk
                            )

                            with self._lock:
                                if command_id in self._commands:
                                    cmd.output_chunks.append(chunk_to_add)

                            if not should_continue:
                                logger.warning(
                                    f"[TIMEOUT_MONITOR_LIMIT] Output limit reached for {command_id}"
                                )
                                with self._lock:
                                    if command_id in self._commands:
                                        # Ensure stdout is updated before failing
                                        if cmd.output_chunks:
                                            cmd.stdout += "".join(cmd.output_chunks)
                                            cmd.output_chunks = []
                                        cmd.status = CommandStatus.FAILED
                                        cmd.stderr += f"\n{output_buffer.limit_message}"
                                        cmd.end_time = datetime.now()
                                return

                            last_recv_time = time.time()

                            # Rate limit logging (max once per second unless large chunk)
                            if len(chunk) > 1000 or (time.time() - last_log_time) > 1.0:
                                logger.debug(
                                    f"[TIMEOUT_MONITOR_RECV] Received {len(chunk)} bytes"
                                )
                                last_log_time = time.time()

                            # Optimization: During active output, we primarily want to update stdout
                            # so get_command_status shows progress. We only check for prompts/input
                            # if the chunk looks like it might contain one (e.g. ends with prompt-like char)
                            # or if we have a lot of new data.
                            should_check = False
                            if len(chunk) < 100:  # Small chunks often contain prompts
                                stripped_chunk = self._session_manager._strip_ansi(
                                    chunk
                                )
                                if (
                                    stripped_chunk
                                    and stripped_chunk.strip()
                                    and stripped_chunk.strip()[-1]
                                    in ("$", "#", ">", "%", ":", "?")
                                ):
                                    should_check = True

                            # Also check if we have accumulated a lot of data since last check
                            # and periodically update stdout so users see progress
                            if not should_check and (
                                len(chunk) > 16384 or poll_count % 50 == 0
                            ):
                                should_check = True

                            if should_check:
                                # Update stdout before checks to avoid O(N^2) concatenation in every loop
                                with self._lock:
                                    if (
                                        command_id in self._commands
                                        and cmd.output_chunks
                                    ):
                                        cmd.stdout += "".join(cmd.output_chunks)
                                        cmd.output_chunks = []

                                # Check for interactive prompts
                                awaiting = self._session_manager._detect_awaiting_input(
                                    cmd.stdout, session_key
                                )

                                if awaiting:
                                    logger.info(
                                        f"[TIMEOUT_MONITOR_AWAITING] Command awaiting input: {awaiting}"
                                    )
                                    with self._lock:
                                        if command_id in self._commands:
                                            cmd.status = CommandStatus.AWAITING_INPUT
                                            cmd.awaiting_input_reason = awaiting
                                    return

                                # Check for sentinel if one was used
                                if cmd.sentinel and cmd.sentinel in cmd.stdout:
                                    logger.info(
                                        "[TIMEOUT_MONITOR_SENTINEL] Sentinel detected - command complete"
                                    )
                                    clean_output = self._session_manager._strip_ansi(
                                        cmd.stdout
                                    )
                                    sentinel_pattern = re.compile(
                                        re.escape(cmd.sentinel) + r"(\d+)"
                                    )
                                    match = sentinel_pattern.search(clean_output)
                                    if match:
                                        exit_code = int(match.group(1))
                                        final_output = clean_output[: match.start()]
                                        with self._lock:
                                            if command_id in self._commands:
                                                cmd.stdout = final_output.strip()
                                                cmd.status = CommandStatus.COMPLETED
                                                cmd.exit_code = exit_code
                                                cmd.end_time = datetime.now()
                                        return

                                # Check for prompt completion
                                clean_output = self._session_manager._strip_ansi(
                                    cmd.stdout
                                )
                                is_complete, cleaned_output = (
                                    self._session_manager._check_prompt_completion(
                                        session_key, cmd.stdout, clean_output
                                    )
                                )
                                if is_complete:
                                    logger.info(
                                        "[TIMEOUT_MONITOR_COMPLETE] Prompt detected - command complete"
                                    )

                                    # Try to retrieve actual exit code
                                    exit_code = self._retrieve_exit_code(
                                        cmd.shell, session_key
                                    )

                                    with self._lock:
                                        if command_id in self._commands:
                                            cmd.stdout = cleaned_output
                                            cmd.status = CommandStatus.COMPLETED
                                            cmd.exit_code = exit_code
                                            cmd.end_time = datetime.now()
                                    return
                    else:
                        # No data available - check if we've been idle long enough
                        elapsed_idle = time.time() - last_recv_time
                        if elapsed_idle > idle_timeout and (
                            cmd.stdout or cmd.output_chunks
                        ):
                            # Update stdout before checks to avoid O(N^2) concatenation
                            with self._lock:
                                if command_id in self._commands and cmd.output_chunks:
                                    cmd.stdout += "".join(cmd.output_chunks)
                                    cmd.output_chunks = []

                            # Check for sentinel if one was used
                            if cmd.sentinel and cmd.sentinel in cmd.stdout:
                                logger.info(
                                    "[TIMEOUT_MONITOR_IDLE_SENTINEL] Sentinel detected - command complete"
                                )
                                clean_output = self._session_manager._strip_ansi(
                                    cmd.stdout
                                )
                                sentinel_pattern = re.compile(
                                    re.escape(cmd.sentinel) + r"(\d+)"
                                )
                                match = sentinel_pattern.search(clean_output)
                                if match:
                                    exit_code = int(match.group(1))
                                    final_output = clean_output[: match.start()]
                                    with self._lock:
                                        if command_id in self._commands:
                                            cmd.stdout = final_output.strip()
                                            cmd.status = CommandStatus.COMPLETED
                                            cmd.exit_code = exit_code
                                            cmd.end_time = datetime.now()
                                    return

                            # Check one more time for prompt
                            clean_output = self._session_manager._strip_ansi(cmd.stdout)

                            is_complete, cleaned_output = (
                                self._session_manager._check_prompt_completion(
                                    session_key, cmd.stdout, clean_output
                                )
                            )
                            if is_complete:
                                logger.info(
                                    "[TIMEOUT_MONITOR_IDLE_COMPLETE] Idle timeout with prompt - command complete"
                                )

                                # Try to retrieve actual exit code
                                exit_code = self._retrieve_exit_code(
                                    cmd.shell, session_key
                                )

                                with self._lock:
                                    if command_id in self._commands:
                                        cmd.stdout = cleaned_output
                                        cmd.status = CommandStatus.COMPLETED
                                        cmd.exit_code = exit_code
                                        cmd.end_time = datetime.now()
                                return

                        time.sleep(0.1)
                except Exception as recv_error:
                    logger.error(
                        f"[TIMEOUT_MONITOR_RECV_ERROR] Error receiving data: {recv_error}"
                    )
                    break
        except Exception as e:
            logger.error(
                f"[TIMEOUT_MONITOR_ERROR] Error in timeout monitoring: {e}",
                exc_info=True,
            )
            with self._lock:
                if command_id in self._commands:
                    cmd.status = CommandStatus.FAILED
                    cmd.stderr = f"Error during background monitoring: {e}"
                    cmd.end_time = datetime.now()

        # If we reached max timeout, mark as completed with what we have
        logger.warning(
            f"[TIMEOUT_MONITOR_MAX] Command {command_id} reached max monitoring time"
        )
        with self._lock:
            if command_id in self._commands and cmd.status == CommandStatus.RUNNING:
                # Merge any pending chunks before completing
                if cmd.output_chunks:
                    cmd.stdout += "".join(cmd.output_chunks)
                    cmd.output_chunks = []
                cmd.status = CommandStatus.COMPLETED
                cmd.exit_code = 124
                cmd.stderr = f"Command exceeded maximum monitoring time ({max_additional_timeout}s after initial timeout)"
                cmd.end_time = datetime.now()

    def _continue_monitoring_shell_background(
        self, command_id: str, cmd: RunningCommand
    ) -> None:
        """Background task to monitor shell output after input has been sent.

        Updates command status when completion is detected.
        Runs in background thread pool, does not block caller.
        """
        logger = self.logger.getChild("continue_monitoring_bg")
        logger.info(f"[BG_MONITOR_START] command_id={command_id}")

        max_additional_timeout = self.config.background_monitor_max_timeout
        idle_timeout = self.config.normal_idle_timeout
        last_recv_time = time.time()
        start_time = time.time()

        # Seed the buffer with output collected before monitoring started,
        # so a spill file really does hold the whole stream.
        output_buffer = OutputBuffer(spill_dir=self.config.log_dir)
        output_buffer.add_chunk(cmd.stdout)

        last_log_time = 0.0
        poll_count = 0

        try:
            while time.time() - start_time < max_additional_timeout:
                poll_count += 1
                # Check cancellation signal
                if cmd.monitoring_cancelled.is_set():
                    logger.info(
                        f"[BG_MONITOR_CANCELLED] Monitoring cancelled for {command_id}"
                    )
                    return

                try:
                    if cmd.shell.recv_ready():
                        chunk = cmd.shell.recv(65535).decode("utf-8", errors="replace")
                        if chunk:
                            # Feed to terminal emulator
                            self._session_manager._feed_emulator(cmd.session_key, chunk)

                            # Apply output limiting
                            chunk_to_add, should_continue = output_buffer.add_chunk(
                                chunk
                            )

                            with self._lock:
                                if command_id in self._commands:
                                    cmd.output_chunks.append(chunk_to_add)

                            if not should_continue:
                                logger.warning(
                                    f"[BG_MONITOR_LIMIT] Output limit reached for {command_id}"
                                )
                                with self._lock:
                                    if command_id in self._commands:
                                        # Ensure stdout is updated before failing
                                        if cmd.output_chunks:
                                            cmd.stdout += "".join(cmd.output_chunks)
                                            cmd.output_chunks = []
                                        cmd.status = CommandStatus.FAILED
                                        cmd.stderr += f"\n{output_buffer.limit_message}"
                                        cmd.end_time = datetime.now()
                                return

                            last_recv_time = time.time()

                            # Rate limit logging (max once per second unless large chunk)
                            if len(chunk) > 1000 or (time.time() - last_log_time) > 1.0:
                                logger.debug(
                                    f"[BG_MONITOR_RECV] Received {len(chunk)} bytes: {chunk[:100]!r}"
                                )
                                last_log_time = time.time()

                            # Optimization: During active output, we primarily want to update stdout
                            # so get_command_status shows progress. We only check for prompts/input
                            # if the chunk looks like it might contain one (e.g. ends with prompt-like char)
                            # or if we have a lot of new data.
                            should_check = False
                            if len(chunk) < 100:  # Small chunks often contain prompts
                                stripped_chunk = self._session_manager._strip_ansi(
                                    chunk
                                )
                                if (
                                    stripped_chunk
                                    and stripped_chunk.strip()
                                    and stripped_chunk.strip()[-1]
                                    in ("$", "#", ">", "%", ":", "?")
                                ):
                                    should_check = True

                            # Also check if we have accumulated a lot of data since last check
                            # and periodically update stdout so users see progress
                            if not should_check and (
                                len(chunk) > 16384 or poll_count % 50 == 0
                            ):
                                should_check = True

                            if should_check:
                                # Update stdout before checks to avoid O(N^2) concatenation in every loop
                                with self._lock:
                                    if (
                                        command_id in self._commands
                                        and cmd.output_chunks
                                    ):
                                        cmd.stdout += "".join(cmd.output_chunks)
                                        cmd.output_chunks = []

                                # Check for sentinel if one was used
                                if cmd.sentinel and cmd.sentinel in cmd.stdout:
                                    logger.info(
                                        "[BG_MONITOR_SENTINEL] Sentinel detected - command complete"
                                    )
                                    clean_output = self._session_manager._strip_ansi(
                                        cmd.stdout
                                    )
                                    sentinel_pattern = re.compile(
                                        re.escape(cmd.sentinel) + r"(\d+)"
                                    )
                                    match = sentinel_pattern.search(clean_output)
                                    if match:
                                        exit_code = int(match.group(1))
                                        final_output = clean_output[: match.start()]
                                        with self._lock:
                                            if command_id in self._commands:
                                                cmd.stdout = final_output.strip()
                                                cmd.status = CommandStatus.COMPLETED
                                                cmd.exit_code = exit_code
                                                cmd.end_time = datetime.now()
                                        return

                                # Check for completion
                                clean_output = self._session_manager._strip_ansi(
                                    cmd.stdout
                                )

                                is_complete, cleaned_output = (
                                    self._session_manager._check_prompt_completion(
                                        cmd.session_key, cmd.stdout, clean_output
                                    )
                                )

                                if is_complete:
                                    logger.info(
                                        "[BG_MONITOR_COMPLETE] Prompt detected - command complete"
                                    )
                                    with self._lock:
                                        if command_id in self._commands:
                                            cmd.stdout = cleaned_output
                                            cmd.status = CommandStatus.COMPLETED
                                            cmd.end_time = datetime.now()
                                    return

                                # Check for interactive prompts
                                awaiting = self._session_manager._detect_awaiting_input(
                                    cmd.stdout, cmd.session_key
                                )
                                if awaiting:
                                    logger.info(
                                        f"[BG_MONITOR_AWAITING] Command awaiting input: {awaiting}"
                                    )
                                    with self._lock:
                                        if command_id in self._commands:
                                            cmd.status = CommandStatus.AWAITING_INPUT
                                            cmd.awaiting_input_reason = awaiting
                                    return
                        else:
                            # Rate limit empty chunk logging significantly (every 5 seconds)
                            if (time.time() - last_log_time) > 5.0:
                                logger.debug(
                                    "[BG_MONITOR_EMPTY] recv() returned empty chunk"
                                )
                                last_log_time = time.time()
                    else:
                        # No data available - check if we've timed out from inactivity
                        elapsed_idle = time.time() - last_recv_time
                        if elapsed_idle > idle_timeout:
                            # Update stdout before checks to avoid O(N^2) concatenation
                            with self._lock:
                                if command_id in self._commands and cmd.output_chunks:
                                    cmd.stdout += "".join(cmd.output_chunks)
                                    cmd.output_chunks = []

                            # Check for sentinel if one was used
                            if cmd.sentinel and cmd.sentinel in cmd.stdout:
                                logger.info(
                                    "[BG_MONITOR_IDLE_SENTINEL] Sentinel detected - command complete"
                                )
                                clean_output = self._session_manager._strip_ansi(
                                    cmd.stdout
                                )
                                sentinel_pattern = re.compile(
                                    re.escape(cmd.sentinel) + r"(\d+)"
                                )
                                match = sentinel_pattern.search(clean_output)
                                if match:
                                    exit_code = int(match.group(1))
                                    final_output = clean_output[: match.start()]
                                    with self._lock:
                                        if command_id in self._commands:
                                            cmd.stdout = final_output.strip()
                                            cmd.status = CommandStatus.COMPLETED
                                            cmd.exit_code = exit_code
                                            cmd.end_time = datetime.now()
                                    break

                            # Check for prompt completion as well
                            clean_output = self._session_manager._strip_ansi(cmd.stdout)
                            is_complete, cleaned_output = (
                                self._session_manager._check_prompt_completion(
                                    cmd.session_key, cmd.stdout, clean_output
                                )
                            )

                            if is_complete:
                                logger.info(
                                    "[BG_MONITOR_IDLE_COMPLETE] Prompt detected - command complete"
                                )
                                with self._lock:
                                    if command_id in self._commands:
                                        cmd.stdout = cleaned_output
                                        cmd.status = CommandStatus.COMPLETED
                                        cmd.end_time = datetime.now()
                                break

                            logger.info(
                                f"[BG_MONITOR_IDLE_TIMEOUT] Idle timeout ({elapsed_idle:.1f}s) - command complete"
                            )

                            # Update command status to completed (fallback)
                            with self._lock:
                                if command_id in self._commands:
                                    cmd.status = CommandStatus.COMPLETED
                                    cmd.end_time = datetime.now()
                            break

                        time.sleep(0.1)
                except Exception as recv_error:
                    logger.error(
                        f"[BG_MONITOR_RECV_ERROR] Error receiving data: {recv_error}"
                    )
                    break
        except Exception as e:
            logger.error(
                f"[BG_MONITOR_ERROR] Error in background monitoring: {e}", exc_info=True
            )
            with self._lock:
                if command_id in self._commands:
                    cmd.status = CommandStatus.FAILED
                    cmd.stderr = str(e)
                    cmd.end_time = datetime.now()

    def list_running_commands(self) -> list[dict]:
        """List all running async commands."""
        logger = self.logger.getChild("list_running")
        with self._lock:
            running_list = [
                {
                    "command_id": cmd.command_id,
                    "session_key": cmd.session_key,
                    "command": cmd.command,
                    "status": cmd.status.value,
                    "start_time": cmd.start_time.isoformat(),
                }
                for cmd in self._commands.values()
                if cmd.status == CommandStatus.RUNNING
            ]
            logger.info(f"Found {len(running_list)} running commands.")
            return running_list

    def list_command_history(self, limit: int = 50) -> list[dict]:
        """List recent command history (completed, failed, interrupted)."""
        logger = self.logger.getChild("list_history")
        with self._lock:
            completed = [
                {
                    "command_id": cmd.command_id,
                    "session_key": cmd.session_key,
                    "command": cmd.command,
                    "status": cmd.status.value,
                    "exit_code": cmd.exit_code,
                    "start_time": cmd.start_time.isoformat(),
                    "end_time": cmd.end_time.isoformat() if cmd.end_time else None,
                }
                for cmd in self._commands.values()
                if cmd.status != CommandStatus.RUNNING
            ]
            # Sort by end time, most recent first
            completed.sort(key=lambda x: x["end_time"] or "", reverse=True)
            result = completed[:limit]
            logger.info(
                f"Returning {len(result)} commands from history (limit: {limit})."
            )
            return result

    def clear_session_commands(self, session_key: str):
        """Clear all commands for a specific session.

        Args:
            session_key: Session identifier (e.g., "user@host:22")
        """
        logger = self.logger.getChild("clear_session_commands")
        with self._lock:
            commands_to_remove = [
                cmd_id
                for cmd_id, cmd in self._commands.items()
                if cmd.session_key == session_key
            ]

            if commands_to_remove:
                logger.info(
                    f"Clearing {len(commands_to_remove)} commands for session {session_key}"
                )
                for cmd_id in commands_to_remove:
                    cmd = self._commands[cmd_id]
                    # Signal cancellation to background threads
                    cmd.monitoring_cancelled.set()

                    # Mark as interrupted if still running/awaiting
                    if cmd.status in (
                        CommandStatus.RUNNING,
                        CommandStatus.AWAITING_INPUT,
                    ):
                        cmd.status = CommandStatus.INTERRUPTED
                        cmd.end_time = datetime.now()
                        logger.debug(f"Marked command {cmd_id} as interrupted")
                    del self._commands[cmd_id]
                logger.info(
                    f"Cleared {len(commands_to_remove)} commands for session {session_key}"
                )
            else:
                logger.debug(f"No commands found for session {session_key}")

    def clear_all_commands(self):
        """Clear all commands from all sessions."""
        logger = self.logger.getChild("clear_all_commands")
        with self._lock:
            count = len(self._commands)
            if count > 0:
                logger.info(f"Clearing {count} commands from all sessions")
                for cmd in self._commands.values():
                    # Signal cancellation
                    cmd.monitoring_cancelled.set()

                    if cmd.status in (
                        CommandStatus.RUNNING,
                        CommandStatus.AWAITING_INPUT,
                    ):
                        cmd.status = CommandStatus.INTERRUPTED
                        cmd.end_time = datetime.now()
                self._commands.clear()
                logger.info(f"Cleared {count} commands")
            else:
                logger.debug("No commands to clear")

    def shutdown(self):
        """Shut down the underlying thread pool executor and clear running commands."""
        logger = self.logger.getChild("shutdown")
        logger.info("Shutting down command executor pool")
        self._executor.shutdown(wait=False, cancel_futures=True)

        with self._lock:
            running_count = sum(
                1
                for cmd in self._commands.values()
                if cmd.status in (CommandStatus.RUNNING, CommandStatus.AWAITING_INPUT)
            )
            if running_count > 0:
                logger.info(
                    f"Clearing {running_count} active commands from the registry due to shutdown."
                )

            # Signal cancellation to all commands
            for cmd in self._commands.values():
                cmd.monitoring_cancelled.set()

            self._commands.clear()

    # ------------------------------------------------------------------
    # Execution internals (moved out of SSHSessionManager)
    # ------------------------------------------------------------------

    def _execute_sudo_command_internal(
        self,
        client: paramiko.SSHClient,
        command: str,
        sudo_password: str,
        timeout: int = 30,
        *,
        cancel_event: threading.Event | None = None,
        output_buffer: OutputBuffer | None = None,
    ) -> tuple[str, str, int]:
        """Execute a sudo command using the persistent shell, handling password prompts.

        Uses the persistent shell from the session to maintain state and benefit from
        prompt detection.
        """
        logger = self._sm.logger.getChild("sudo_command")

        # Get session key for this client
        # We need to derive the session key from the client
        # Find the session key that matches this client
        session_key = None
        with self._sm.registry.lock:
            for key, sess_client in self._sm.registry.sessions.items():
                if sess_client == client:
                    session_key = key
                    break

        if not session_key:
            logger.error("Could not find session key for client")
            return "", "Could not find session for sudo command", 1

        try:
            timeout = min(timeout, self._sm.MAX_COMMAND_TIMEOUT)

            # Ensure command starts with sudo
            if not command.strip().startswith("sudo"):
                command = f"sudo {command}"

            # Get the persistent shell
            shell = self._sm._get_or_create_shell(session_key, client)
            shell.settimeout(timeout)

            # Send the command
            shell.send((command + "\n").encode("utf-8"))
            time.sleep(0.5)

            output_buffer = output_buffer or OutputBuffer(
                spill_dir=self.config.log_dir
            )
            raw_output = ""
            password_sent = False
            start_time = time.time()
            last_recv_time = start_time
            idle_timeout = 2.0
            max_idle_checks = 50  # Max 5 seconds of idle checking (50 * 0.1s)
            idle_check_count = 0

            while time.time() - start_time < timeout:
                if cancel_event is not None and cancel_event.is_set():
                    logger.info("Execution cancelled by caller")
                    return raw_output, "Command interrupted", 130
                if shell.recv_ready():
                    chunk = shell.recv(4096).decode("utf-8", errors="ignore")
                    logger.debug(f"Received chunk: {chunk!r}")
                    self._sm._feed_emulator(session_key, chunk)
                    last_recv_time = time.time()
                    idle_check_count = 0  # Reset idle check counter on new data
                    limited_chunk, should_continue = output_buffer.add_chunk(chunk)
                    raw_output += limited_chunk

                    # Check for password prompt
                    if not password_sent and re.search(
                        r"\[sudo\] password|password for", raw_output, re.IGNORECASE
                    ):
                        logger.debug("Detected sudo password prompt, sending password")
                        shell.send(f"{sudo_password}\n".encode())
                        password_sent = True
                        time.sleep(0.3)
                        # Clear output buffer to avoid re-detecting the prompt
                        raw_output = ""
                        continue

                    if not should_continue:
                        return (
                            output_buffer.render(raw_output),
                            output_buffer.limit_message,
                            1,
                        )

                    # Check for interactive prompts (SSH host key, etc.) BEFORE checking completion
                    awaiting = self._sm._detect_awaiting_input(raw_output, session_key)
                    if awaiting:
                        logger.info(f"Sudo command waiting for input: {awaiting}")
                        return raw_output, f"Command requires input: {awaiting}", 1

                    # Check for command completion using prompt detection
                    clean_output = self._sm._strip_ansi(raw_output)
                    is_complete, cleaned_output = self._sm._check_prompt_completion(
                        session_key, raw_output, clean_output
                    )

                    if is_complete:
                        logger.debug("Sudo command completed (prompt detected)")
                        return cleaned_output, "", 0
                else:
                    # Check idle timeout
                    if raw_output and (time.time() - last_recv_time) > idle_timeout:
                        idle_check_count += 1

                        # If we've been idle-checking too long without finding a prompt, break
                        if idle_check_count > max_idle_checks:
                            logger.warning(
                                f"Sudo command exceeded max idle checks ({max_idle_checks}), assuming still running"
                            )
                            break

                        # Check for interactive prompts during idle
                        awaiting = self._sm._detect_awaiting_input(raw_output, session_key)
                        if awaiting:
                            logger.info(
                                f"Sudo command waiting for input (idle): {awaiting}"
                            )
                            return raw_output, f"Command requires input: {awaiting}", 1

                        logger.debug("Sudo command idle timeout, checking completion")
                        clean_output = self._sm._strip_ansi(raw_output)
                        is_complete, cleaned_output = self._sm._check_prompt_completion(
                            session_key, raw_output, clean_output
                        )
                        if is_complete:
                            logger.debug("Sudo command completed (idle timeout)")
                            return cleaned_output, "", 0
                        # If not complete but idle, wait a bit more

                    time.sleep(0.1)

            # Timeout reached
            logger.warning(f"Sudo command timed out after {timeout}s")
            return raw_output.strip(), f"Command timed out after {timeout} seconds", 124

        except paramiko.SSHException as exc:
            logger.error(f"SSH error during sudo command: {exc}")
            return "", f"SSH error: {exc}", 1
        except Exception as exc:
            logger.logger.error(f"Error executing sudo command: {exc}", exc_info=True)
            return "", f"Error executing sudo command: {exc}", 1


    def _execute_standard_command_internal(
        self,
        client: paramiko.SSHClient,
        command: str,
        timeout: int,
        session_key: str,
        *,
        cancel_event: threading.Event | None = None,
        output_buffer: OutputBuffer | None = None,
    ) -> tuple[str, str, int, str | None, str | None]:
        """Execute command with natural completion detection and interactive prompt detection.

        Returns: (stdout, stderr, exit_code, awaiting_input_reason, sentinel)
        - awaiting_input_reason is None if complete, or a string describing what input is needed
        - sentinel is the marker string used for Unix completion, or None
        """
        logger = self._sm.logger.getChild("standard_command")
        command = self._sm._maybe_rewrite_mikrotik_command(session_key, command)

        # Check if this command will change the shell context
        context_changing = self._sm._is_context_changing_command(command)
        if context_changing:
            logger.info(f"Detected context-changing command: {command}")

        sentinel: str | None = None
        try:
            shell = self._sm._get_or_create_shell(session_key, client)
            shell.settimeout(timeout)

            with self._sm.registry.lock:
                self._sm.registry.active_commands[session_key] = shell

            # Clear any pending output to avoid matching stale prompts
            if shell.recv_ready():
                try:
                    while shell.recv_ready():
                        shell.recv(4096)
                except Exception:
                    pass

            # Check shell type to decide on sentinel usage
            shell_type = self._sm.registry.shell_types.get(session_key, "unknown")
            logger.debug(f"Shell type for {session_key}: {shell_type}")
            is_unix = shell_type == "unix_shell"

            # Use sentinel only for Unix-like shells and non-interactive commands
            sentinel = None
            command_to_send = command
            # Skip sentinel if command appears to read from stdin (like 'read' command)
            is_interactive_cmd = re.search(r"\bread\b", command)
            if is_unix and not is_interactive_cmd:
                marker = f"__MCP_CMD_{uuid.uuid4().hex[:8]}__"
                command_to_send = self._sm._build_command_with_sentinel(command, marker, "")
                sentinel = marker
                logger.debug(f"Using sentinel marker: {sentinel}")

            logger.info(f"Executing command on {session_key}: {command}")
            shell.send((command_to_send + "\n").encode("utf-8"))
            time.sleep(0.3)

            output_buffer = output_buffer or OutputBuffer(
                spill_dir=self.config.log_dir
            )
            raw_output = ""
            start_time = time.time()
            last_recv_time = start_time

            # Package managers need longer idle timeout due to database operations
            command_lower = command.lower().strip()
            is_package_manager = any(
                [
                    re.search(
                        r"\bpkg\s+(install|upgrade|update|remove|delete)", command_lower
                    ),
                    re.search(
                        r"\bapt(?:-get)?\s+(install|upgrade|update|dist-upgrade|full-upgrade|remove|purge)",
                        command_lower,
                    ),
                    re.search(
                        r"\b(dnf|yum|zypper)\s+(install|upgrade|update|remove|erase)",
                        command_lower,
                    ),
                    re.search(
                        r"\bpacman\s+(-[SsRr]\b|--sync\b|--remove\b|install|upgrade|update|remove)",
                        command_lower,
                    ),
                    re.search(
                        r"\bapk\s+(add|install|upgrade|update|del|delete)",
                        command_lower,
                    ),
                    re.search(
                        r"\bbrew\s+(install|upgrade|update|uninstall|remove)",
                        command_lower,
                    ),
                ]
            )
            # Use 10 second idle timeout for package managers, 2 seconds for others
            idle_timeout = (
                self._sm.config.package_manager_idle_timeout
                if is_package_manager
                else self._sm.config.normal_idle_timeout
            )
            if is_package_manager:
                logger.info(
                    f"Detected package manager command, using extended idle timeout of {idle_timeout}s"
                )

            seen_command_echo = False
            echo_end_pos: int | None = None
            # Ensure prompt pattern exists as fallback
            self._sm._ensure_prompt_pattern(session_key, client, shell=shell)
            consecutive_misses = 0  # Track consecutive prompt detection failures

            output_buffer = output_buffer or OutputBuffer(
                spill_dir=self.config.log_dir
            )
            raw_output_chunks = []

            while time.time() - start_time < timeout:
                if cancel_event is not None and cancel_event.is_set():
                    logger.info("Execution cancelled by caller")
                    return (
                        self._sm._strip_sentinel(
                            "".join(raw_output_chunks), sentinel
                        ).strip(),
                        "Command interrupted",
                        130,
                        None,
                        sentinel,
                    )
                if shell.recv_ready():
                    chunk = shell.recv(4096).decode("utf-8", errors="ignore")
                    logger.debug(f"Received chunk: {chunk!r}")
                    self._sm._feed_emulator(session_key, chunk)
                    last_recv_time = time.time()
                    limited_chunk, should_continue = output_buffer.add_chunk(chunk)
                    raw_output_chunks.append(limited_chunk)

                    # Optimization: We primarily check for prompts/input on small chunks
                    # or after data accumulation.
                    should_check = False
                    if len(chunk) < 100:  # Small chunks often contain prompts
                        stripped_chunk = self._sm._strip_ansi(chunk)
                        if (
                            stripped_chunk
                            and stripped_chunk.strip()
                            and stripped_chunk.strip()[-1]
                            in ("$", "#", ">", "%", ":", "?")
                        ):
                            should_check = True

                    if not should_check and (len(raw_output_chunks) % 20 == 0):
                        should_check = True

                    # ALWAYS update raw_output if we need to check echo, limit, or sentinel
                    # to ensure we don't use stale data.
                    if should_check or not seen_command_echo or sentinel:
                        raw_output = "".join(raw_output_chunks)

                    if not seen_command_echo and "\n" in raw_output:
                        seen_command_echo = True
                        # Record end of echo line so prompt detection only looks after it
                        clean_snapshot = self._sm._strip_ansi(raw_output)
                        newline_idx = clean_snapshot.find("\n")
                        if newline_idx != -1:
                            echo_end_pos = newline_idx + 1

                    if not should_continue:
                        logger.warning("Output limit reached")
                        return (
                            output_buffer.render("".join(raw_output_chunks)),
                            output_buffer.limit_message,
                            1,
                            None,
                            sentinel,
                        )

                    if should_check:
                        # Check for interactive prompts BEFORE checking for completion
                        awaiting = self._sm._detect_awaiting_input(raw_output, session_key)
                        if awaiting:
                            # Only treat as awaiting input after a brief idle and if prompt isn't present
                            # Exception: Pagers should be handled immediately to keep stream flowing
                            if (
                                awaiting == "pager"
                                or (time.time() - last_recv_time) > 0.2
                            ):
                                clean_output = self._sm._strip_ansi(raw_output)
                                tail_start = echo_end_pos or 0
                                tail_clean = clean_output[tail_start:]
                                is_complete, _ = self._sm._check_prompt_completion(
                                    session_key, raw_output, tail_clean
                                )
                                if not is_complete:
                                    logger.info(
                                        f"Detected interactive prompt: {awaiting}"
                                    )
                                    # Automatically handle pagers by sending 'q' to quit
                                    if awaiting == "pager":
                                        logger.info(
                                            "Automatically handling pager - sending 'q' to quit"
                                        )

                                        # Strip MikroTik pager prompt from output to avoid agent confusion
                                        # Match raw output as detection does
                                        raw_output = re.sub(
                                            r"--\s*\[Q quit\|D dump\|.*?\]\s*$",
                                            "",
                                            raw_output,
                                        )
                                        # Update chunks
                                        raw_output_chunks = [raw_output]

                                        shell.send(b"q")
                                        # Wait for pager to exit and shell prompt to appear
                                        # Don't just continue - actively wait for the prompt
                                        pager_exit_start = time.time()
                                        pager_exit_timeout = 3.0
                                        while (
                                            time.time() - pager_exit_start
                                            < pager_exit_timeout
                                        ):
                                            time.sleep(0.1)
                                            if shell.recv_ready():
                                                chunk = shell.recv(4096).decode(
                                                    "utf-8", errors="ignore"
                                                )
                                                logger.debug(
                                                    f"Received chunk (pager): {chunk!r}"
                                                )
                                                self._sm._feed_emulator(session_key, chunk)
                                                limited_chunk, should_continue = (
                                                    output_buffer.add_chunk(chunk)
                                                )
                                                raw_output_chunks.append(limited_chunk)
                                                raw_output = "".join(raw_output_chunks)
                                                if not should_continue:
                                                    return (
                                                        output_buffer.render(raw_output),
                                                        output_buffer.limit_message,
                                                        1,
                                                        None,
                                                        sentinel,
                                                    )
                                                # Check if we now have the shell prompt
                                                clean_output = self._sm._strip_ansi(
                                                    raw_output
                                                )
                                                tail_start = echo_end_pos or 0
                                                tail_clean = clean_output[tail_start:]
                                                is_complete, cleaned_output = (
                                                    self._sm._check_prompt_completion(
                                                        session_key,
                                                        raw_output,
                                                        tail_clean,
                                                    )
                                                )
                                                if is_complete:
                                                    logger.debug(
                                                        "Shell prompt detected after quitting pager"
                                                    )
                                                    return (
                                                        cleaned_output,
                                                        "",
                                                        0,
                                                        None,
                                                        sentinel,
                                                    )

                                # For other types of input (password, etc.), return and let agent handle
                                return (
                                    self._sm._strip_sentinel(raw_output, sentinel),
                                    "",
                                    0,
                                    awaiting,
                                    sentinel,
                                )

                    # Check for sentinel (Unix shells)
                    if sentinel and sentinel in raw_output:
                        logger.debug("Sentinel detected")
                        # Extract exit code and clean output
                        clean_output = self._sm._strip_ansi(raw_output)

                        # Find sentinel and exit code
                        # Pattern: marker + digits
                        sentinel_pattern = re.compile(re.escape(sentinel) + r"(\d+)")
                        match = sentinel_pattern.search(clean_output)
                        if match:
                            exit_code = int(match.group(1))

                            # Clean up output: remove everything from sentinel onwards using the match position
                            # This avoids truncating at the command echo which also contains the sentinel string

                            # We use clean_output for truncation to ensure accurate regex index matching
                            # match.start() is the index of the sentinel in clean_output
                            final_output = clean_output[: match.start()]

                            # We should return the clean output directly
                            return final_output.strip(), "", exit_code, None, sentinel

                    # Check for command completion using captured prompt or pattern
                    # Only check after brief idle to avoid false positives from command echo
                    # AND make sure we've seen the command echo (newline)
                    if seen_command_echo and (time.time() - last_recv_time) > 0.2:
                        clean_output = self._sm._strip_ansi(raw_output)
                        tail_start = echo_end_pos or 0
                        tail_clean = clean_output[tail_start:]

                        is_complete, cleaned_output = self._sm._check_prompt_completion(
                            session_key, raw_output, tail_clean
                        )

                        # If sentinel mode is on, we ignore simple prompt matching unless
                        # we are really sure or it's been a long time?
                        # Actually, the bug is "Cost is 10$" triggers prompt match.
                        # If we have a sentinel, "Cost is 10$" will appear, but sentinel won't.
                        # So we should IGNORE is_complete if sentinel is active and sentinel not found.
                        if sentinel and is_complete:
                            # Logic: If sentinel is used, we trust sentinel.
                            # We DO NOT return on prompt detection alone to fix the bug.
                            is_complete = False
                    else:
                        is_complete = False
                        cleaned_output = ""

                    if is_complete:
                        # Reset miss count on successful match
                        self._sm.registry.prompt_miss_count[session_key] = 0
                        consecutive_misses = 0

                        # If this was a context-changing command, recapture the prompt
                        if context_changing:
                            logger.info(
                                "Recapturing prompt after context-changing command"
                            )
                            with self._sm.registry.lock:
                                self._sm.registry.prompts.pop(session_key, None)
                            self._sm._capture_prompt(session_key, shell)

                        return cleaned_output, "", 0, None, sentinel

                    else:
                        consecutive_misses += 1

                        # If we've had too many consecutive misses, try recapturing the prompt
                        if consecutive_misses > 10:
                            miss_count = self._sm.registry.prompt_miss_count.get(session_key, 0) + 1
                            self._sm.registry.prompt_miss_count[session_key] = miss_count

                            if miss_count > 3:
                                logger.warning(
                                    f"Prompt detection failing repeatedly ({miss_count} times), recapturing for {session_key}"
                                )
                                with self._sm.registry.lock:
                                    self._sm.registry.prompts.pop(session_key, None)
                                    self._sm.registry.prompt_patterns.pop(session_key, None)

                                # Try to clear any stuck state with Ctrl+C
                                logger.info(
                                    f"Sending Ctrl+C to clear stuck state for {session_key}"
                                )
                                try:
                                    shell.send(b"\x03")
                                    time.sleep(0.5)
                                    # Clear any output from Ctrl+C
                                    if shell.recv_ready():
                                        shell.recv(4096)
                                except Exception as e:
                                    logger.warning(f"Error sending Ctrl+C: {e}")

                                # Try to recapture prompt
                                self._sm._capture_prompt(session_key, shell)
                                self._sm._ensure_prompt_pattern(
                                    session_key, client, raw_output, shell
                                )
                                consecutive_misses = 0
                                logger.info("Recaptured prompt and regenerated pattern")

                            # Nuclear option: if we've tried many times, reset the shell
                            if miss_count > 5:
                                logger.error(
                                    f"Prompt detection failed {miss_count} times for {session_key}. "
                                    f"Shell state may be corrupted. Consider closing and recreating the session."
                                )
                                # Mark the shell as needing reset by closing it
                                # The next command will create a new shell
                                try:
                                    shell.close()
                                except Exception:
                                    pass
                                if session_key in self._sm.registry.shells:
                                    del self._sm.registry.shells[session_key]
                                # Return error indicating session needs reset
                                return (
                                    self._sm._strip_sentinel(raw_output, sentinel),
                                    "Session state corrupted. The session has been reset. Please retry your command.",
                                    1,
                                    None,
                                    sentinel,
                                )
                else:
                    # No data available - check if we should timeout from inactivity
                    if raw_output and (time.time() - last_recv_time) > idle_timeout:
                        clean_output = self._sm._strip_ansi(raw_output)

                        # Check for interactive prompts BEFORE checking for completion
                        awaiting = self._sm._detect_awaiting_input(raw_output, session_key)
                        if awaiting:
                            logger.info(
                                f"Detected interactive prompt during idle timeout: {awaiting}"
                            )
                            # Automatically handle pagers by sending 'q' to quit
                            if awaiting == "pager":
                                logger.info(
                                    "Automatically handling pager during idle timeout - sending 'q' to quit"
                                )

                                # Strip MikroTik pager prompt from output to avoid agent confusion
                                raw_output = re.sub(
                                    r"--\s*\[Q quit\|D dump\|.*?\]\s*$", "", raw_output
                                )

                                shell.send(b"q")
                                # Wait for pager to exit and shell prompt to appear
                                pager_exit_start = time.time()
                                pager_exit_timeout = 3.0
                                while (
                                    time.time() - pager_exit_start < pager_exit_timeout
                                ):
                                    time.sleep(0.1)
                                    if shell.recv_ready():
                                        chunk = shell.recv(4096).decode(
                                            "utf-8", errors="ignore"
                                        )
                                        logger.debug(
                                            f"Received chunk (idle-pager): {chunk!r}"
                                        )
                                        self._sm._feed_emulator(session_key, chunk)
                                        limited_chunk, should_continue = (
                                            output_buffer.add_chunk(chunk)
                                        )
                                        raw_output += limited_chunk
                                        if not should_continue:
                                            return (
                                                output_buffer.render(raw_output),
                                                output_buffer.limit_message,
                                                1,
                                                None,
                                                sentinel,
                                            )
                                        # Check if we now have the shell prompt
                                        clean_output = self._sm._strip_ansi(raw_output)
                                        tail_start = echo_end_pos or 0
                                        tail_clean = clean_output[tail_start:]
                                        is_complete, cleaned_output = (
                                            self._sm._check_prompt_completion(
                                                session_key, raw_output, tail_clean
                                            )
                                        )
                                        if is_complete:
                                            logger.debug(
                                                "Shell prompt detected after quitting pager (idle)"
                                            )
                                            return cleaned_output, "", 0, None, sentinel

                                # Reset idle timer and continue collecting
                                last_recv_time = time.time()
                                logger.debug(
                                    "Pager quit during idle, continuing to wait for shell prompt"
                                )
                                continue
                            # For other types of input (password, etc.), return and let agent handle
                            # Only return awaiting input if prompt isn't already visible
                            tail_start = echo_end_pos or 0
                            tail_clean = clean_output[tail_start:]
                            is_complete, _ = self._sm._check_prompt_completion(
                                session_key, raw_output, tail_clean
                            )
                            if not is_complete:
                                return (
                                    self._sm._strip_sentinel(raw_output, sentinel),
                                    "",
                                    0,
                                    awaiting,
                                    sentinel,
                                )

                        # Only complete on idle timeout if we detect a prompt
                        tail_start = echo_end_pos or 0
                        tail_clean = clean_output[tail_start:]
                        is_complete, cleaned_output = self._sm._check_prompt_completion(
                            session_key, raw_output, tail_clean
                        )
                        if is_complete:
                            logger.debug(
                                "Prompt found in cleaned output during idle timeout"
                            )

                            # If sentinel is active, verify sentinel presence even on idle timeout
                            if sentinel:
                                if sentinel in raw_output:
                                    # Sentinel found, we can proceed
                                    # Logic handled in main loop, but here we are in idle block
                                    # Let main loop handle it in next iteration (idle doesn't break loop unless we return)
                                    pass
                                else:
                                    # Sentinel NOT found, but prompt found.
                                    # This is the ambiguous case.
                                    # If we return here, we risk the bug.
                                    # If we don't, we risk hanging if sentinel is lost.
                                    # Given the bug report, we MUST prioritize avoiding false positives.
                                    # So we ignore the prompt if sentinel is missing.
                                    logger.debug(
                                        "Sentinel active but not found - ignoring prompt detection on idle"
                                    )
                                    is_complete = False

                        if is_complete:
                            # If this was a context-changing command, recapture the prompt
                            if context_changing:
                                logger.info(
                                    "Recapturing prompt after context-changing command (idle timeout)"
                                )
                                with self._sm.registry.lock:
                                    self._sm.registry.prompts.pop(session_key, None)
                                self._sm._capture_prompt(session_key, shell)

                            return cleaned_output, "", 0, None, sentinel
                    time.sleep(0.1)

            logger.warning(f"Command timed out after {timeout}s")
            # Final join to ensure all output is returned
            raw_output = "".join(raw_output_chunks)
            return (
                self._sm._strip_sentinel(raw_output, sentinel).strip(),
                f"Command timed out after {timeout} seconds",
                124,
                None,
                sentinel,
            )

        except Exception as exc:
            logger.logger.error(f"Error executing command: {exc}", exc_info=True)
            if session_key in self._sm.registry.shells:
                try:
                    self._sm.registry.shells[session_key].close()
                except Exception:
                    pass
                del self._sm.registry.shells[session_key]
            return "", f"Error: {exc}", 1, None, sentinel
        finally:
            with self._sm.registry.lock:
                self._sm.registry.active_commands.pop(session_key, None)


    async def _execute_enable_mode_command_internal(
        self,
        client: paramiko.SSHClient,
        session_key: str,
        command: str,
        enable_password: str,
        enable_command: str,
        timeout: int,
        *,
        cancel_event: threading.Event | None = None,
        output_buffer: OutputBuffer | None = None,
    ) -> tuple[str, str, int]:
        """Execute a command while the session is in enable mode using the persistent shell."""
        logger = self._sm.logger.getChild("enable_mode_command")

        try:
            # Get the persistent shell for this session
            shell = self._sm._get_or_create_shell(session_key, client)
            shell.settimeout(timeout)

            # Validate enable mode state if we think we are enabled
            if self._sm.registry.enable_mode.get(session_key, False):
                # Clear pending output first
                if shell.recv_ready():
                    shell.recv(4096)

                # Check prompt
                shell.send(b"\n")
                await asyncio.sleep(0.5)

                if shell.recv_ready():
                    output = shell.recv(4096).decode("utf-8", errors="ignore")
                    clean = self._sm._strip_ansi(output).strip()
                    # Check if prompt ends with # (standard enable mode indicator)
                    # We also check if it contains '>' which usually indicates user mode
                    if (
                        clean
                        and not clean.endswith("#")
                        and (clean.endswith(">") or ">" in clean.splitlines()[-1])
                    ):
                        logger.warning(
                            f"Enable mode validation failed. Prompt '{clean}' does not appear to be enable mode. Re-entering enable mode."
                        )
                        self._sm.registry.enable_mode[session_key] = False

            # Enter enable mode if not already in it
            if not self._sm.registry.enable_mode.get(session_key, False):
                success, message = await self._sm._enter_enable_mode(
                    session_key, client, enable_password, enable_command
                )
                if not success:
                    return "", f"Failed to enter enable mode: {message}", 1

            # Clear any pending output
            if shell.recv_ready():
                shell.recv(4096)

            # Send the command
            shell.send(f"{command}\n".encode())
            await asyncio.sleep(0.5)

            output_buffer = output_buffer or OutputBuffer(
                spill_dir=self.config.log_dir
            )
            raw_output = ""
            start_time = time.time()
            last_output_time = time.time()
            idle_timeout = 2.0  # Consider command complete after 2 seconds of no output

            while time.time() - start_time < timeout:
                if cancel_event is not None and cancel_event.is_set():
                    logger.info("Execution cancelled by caller")
                    return raw_output, "Command interrupted", 130
                if shell.recv_ready():
                    chunk = shell.recv(4096).decode("utf-8", errors="ignore")
                    logger.debug(f"Received chunk: {chunk!r}")
                    self._sm._feed_emulator(session_key, chunk)
                    limited_chunk, should_continue = output_buffer.add_chunk(chunk)
                    raw_output += limited_chunk
                    last_output_time = time.time()

                    if not should_continue:
                        break

                    # Use proper prompt detection instead of naive character checking
                    clean_output = re.sub(r"\x1b\[[0-9;]*[mGKHF]", "", raw_output)
                    is_complete, _ = self._sm._check_prompt_completion(
                        session_key, raw_output, clean_output
                    )
                    if is_complete:
                        logger.debug("Prompt detected - command complete")
                        break
                else:
                    # No data available - check if we've been idle long enough
                    if time.time() - last_output_time >= idle_timeout and raw_output:
                        logger.debug(
                            f"Idle timeout reached after {idle_timeout}s - command appears complete"
                        )
                        break
                    await asyncio.sleep(0.1)
            else:
                return raw_output, f"Command timed out after {timeout} seconds", 124

            # Clean up the output using proper prompt detection
            clean_output = re.sub(r"\x1b\[[0-9;]*[mGKHF]", "", raw_output)
            is_complete, cleaned_output = self._sm._check_prompt_completion(
                session_key, raw_output, clean_output
            )

            # Remove the command echo (first line)
            lines = cleaned_output.split("\n")
            if len(lines) > 1 and lines[0].strip() in command:
                # First line is command echo, skip it
                output = "\n".join(lines[1:]).strip()
            else:
                output = cleaned_output.strip()

            return output, "", 0

        except Exception as exc:
            logger.logger.error(f"Enable mode command error: {exc}", exc_info=True)
            return "", f"Error executing enable mode command: {exc}", 1


