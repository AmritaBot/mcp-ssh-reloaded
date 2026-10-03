"""MCP server layer - thin wrapper that translates @mcp.tool() args
into api_types.ConnectionParams and calls SSHService.

This module is the ONLY place that depends on fastmcp.
All business logic lives in services.py / api_types.py.
"""

import asyncio
import json

from fastmcp import FastMCP

from .api_types import ConnectionParams, ServerConfig
from .error_handler import ErrorHandler
from .services import SSHService

#  MCP server instance
mcp = FastMCP("ssh-session")

# Lazy init so tests that import server.py don't trigger engine construction.
_svc: SSHService | None = None
_config: ServerConfig | None = None


def configure_server(config: ServerConfig | None = None) -> None:
    """Configure server before first use (idempotent).

    Call this *before* any tool invocations to override defaults.
    If *config* is None, ``ServerConfig()`` is used which reads ``MCP_SSH_*`` env vars.
    """
    global _config
    _config = config or ServerConfig()


def _service() -> SSHService:
    global _svc, _config
    if _svc is None:
        if _config is None:
            _config = ServerConfig()
        _svc = SSHService(config=_config)
    return _svc


def _conn(
    host: str,
    username: str | None = None,
    password: str | None = None,
    key_filename: str | None = None,
    port: int | None = None,
    enable_password: str | None = None,
    enable_command: str = "enable",
    sudo_password: str | None = None,
) -> ConnectionParams:
    return ConnectionParams(
        host=host,
        port=port or 22,
        username=username,
        password=password,
        key_filename=key_filename,
        enable_password=enable_password,
        enable_command=enable_command,
        sudo_password=sudo_password,
    )


#  Core tools


@mcp.tool()
async def execute_command(
    host: str,
    command: str,
    username: str | None = None,
    password: str | None = None,
    key_filename: str | None = None,
    port: int | None = None,
    enable_password: str | None = None,
    enable_command: str = "enable",
    sudo_password: str | None = None,
    timeout: int = 30,
) -> str:
    """Execute a command on an SSH host using a persistent session."""
    try:
        r = await _service().execute(
            _conn(
                host,
                username,
                password,
                key_filename,
                port,
                enable_password,
                enable_command,
                sudo_password,
            ),
            command,
            timeout=timeout,
        )
    except Exception as e:
        return _fmt_error(e)

    if r.status.value == "running":
        return (
            f"Command is now running in background.\n\n"
            f"Command ID: {r.command_id}\n\n"
            f"Use get_command_status('{r.command_id}') to check progress.\n"
            f"Use interrupt_command_by_id('{r.command_id}') to stop it."
        )
    if r.status.value == "awaiting_input":
        return (
            f"Command paused waiting for user input.\n\n"
            f"Command ID: {r.command_id}\n\n"
            f"Use send_input('{r.command_id}', 'your_input\\n') to provide input."
        )

    result = f"Exit Status: {r.exit_code}\n\n"
    if r.stdout:
        result += f"STDOUT:\n{r.stdout}\n"
    if r.stderr:
        result += f"STDERR:\n{r.stderr}\n"
    if r.truncated:
        result += "\n[OUTPUT TRUNCATED - head and tail only"
        result += f"; full output saved to {r.spilled_path}]" if r.spilled_path else "]"
        result += "\n"
    return result


@mcp.tool()
async def list_sessions() -> str:
    """List all active SSH sessions."""
    sessions = await _service().list_sessions()
    if not sessions:
        return "No active SSH sessions"
    return "Active SSH Sessions:\n" + "\n".join(f"- {s.session_key}" for s in sessions)


@mcp.tool()
async def close_session(
    host: str, username: str | None = None, port: int | None = None
) -> str:
    """Close a specific SSH session."""
    await _service().close_session(
        ConnectionParams(host=host, username=username, port=port or 22)
    )
    return f"Closed session: {username or '(default)'}@{host}:{port or 22}"


