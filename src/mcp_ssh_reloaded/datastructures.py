"""Backward-compatible shim for the former ``datastructures`` module.

The canonical definitions now live in :mod:`mcp_ssh_reloaded.models`.
This module only re-exports them so existing imports keep working.
"""

from .models import (
    CommandStatus,
    ConnectionProfile,
    ErrorCategory,
    ErrorInfo,
    RunningCommand,
    SessionDiagnostics,
)

__all__ = [
    "CommandStatus",
    "ConnectionProfile",
    "ErrorCategory",
    "ErrorInfo",
    "RunningCommand",
    "SessionDiagnostics",
]
