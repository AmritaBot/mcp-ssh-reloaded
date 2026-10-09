"""SSH service layer - takes api_types, does real work.

This is a bridge between the declarative api_types and the internal
SSHSessionManager engine.  Over time the engine internals can be
refactored without changing this public API.

Zero dependency on fastmcp / MCP protocol.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from .api_types import (
    CommandResult,
    CommandStatus,
    ConnectionParams,
    ErrorCategory,
    FileContent,
    ServerConfig,
    SessionInfo,
    SSHError,
)
from .datastructures import SessionDiagnostics
from .session_manager import SSHSessionManager


class SSHService:
    """Public API for SSH session operations.

    Usage::

        from mcp_ssh_reloaded import SSHService, ConnectionParams

        svc = SSHService()
        conn = ConnectionParams(host="devbox", username="me")
        r = svc.execute(conn, "uptime")
        print(r.stdout)
    """

    def __init__(
        self,
        config: ServerConfig | None = None,
        logger: logging.Logger | None = None,
    ):
        self.config: ServerConfig = config or ServerConfig()
        self.logger: logging.Logger = logger or logging.getLogger("ssh_service")
        self._engine = SSHSessionManager(config=self.config)

    #  execute

    async def execute(
        self,
        conn: ConnectionParams,
        command: str,
        *,
        timeout: int | None = None,
        sudo: bool = False,
    ) -> CommandResult:
        """Execute a command and wait for completion."""
        t0 = _now_ms()
        try:
            result = await self._engine.execute_result(
                host=conn.host,
                username=conn.username,
                command=command,
                password=conn.password,
                key_filename=conn.key_filename,
                port=conn.port,
                enable_password=conn.enable_password,
                enable_command=conn.enable_command,
                sudo_password=conn.sudo_password if sudo else None,
                timeout=timeout or self.config.default_timeout,
            )
            # No string parsing: the executor already told us what happened.
            if result.status is CommandStatus.RUNNING:
                return CommandResult(
                    stdout=result.stdout,
                    stderr=result.stderr,
                    exit_code=0,
                    status=CommandStatus.RUNNING,
                    command_id=result.command_id,
                    truncated=result.truncated,
                    spilled_path=result.spilled_path,
                )
            if result.status is CommandStatus.AWAITING_INPUT:
                return CommandResult(
                    stdout=result.stdout,
                    stderr=result.stderr,
                    exit_code=0,
                    status=CommandStatus.AWAITING_INPUT,
                    command_id=result.command_id,
                    truncated=result.truncated,
                    spilled_path=result.spilled_path,
                )
            return CommandResult(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code or 0,
                duration_ms=_now_ms() - t0,
                truncated=result.truncated,
                spilled_path=result.spilled_path,
            )
        except ConnectionError as e:
            raise _to_ssh_error(e)
        except Exception as e:
            raise _to_ssh_error(e)

    #  execute async

    async def execute_async(
        self,
        conn: ConnectionParams,
        command: str,
        *,
        timeout: int = 300,
    ) -> str:
        """Start a command in background; returns a command_id."""
        return await self._engine.execute_command_async(
            host=conn.host,
            username=conn.username,
            command=command,
            password=conn.password,
            key_filename=conn.key_filename,
            port=conn.port,
            sudo_password=conn.sudo_password,
            enable_password=conn.enable_password,
            enable_command=conn.enable_command,
            timeout=timeout,
        )

    def get_command_status(self, command_id: str) -> dict[str, Any]:
        """Poll an async command."""
        return self._engine.get_command_status(command_id)

    async def send_input(self, command_id: str, text: str) -> tuple[bool, str, str]:
        """Send input to a running async command."""
        return await self._engine.send_input(command_id, text)

    def interrupt(self, command_id: str) -> tuple[bool, str]:
        """Send Ctrl+C to a running command."""
        return self._engine.interrupt_command_by_id(command_id)

    def list_running(self) -> list[dict[str, Any]]:
        """List active async commands."""
        return self._engine.list_running_commands()

    def list_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """List completed/failed/interrupted commands."""
        return self._engine.list_command_history(limit)

    #  file ops

    async def read_file(
        self,
        conn: ConnectionParams,
        path: str,
        *,
        encoding: str = "utf-8",
        max_bytes: int | None = None,
        start_line: int = 1,
        max_lines: int | None = None,
        use_sudo: bool = False,
        timeout: int | None = None,
    ) -> FileContent:
        """Read a remote file, optionally a bounded window of its lines."""
        fc = await self._engine.read_file(
            host=conn.host,
            remote_path=path,
            username=conn.username,
            password=conn.password,
            key_filename=conn.key_filename,
            port=conn.port,
            encoding=encoding,
            errors="replace",
            max_bytes=max_bytes,
            start_line=start_line,
            max_lines=max_lines,
            sudo_password=conn.sudo_password if use_sudo else None,
            use_sudo=use_sudo,
            timeout=timeout or self.config.default_timeout,
        )
        if fc.error:
            raise SSHError(
                category=ErrorCategory.PERMISSION,
                message=f"Cannot read {path}",
                detail=fc.error,
            )
        return fc

    async def write_file(
        self,
        conn: ConnectionParams,
        path: str,
        content: str,
        *,
        encoding: str = "utf-8",
        append: bool = False,
        make_dirs: bool = False,
        permissions: int | None = None,
        max_bytes: int | None = None,
        use_sudo: bool = False,
        timeout: int | None = None,
    ) -> str:
        """Write content to a remote file. Returns status message."""
        msg, stderr, exit_code = await self._engine.write_file(
            host=conn.host,
            remote_path=path,
            content=content,
            username=conn.username,
            password=conn.password,
            key_filename=conn.key_filename,
            port=conn.port,
            encoding=encoding,
            errors="strict",
            append=append,
            make_dirs=make_dirs,
            permissions=permissions,
            max_bytes=max_bytes,
            sudo_password=conn.sudo_password if use_sudo else None,
            use_sudo=use_sudo,
            timeout=timeout or self.config.default_timeout,
        )
        if exit_code != 0:
            raise SSHError(
                category=ErrorCategory.PERMISSION,
                message=f"Cannot write {path}",
                detail=stderr or msg,
            )
        return msg

    #  session lifecycle

    async def list_sessions(self) -> list[SessionInfo]:
        """List active SSH sessions."""
        raw = await self._engine.list_sessions()
        result: list[SessionInfo] = []
        for key in raw:
            host = key.split("@", 1)[1] if "@" in key else key
            user = key.split("@", 1)[0] if "@" in key else "?"
            port = 22
            if ":" in host:
                host, port_str = host.rsplit(":", 1)
                try:
                    port = int(port_str)
                except ValueError:
                    port = 22
            result.append(
                SessionInfo(
                    session_key=key,
                    host=host,
                    port=port,
                    username=user,
                )
            )
        return result

    async def close_session(
        self,
        conn: ConnectionParams,
    ) -> None:
        """Close one session."""
        await self._engine.close_session(conn.host, conn.username, conn.port)

    async def close_all(self) -> None:
        """Close all sessions."""
        await self._engine.close_all_sessions()

    #  diagnostics

    async def get_diagnostics(self, conn: ConnectionParams) -> SessionDiagnostics:
        """Get session diagnostic info."""
        return self._engine.get_session_diagnostics(conn.host, conn.username, conn.port)

    async def reset_prompt(self, conn: ConnectionParams) -> bool:
        """Reset prompt detection for a session."""
        return self._engine.reset_session_prompt(conn.host, conn.username, conn.port)

    async def get_health_report(self) -> dict[str, Any]:
        """Get health report for all connections."""
        return self._engine.get_connection_health_report()

    async def get_perf_metrics(self) -> dict[str, Any]:
        """Get performance metrics."""
        return self._engine.get_performance_metrics()

    #  raw access (for legacy code)

    @property
    def engine(self) -> SSHSessionManager:
        """Access the underlying engine for legacy code paths."""
        return self._engine


#  helpers


def _now_ms() -> float:
    return datetime.now(timezone.utc).timestamp() * 1000


def _to_ssh_error(exc: Exception) -> SSHError:
    msg = str(exc)
    if "onnection refused" in msg or "NoValidConnections" in type(exc).__name__:
        return SSHError(ErrorCategory.NETWORK, "Connection refused", detail=msg)
    if "uthentication" in msg or "Auth" in type(exc).__name__:
        return SSHError(ErrorCategory.AUTH, "Authentication failed", detail=msg)
    if "imed out" in msg or "Timeout" in type(exc).__name__:
        return SSHError(ErrorCategory.TIMEOUT, "Connection timed out", detail=msg)
    return SSHError(ErrorCategory.UNKNOWN, msg)
