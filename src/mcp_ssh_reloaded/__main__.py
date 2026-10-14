"""Entry point for mcp-ssh.

Supports multiple modes:
    mcp-ssh serve mcp          # MCP stdio server (default)
    mcp-ssh serve http         # MCP Streamable HTTP server
    mcp-ssh serve sse          # MCP SSE server
    mcp-ssh exec <host> <cmd>  # Direct execution (no MCP)
    mcp-ssh list               # List active sessions
    mcp-ssh close <host>       # Close a session
"""

import argparse
import asyncio
import sys
from typing import Any, Literal, cast

from .api_types import ConnectionParams, ServerConfig
from .services import SSHService


def _add_config_args(parser: argparse.ArgumentParser, prefix: str = "") -> None:
    """Add common ServerConfig CLI flags to a (sub)parser.

    All flags are optional; they override env vars, which override defaults.
    """
    group = parser.add_argument_group("SSH config overrides")
    group.add_argument(
        "--default-timeout",
        type=int,
        default=None,
        help="Default command timeout in seconds (env: MCP_SSH_DEFAULT_TIMEOUT, default: 30)",
    )
    group.add_argument(
        "--max-timeout",
        type=int,
        default=None,
        help="Maximum command timeout in seconds (env: MCP_SSH_MAX_TIMEOUT, default: 300)",
    )
    group.add_argument(
        "--connect-timeout",
        type=int,
        default=None,
        help="SSH connection timeout in seconds (env: MCP_SSH_CONNECT_TIMEOUT, default: 30)",
    )
    group.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Thread pool max workers (env: MCP_SSH_MAX_WORKERS, default: 10)",
    )
    group.add_argument(
        "--max-file-bytes",
        type=int,
        default=None,
        help="Max bytes for file read/write (env: MCP_SSH_MAX_FILE_BYTES, default: 2MB)",
    )
    group.add_argument(
        "--max-output-bytes",
        type=int,
        default=None,
        help="Max bytes for command output (env: MCP_SSH_MAX_OUTPUT_BYTES, default: 10MB)",
    )
    group.add_argument(
        "--async-default-timeout",
        type=int,
        default=None,
        help="Default async command timeout (env: MCP_SSH_ASYNC_DEFAULT_TIMEOUT, default: 30)",
    )
    group.add_argument(
        "--background-monitor-max-timeout",
        type=int,
        default=None,
        help="Background monitor max timeout (env: MCP_SSH_BACKGROUND_MONITOR_MAX_TIMEOUT, default: 300)",
    )
    group.add_argument(
        "--normal-idle-timeout",
        type=int,
        default=None,
        help="Normal idle timeout in seconds (env: MCP_SSH_NORMAL_IDLE_TIMEOUT, default: 2)",
    )
    group.add_argument(
        "--package-manager-idle-timeout",
        type=int,
        default=None,
        help="Package manager idle timeout (env: MCP_SSH_PACKAGE_MANAGER_IDLE_TIMEOUT, default: 10)",
    )
    group.add_argument(
        "--interactive-mode",
        type=lambda v: v.lower() in ("1", "true", "yes", "on"),
        default=None,
        help="Enable interactive/PTY mode (env: MCP_SSH_INTERACTIVE_MODE, default: on)",
    )
    group.add_argument(
        "--pty-aware-validation",
        type=lambda v: v.lower() in ("1", "true", "yes", "on"),
        default=None,
        help="Enable PTY-aware command validation (env: MCP_SSH_PTY_AWARE_VALIDATION, default: off)",
    )
    group.add_argument(
        "--mikrotik-auto-paging",
        type=lambda v: v.lower() in ("1", "true", "yes", "on"),
        default=None,
        help="Auto-deal with MikroTik pagers (env: MCP_SSH_MIKROTIK_AUTO_PAGING, default: on)",
    )
    group.add_argument(
        "--terminal-width",
        type=int,
        default=None,
        help="PTY terminal width (env: MCP_SSH_TERMINAL_WIDTH, default: 100)",
    )
    group.add_argument(
        "--terminal-height",
        type=int,
        default=None,
        help="PTY terminal height (env: MCP_SSH_TERMINAL_HEIGHT, default: 24)",
    )
    group.add_argument(
        "--log-dir",
        type=str,
        default=None,
        help="Session log directory (env: MCP_SSH_LOG_DIR, default: /tmp/mcp_ssh_session_logs)",
    )


