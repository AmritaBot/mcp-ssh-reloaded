"""Single source of truth for the data types used across mcp-ssh-reloaded.

This module consolidates the former ``api_types`` and ``datastructures``
modules, which used to declare overlapping (and subtly divergent) copies of
``CommandStatus`` / ``ErrorCategory`` / ``SessionDiagnostics``.

It is a pure data layer: it declares *what* the service deals with and carries
no runtime dependency on the network stack beyond type-only references.

``api_types`` and ``datastructures`` remain importable as thin
backward-compatible shims, so existing callers keep working unchanged.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    import paramiko


# Identity & connection


class DeviceFamily(Enum):
    """Broad device category - drives shell interaction strategy."""

    UNIX = auto()
    CISCO = auto()
    JUNIPER = auto()
    MIKROTIK = auto()
    FORTINET = auto()
    ARISTA = auto()
    PALOALTO = auto()
    CHECKPOINT = auto()
    VYOS = auto()
    OPENWRT = auto()
    GENERIC_NETWORK = auto()
    UNKNOWN = auto()


class AuthMethod(Enum):
    PASSWORD = auto()
    KEY = auto()
    AGENT = auto()
    NONE = auto()


@dataclass
class ConnectionParams:
    """Immutable-ish description of *how* to reach a host.

    This is the data you declare.  The service layer resolves
    SSH config / env overrides / key paths at execution time.
    """

    host: str
    port: int = 22
    username: str | None = None
    password: str | None = None
    key_filename: str | None = None
    device_family: DeviceFamily = DeviceFamily.UNKNOWN

    # Privilege elevation
    sudo_password: str | None = None
    enable_password: str | None = None
    enable_command: str = "enable"

    # Optional tags for grouping / filtering
    tags: list[str] = field(default_factory=list)

    @property
    def session_key(self) -> str:
        """Canonical session identifier."""
        u = self.username or "?"
        return f"{u}@{self.host}:{self.port}"

    def with_overrides(self, **kw) -> "ConnectionParams":
        """Return a copy with some fields replaced."""
        d = {f.name: getattr(self, f.name) for f in self.__dataclass_fields__.values()}  # type: ignore[arg-type]
        d.update(kw)
        return ConnectionParams(**d)


# Execution results


class CommandStatus(Enum):
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"  # Waiting for user input (password, prompt, ...)
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    STREAMING = "streaming"  # Long-running command with streaming output


@dataclass
class CommandResult:
    """Outcome of a single command execution."""

    stdout: str
    stderr: str
    exit_code: int
    status: CommandStatus = CommandStatus.COMPLETED
    command_id: str | None = None
    duration_ms: float = 0.0
    truncated: bool = False
    spilled_path: str | None = None


@dataclass
class FileContent:
    """Result of a remote file read."""

    content: str
    path: str
    truncated: bool = False
    max_bytes: int = 0


# Session lifecycle


@dataclass
class SessionInfo:
    """Lightweight summary of an active session."""

    session_key: str
    host: str
    port: int
    username: str
    device_family: DeviceFamily = DeviceFamily.UNKNOWN
    connected_at: str = ""
    last_active: str = ""
    active_command: bool = False
    enable_mode: bool = False


@dataclass
class SessionDiagnostics:
    """Full diagnostics for one session.

    Field names from the former ``api_types`` / ``datastructures`` copies are
    both retained so that neither producer nor consumer needs to change.
    """

    session_key: str
    connection_health: str = "unknown"  # "healthy", "degraded", "dead"
    shell_type: str | None = None
    # Prompt capture (datastructures naming)
    captured_prompt: str | None = None
    generalized_prompt: str | None = None
    prompt_pattern: str | None = None
    prompt_detection_confidence: float = 0.0
    # Prompt capture (api_types naming)
    prompt_captured: str | None = None
    prompt_confidence: float = 0.0
    last_activity: datetime | str = ""
    shell_state: dict[str, Any] = field(default_factory=dict)
    command_history: list[str] = field(default_factory=list)
    recent_commands: list[str] = field(default_factory=list)
    optimization_hints: list[str] = field(default_factory=list)


@dataclass
class RunningCommand:
    command_id: str
    session_key: str
    command: str
    shell: paramiko.Channel
    future: ConcurrentFuture[Any] | asyncio.Future[Any] | None
    status: CommandStatus
    stdout: str
    stderr: str
    exit_code: int | None
    start_time: datetime
    end_time: datetime | None
    awaiting_input_reason: str | None = (
        None  # What is the command waiting for? (e.g., "password", "user_input")
    )
    monitoring_cancelled: threading.Event = field(default_factory=threading.Event)
    sentinel: str | None = None  # Sentinel marker used for Unix command completion

    # Enhanced UX fields
    auto_extend_timeout: bool = False
    max_timeout: int = 300  # Maximum timeout if auto-extending
    progress_callback: str | None = None  # MCP tool name for progress callbacks
    streaming_mode: bool = False
    last_output_time: datetime | None = None
    output_chunks: list[str] = field(default_factory=list)  # For streaming mode
    truncated: bool = False  # Output hit the cap; stdout is head+tail only
    spilled_path: str | None = None  # Where the full stream was written


@dataclass
class ConnectionProfile:
    """Cached SSH connection profile for performance."""

    hostname: str
    username: str
    port: int
    key_filename: str | None
    config_host: str | None  # Original SSH config alias
    resolved_at: datetime = field(default_factory=datetime.now)

    # Performance metrics
    connect_count: int = 0
    last_connect: datetime | None = None
    avg_connect_time: float = 0.0
    connection_health: str = "unknown"  # "healthy", "degraded", "dead"


# Server config


class ServerConfig(BaseSettings):
    """Tunables for the SSH service - read once at startup.

    Values are resolved in this priority (highest to lowest):
      1. Explicit constructor kwargs
      2. Environment variables (MCP_SSH_*)
      3. Class-level defaults

    Example::

        export MCP_SSH_DEFAULT_TIMEOUT=60
        export MCP_SSH_INTERACTIVE_MODE=false
    """

    model_config = SettingsConfigDict(
        env_prefix="MCP_SSH_",
        env_nested_delimiter="__",
        case_sensitive=False,
    )

    default_timeout: int = 30
    max_timeout: int = 300
    connect_timeout: int = 30
    max_workers: int = 10
    max_file_bytes: int = 2 * 1024 * 1024
    max_output_bytes: int = 10 * 1024 * 1024
    interactive_mode: bool = True
    pty_aware_validation: bool = False
    mikrotik_auto_paging: bool = True
    terminal_width: int = 100
    terminal_height: int = 24
    log_dir: str = "/tmp/mcp_ssh_session_logs"
    background_monitor_max_timeout: int = 300
    normal_idle_timeout: int = 2
    package_manager_idle_timeout: int = 10
    async_default_timeout: int = 30


# Error types


class ErrorCategory(Enum):
    """Categories of errors for better user understanding.

    ``AUTH`` is kept as an alias of ``AUTHENTICATION`` for backward
    compatibility with callers written against the old ``api_types``.
    """

    NETWORK = "network"
    AUTHENTICATION = "authentication"
    AUTH = "authentication"  # alias of AUTHENTICATION
    TIMEOUT = "timeout"
    COMMAND = "command"
    PROTOCOL = "protocol"
    PERMISSION = "permission"
    UNKNOWN = "unknown"


@dataclass
class ErrorInfo:
    """Structured error information with troubleshooting hints."""

    category: ErrorCategory
    message: str
    original_error: str | None = None
    troubleshooting_hint: str | None = None
    suggest_action: str | None = None


class SSHError(Exception):
    """Structured error returned by the service layer."""

    category: ErrorCategory
    message: str
    detail: str
    hint: str
    recoverable: bool

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        detail: str = "",
        hint: str = "",
        recoverable: bool = False,
    ):
        self.category = category
        self.message = message
        self.detail = detail
        self.hint = hint
        self.recoverable = recoverable
        super().__init__(message)


@dataclass
class ExecutionResult:
    """Structured outcome of one execution pass.

    Declared here as the target replacement for the overloaded ``124`` exit
    code and the ``"ASYNC:..."`` / ``"AWAITING_INPUT:..."`` string protocols.
    """

    status: CommandStatus
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    command_id: str | None = None
    awaiting_input: str | None = None
    sentinel: str | None = None
    truncated: bool = False
    spilled_path: str | None = None
    long_running: bool = False
    duration_ms: float = 0.0
