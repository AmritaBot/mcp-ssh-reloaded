"""Backward-compatible shim for the former ``api_types`` module.

The canonical definitions now live in :mod:`mcp_ssh_reloaded.models`.
This module only re-exports them so existing imports keep working.
"""

from .models import (
    AuthMethod,
    CommandResult,
    CommandStatus,
    ConnectionParams,
    DeviceFamily,
    ErrorCategory,
    ExecutionResult,
    FileContent,
    ServerConfig,
    SessionDiagnostics,
    SessionInfo,
    SSHError,
)

__all__ = [
    "AuthMethod",
    "CommandResult",
    "CommandStatus",
    "ConnectionParams",
    "DeviceFamily",
    "ErrorCategory",
    "ExecutionResult",
    "FileContent",
    "SSHError",
    "ServerConfig",
    "SessionDiagnostics",
    "SessionInfo",
]
