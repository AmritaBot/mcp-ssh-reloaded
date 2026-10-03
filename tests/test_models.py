"""Guardrails for the unified type source (``models.py``).

These tests pin the invariants that the api_types/datastructures merge must
keep, so a future edit cannot silently re-introduce divergent copies.
"""

import mcp_ssh_reloaded.api_types as api_types
import mcp_ssh_reloaded.datastructures as datastructures
from mcp_ssh_reloaded import models


def test_auth_is_alias_of_authentication():
    """Old api_types spelling and datastructures spelling resolve to one member."""
    assert models.ErrorCategory.AUTH is models.ErrorCategory.AUTHENTICATION


def test_command_status_includes_streaming():
    """Union of both former enums keeps the STREAMING member."""
    assert models.CommandStatus.STREAMING.value == "streaming"
    assert models.CommandStatus.AWAITING_INPUT.value == "awaiting_input"


def test_shims_reexport_same_objects():
    """api_types / datastructures must hand out the very same objects."""
    for name in api_types.__all__:
        assert getattr(api_types, name) is getattr(models, name)
    for name in datastructures.__all__:
        assert getattr(datastructures, name) is getattr(models, name)


def test_session_diagnostics_supports_both_field_namings():
    """The merged diagnostics type accepts both producers' field names."""
    diag = models.SessionDiagnostics(
        session_key="a@b:22",
        shell_type="bash",
        connection_health="healthy",
        prompt_detection_confidence=95.0,
    )
    diag.prompt_captured = "x"
    diag.recent_commands.append("ls")
    diag.optimization_hints.append("reset prompt")
    assert diag.connection_health == "healthy"
    assert diag.command_history == []