def _build_config_from_args(args: argparse.Namespace) -> ServerConfig:
    """Build ServerConfig: Pydantic reads env vars, CLI overrides take precedence."""
    overrides: dict[str, Any] = {}
    for field_name in ServerConfig.model_fields:
        val = getattr(args, field_name, None)
        if val is not None:
            overrides[field_name] = val
    return ServerConfig(**overrides)


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="mcp-ssh",
        description="MCP SSH Session server",
    )
    sub = parser.add_subparsers(dest="mode")

    # serve <sub-mode>
    serve_p = sub.add_parser("serve", help="Start a server")
    serve_sub = serve_p.add_subparsers(dest="serve_mode", required=True)

    mcp_p = serve_sub.add_parser("mcp", help="MCP stdio server (default)")
    _add_config_args(mcp_p)

    http_p = serve_sub.add_parser("http", help="MCP Streamable HTTP server")
    http_p.add_argument(
        "--host", default="127.0.0.1", help="Listen host (default: 127.0.0.1)"
    )
    http_p.add_argument(
        "--port", type=int, default=8000, help="Listen port (default: 8000)"
    )
    _add_config_args(http_p)

    sse_p = serve_sub.add_parser("sse", help="MCP SSE server")
    sse_p.add_argument(
        "--host", default="127.0.0.1", help="Listen host (default: 127.0.0.1)"
    )
    sse_p.add_argument(
        "--port", type=int, default=8000, help="Listen port (default: 8000)"
    )
    _add_config_args(sse_p)

    # exec <host> <command...>
    exec_p = sub.add_parser("exec", help="Execute a command directly")
    exec_p.add_argument("host")
    exec_p.add_argument("command", nargs="+")
    exec_p.add_argument("-u", "--user")
    exec_p.add_argument("-p", "--port", type=int, default=22)
    exec_p.add_argument("-k", "--key")
    exec_p.add_argument("--sudo-password")
    exec_p.add_argument("-t", "--timeout", type=int, default=30)

    # list
    sub.add_parser("list", help="List active sessions")

    # close <host>
    close_p = sub.add_parser("close", help="Close a session")
    close_p.add_argument("host")
    close_p.add_argument("-u", "--user")
    close_p.add_argument("-p", "--port", type=int, default=22)

    # close-all
    sub.add_parser("close-all", help="Close all sessions")

    args = parser.parse_args(argv)

    if args.mode is None:
        _run_mcp()
    elif args.mode == "serve":
        config = _build_config_from_args(args)
        if args.serve_mode == "mcp":
            _run_mcp(config=config)
        elif args.serve_mode == "http":
            _run_mcp(
                config=config,
                transport="streamable-http",
                host=args.host,
                port=args.port,
            )
        elif args.serve_mode == "sse":
            _run_mcp(
                config=config,
                transport="sse",
                host=args.host,
                port=args.port,
            )
        else:
            _run_mcp(config=config)
    elif args.mode == "exec":
        _run_exec(args)
    elif args.mode == "list":
        _run_list()
    elif args.mode == "close":
        _run_close(args)
    elif args.mode == "close-all":
        _run_close_all()
    else:
        _run_mcp()


Transport = Literal["stdio", "sse", "streamable-http"]


def _run_mcp(
    transport: str = "stdio", config: ServerConfig | None = None, **kwargs: Any
) -> None:
    from .server import configure_server, mcp

    configure_server(config)
    mcp.run(transport=cast(Transport, transport), **kwargs)


def _run_exec(args) -> None:
    async def _exec():
        svc = SSHService()
        conn = ConnectionParams(
            host=args.host,
            port=args.port,
            username=args.user,
            key_filename=args.key,
            sudo_password=args.sudo_password,
        )
        cmd = " ".join(args.command)
        result = await svc.execute(conn, cmd, timeout=args.timeout)
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        sys.exit(result.exit_code)

    asyncio.run(_exec())


def _run_list() -> None:
    async def _list():
        svc = SSHService()
        sessions = await svc.list_sessions()
        if not sessions:
            print("No active SSH sessions")
            return
        for s in sessions:
            print(f"  {s.session_key}")

    asyncio.run(_list())


def _run_close(args) -> None:
    async def _close():
        svc = SSHService()
        await svc.close_session(
            ConnectionParams(host=args.host, username=args.user, port=args.port)
        )
        print(f"Closed: {args.user or '(default)'}@{args.host}:{args.port}")

    asyncio.run(_close())


def _run_close_all() -> None:
    async def _close_all():
        svc = SSHService()
        await svc.close_all()
        print("All SSH sessions closed")

    asyncio.run(_close_all())


if __name__ == "__main__":
    main()