@mcp.tool()
async def close_all_sessions() -> str:
    """Close all active SSH sessions."""
    await _service().close_all()
    return "All SSH sessions closed"


@mcp.tool()
async def read_file(
    host: str,
    remote_path: str,
    username: str | None = None,
    password: str | None = None,
    key_filename: str | None = None,
    port: int | None = None,
    encoding: str = "utf-8",
    errors: str = "replace",
    max_bytes: int | None = None,
    sudo_password: str | None = None,
    use_sudo: bool = False,
) -> str:
    """Read a remote file over SSH. Falls back to sudo cat if permission denied."""
    try:
        fc = await _service().read_file(
            ConnectionParams(
                host=host,
                username=username,
                password=password,
                key_filename=key_filename,
                port=port or 22,
                sudo_password=sudo_password,
            ),
            remote_path,
            encoding=encoding,
            max_bytes=max_bytes,
            use_sudo=use_sudo,
        )
        result = "Exit Status: 0\n\nCONTENT:\n" + fc.content
        if fc.truncated:
            result += f"\n\n[CONTENT TRUNCATED after {fc.max_bytes} bytes]"
        return result
    except Exception as e:
        return _fmt_error(e)


@mcp.tool()
async def write_file(
    host: str,
    remote_path: str,
    content: str,
    username: str | None = None,
    password: str | None = None,
    key_filename: str | None = None,
    port: int | None = None,
    encoding: str = "utf-8",
    errors: str = "strict",
    append: bool = False,
    make_dirs: bool = False,
    permissions: int | None = None,
    max_bytes: int | None = None,
    sudo_password: str | None = None,
    use_sudo: bool = False,
) -> str:
    """Write content to a remote file over SSH."""
    try:
        msg = await _service().write_file(
            ConnectionParams(
                host=host,
                username=username,
                password=password,
                key_filename=key_filename,
                port=port or 22,
                sudo_password=sudo_password,
            ),
            remote_path,
            content,
            encoding=encoding,
            append=append,
            make_dirs=make_dirs,
            permissions=permissions,
            max_bytes=max_bytes,
            use_sudo=use_sudo,
        )
        return f"Exit Status: 0\n\nMESSAGE:\n{msg}\n"
    except Exception as e:
        return _fmt_error(e)


#  Async / interactive tools


@mcp.tool()
async def execute_command_async(
    host: str,
    command: str,
    username: str | None = None,
    password: str | None = None,
    key_filename: str | None = None,
    port: int | None = None,
    timeout: int = 300,
) -> str:
    """Execute a command asynchronously. Returns a command ID."""
    cmd_id = await _service().execute_async(
        _conn(host, username, password, key_filename, port),
        command,
        timeout=timeout,
    )
    return f"Command started with ID: {cmd_id}\n\nUse get_command_status('{cmd_id}') to check progress."


@mcp.tool()
def get_command_status(command_id: str) -> str:
    """Get the status and output of an async command."""
    s = _service().get_command_status(command_id)
    if "error" in s:
        return f"Error: {s['error']}"

    lines = [
        f"Command ID: {s['command_id']}",
        f"Session: {s['session_key']}",
        f"Command: {s['command']}",
        f"Status: {s['status']}",
        f"Started: {s['start_time']}",
    ]
    if s.get("end_time"):
        lines.append(f"Ended: {s['end_time']}")
    if s.get("exit_code") is not None:
        lines.append(f"Exit Code: {s['exit_code']}")
    if s.get("awaiting_input_reason"):
        lines.append(f"Awaiting Input: {s['awaiting_input_reason']}")
        lines.append(f"  → Use send_input('{command_id}', 'your_input\\n')")

    result = "\n".join(lines)
    if s.get("stdout"):
        result += f"\n\nSTDOUT:\n{s['stdout']}"
    if s.get("stderr"):
        result += f"\n\nSTDERR:\n{s['stderr']}"
    return result


@mcp.tool()
def interrupt_command_by_id(command_id: str) -> str:
    """Interrupt a running async command by sending Ctrl+C."""
    _, msg = _service().interrupt(command_id)
    return msg


@mcp.tool()
def list_running_commands() -> str:
    """List all currently running async commands."""
    cmds = _service().list_running()
    if not cmds:
        return "No running commands"
    result = "Running Commands:\n"
    for c in cmds:
        result += f"\n- ID: {c['command_id']}\n  Session: {c['session_key']}\n  Command: {c['command']}\n  Status: {c['status']}\n"
    return result


@mcp.tool()
def list_command_history(limit: int = 50) -> str:
    """List recent command history."""
    cmds = _service().list_history(limit)
    if not cmds:
        return "No command history"
    result = f"Command History (last {len(cmds)}):\n"
    for c in cmds:
        result += f"\n- ID: {c['command_id']}\n  Session: {c['session_key']}\n  Command: {c['command']}\n  Status: {c['status']}"
        if c.get("exit_code") is not None:
            result += f"\n  Exit Code: {c['exit_code']}"
    return result


@mcp.tool()
async def send_input(command_id: str, input_text: str) -> str:
    """Send input to a running async command."""
    ok, output, err = await _service().send_input(command_id, input_text)
    if not ok:
        return f"Error: {err}"
    result = "Input sent successfully\n"
    if output:
        result += f"\nOutput:\n{output}"
    return result


#  Session-level input / screen tools
# (These rely on engine internals - kept for backward compat)


@mcp.tool()
async def send_input_by_session(
    host: str, input_text: str, username: str | None = None, port: int | None = None
) -> str:
    """Send input to the active shell for a session."""
    svc = _service()
    _, _, _, _, session_key = svc.engine._resolve_connection(host, username, port)
    shell = svc.engine.registry.shells.get(session_key)
    if not shell:
        return "Error: No active shell for this session"
    try:
        shell.send(input_text.encode())

        await asyncio.sleep(0.2)
        return "Input sent successfully"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def read_screen(
    host: str, username: str | None = None, port: int | None = None, max_lines: int = 24
) -> str:
    """Read the terminal screen state (requires MCP_SSH_INTERACTIVE_MODE=1)."""
    svc = _service()
    _, _, _, _, session_key = svc.engine._resolve_connection(host, username, port)
    snap = svc.engine._get_screen_snapshot(session_key, max_lines)
    return json.dumps(snap, indent=2)


@mcp.tool()
def send_keys(
    host: str, keys: str, username: str | None = None, port: int | None = None
) -> str:
    """Send special keys to a session."""
    key_map: dict[str, str] = {
        "<enter>": "\n",
        "<return>": "\n",
        "<esc>": "\x1b",
        "<escape>": "\x1b",
        "<tab>": "\t",
        "<ctrl-c>": "\x03",
        "<ctrl-d>": "\x04",
        "<ctrl-z>": "\x1a",
        "<ctrl-g>": "\x07",
        "<up>": "\x1b[A",
        "<down>": "\x1b[B",
        "<right>": "\x1b[C",
        "<left>": "\x1b[D",
        "<space>": " ",
    }
    processed = keys
    for tok, ch in key_map.items():
        processed = processed.replace(tok, ch)

    svc = _service()
    _, _, _, _, session_key = svc.engine._resolve_connection(host, username, port)
    shell = svc.engine.registry.shells.get(session_key)
    if not shell:
        return "Error: No active shell"
    try:
        shell.send(processed.encode())
        return f"Successfully sent keys to {host}"
    except Exception as e:
        return f"Error: {e}"


#  Enhanced / diagnostics tools


@mcp.tool()
async def execute_command_enhanced(
    host: str,
    command: str,
    username: str | None = None,
    password: str | None = None,
    key_filename: str | None = None,
    port: int | None = None,
    enable_password: str | None = None,
    enable_command: str = "enable",
    sudo_password: str | None = None,
    timeout: int = 30,
    auto_extend_timeout: bool = True,
    max_timeout: int = 600,
    streaming_mode: bool = False,
    progress_callback: str | None = None,
) -> str:
    """Execute a command with auto timeout extension and streaming."""
    try:
        return await _service().engine.enhanced_executor.execute_command_enhanced(
            host=host,
            username=username,
            command=command,
            password=password,
            key_filename=key_filename,
            port=port,
            enable_password=enable_password,
            enable_command=enable_command,
            sudo_password=sudo_password,
            timeout=timeout,
            auto_extend_timeout=auto_extend_timeout,
            max_timeout=max_timeout,
            streaming_mode=streaming_mode,
            progress_callback=progress_callback,
        )
    except Exception as e:
        return _fmt_error(e)


@mcp.tool()
async def get_session_diagnostics(
    host: str, username: str | None = None, port: int | None = None
) -> str:
    """Get comprehensive diagnostics for an SSH session."""
    try:
        diag = await _service().get_diagnostics(
            ConnectionParams(host=host, username=username, port=port or 22)
        )
        lines = [
            f"🔍 Session Diagnostics for {diag.session_key}",
            f"📊 Connection Health: {diag.connection_health}",
            f"🖥️  Shell Type: {diag.shell_type or 'unknown'}",
            f"🎯 Prompt Confidence: {diag.prompt_detection_confidence:.1f}%",
            f"📝 Captured Prompt: {diag.captured_prompt!r}",
            f"   Pattern: {diag.prompt_pattern or 'None'}",
        ]
        if diag.last_activity:
            lines.append(f"⏰ Last Activity: {diag.last_activity}")
        if diag.command_history:
            lines.append("📚 Recent: " + ", ".join(diag.command_history[-5:]))
        return "\n".join(lines)
    except Exception as e:
        return _fmt_error(e)


@mcp.tool()
async def reset_session_prompt(
    host: str, username: str | None = None, port: int | None = None
) -> str:
    """Reset and recapture prompt detection."""
    ok = await _service().reset_prompt(
        ConnectionParams(host=host, username=username, port=port or 22)
    )
    return "✅ Prompt reset" if ok else "❌ Failed to reset prompt"


@mcp.tool()
async def get_connection_health_report() -> str:
    """Get health report for all active SSH connections."""
    try:
        r = await _service().get_health_report()
        lines = [
            "🌐 Connection Health Report",
            f"📅 {r['timestamp']}",
            f"Total: {r['total_sessions']} | Healthy: {r['healthy_sessions']} | Degraded: {r['degraded_sessions']} | Dead: {r['dead_sessions']}",
        ]
        for k, d in r.get("session_details", {}).items():
            lines.append(f"  {k}: {d.get('health', '?')}")
        return "\n".join(lines)
    except Exception as e:
        return _fmt_error(e)


@mcp.tool()
def get_command_status_enhanced(command_id: str) -> str:
    """Get enhanced status with detailed metrics."""
    try:
        s = _service().engine.enhanced_executor.get_command_status_enhanced(command_id)
        if s.get("status") == "not_found":
            return f"❌ Command {command_id} not found"
        lines = [
            f"📊 Enhanced Status: {command_id}",
            f"Status: {s['status']} | Started: {s['start_time']}",
            f"Output: {s['output_size_display']}",
        ]
        if s.get("duration_seconds"):
            lines.append(f"Duration: {s['duration_seconds']:.1f}s")
        return "\n".join(lines)
    except Exception as e:
        return _fmt_error(e)


@mcp.tool()
async def get_performance_metrics() -> str:
    """Get performance metrics."""
    m = await _service().get_perf_metrics()
    if not m:
        return "No metrics yet"
    lines = ["📈 Performance:"]
    for op, data in m.items():
        lines.append(f"  {op}: {data['count']} calls, avg {data['avg_time']:.3f}s")
    return "\n".join(lines)


#  helpers


def _fmt_error(exc: Exception) -> str:
    """Format an exception for MCP response."""
    einfo = ErrorHandler.categorize_error(str(exc), exc)
    return ErrorHandler.format_error_for_ai(einfo)
