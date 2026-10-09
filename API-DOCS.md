# API Documentation

## Installation

```bash
uvx mcp-ssh-reloaded
```

Or as a library:

```bash
uv add mcp-ssh-reloaded
```

---

## Quick Start

### Python API

```python
from mcp_ssh_reloaded import SSHService, ConnectionParams

svc = SSHService()
conn = ConnectionParams(host="myserver", username="admin", key_filename="~/.ssh/id_rsa")

# Execute a command
r = svc.execute(conn, "uptime")
print(r.stdout, r.exit_code)

# Read a remote file
f = svc.read_file(conn, "/etc/hostname")
print(f.content)
```

### CLI

```bash
# Start MCP server (default)
mcp-ssh-reloaded serve mcp

# Execute directly
mcp-ssh-reloaded exec myserver "uptime" -u admin

# List sessions
mcp-ssh-reloaded list

# Close sessions
mcp-ssh-reloaded close myserver -u admin
mcp-ssh-reloaded close-all
```

---

## Core Types

### `ConnectionParams`

Everything needed to reach a host. Immutable-ish - use `.with_overrides()` for copies.

| Field             | Type           | Default      | Description                                    |
| ----------------- | -------------- | ------------ | ---------------------------------------------- |
| `host`            | `str`          | _(required)_ | Hostname, IP, or SSH config alias              |
| `port`            | `int`          | `22`         | SSH port                                       |
| `username`        | `str \| None`  | `None`       | SSH user (falls back to SSH config or `$USER`) |
| `password`        | `str \| None`  | `None`       | Password for password auth                     |
| `key_filename`    | `str \| None`  | `None`       | Path to SSH private key                        |
| `device_family`   | `DeviceFamily` | `UNKNOWN`    | Hint for shell interaction strategy            |
| `sudo_password`   | `str \| None`  | `None`       | Sudo password for Unix hosts                   |
| `enable_password` | `str \| None`  | `None`       | Enable mode password for network devices       |
| `enable_command`  | `str`          | `"enable"`   | Command to enter enable mode                   |
| `tags`            | `list[str]`    | `[]`         | Optional grouping / filtering tags             |

**Properties:**

| Property       | Returns | Description                            |
| -------------- | ------- | -------------------------------------- |
| `.session_key` | `str`   | Canonical session ID: `user@host:port` |

**Methods:**

| Method                  | Returns            | Description                       |
| ----------------------- | ------------------ | --------------------------------- |
| `.with_overrides(**kw)` | `ConnectionParams` | Shallow copy with fields replaced |

```python
conn = ConnectionParams(host="dev", username="me")
prod = conn.with_overrides(host="prod.example.com")
```

### `CommandResult`

| Field         | Type            | Default     | Description                            |
| ------------- | --------------- | ----------- | -------------------------------------- |
| `stdout`      | `str`           |             | Command standard output                |
| `stderr`      | `str`           |             | Command standard error                 |
| `exit_code`   | `int`           |             | Process exit code                      |
| `status`      | `CommandStatus` | `COMPLETED` | Execution state                        |
| `command_id`  | `str \| None`   | `None`      | ID for async / awaiting-input commands |
| `duration_ms` | `float`         | `0.0`       | Wall-clock duration in milliseconds    |
| `truncated`   | `bool`          | `False`     | True if output exceeded limit          |

### `CommandStatus`

| Value            | Meaning                                                  |
| ---------------- | -------------------------------------------------------- |
| `RUNNING`        | Async command still executing                            |
| `AWAITING_INPUT` | Command needs interactive input (password, prompt, etc.) |
| `COMPLETED`      | Finished successfully                                    |
| `INTERRUPTED`    | Stopped by Ctrl+C                                        |
| `FAILED`         | Non-zero exit or exception                               |

### `FileContent`

| Field             | Type        | Description                                       |
| ----------------- | ----------- | ------------------------------------------------- |
| `content`         | `str`       | File contents (decoded, always character-aligned) |
| `path`            | `str`       | Remote path                                       |
| `truncated`       | `bool`      | True if the read was capped                       |
| `max_bytes`       | `int`       | Read cap used (0 = unlimited)                     |
| `bytes_read`      | `int`       | Bytes actually returned                           |
| `offset`          | `int`       | Byte offset the window started at                 |
| `next_offset`     | `int \| None` | Byte offset to resume from (truncated reads)      |
| `start_line`      | `int \| None` | First line in the window (line reads only)        |
| `end_line`        | `int \| None` | Last line in the window (line reads only)         |
| `total_lines`     | `int \| None` | Total lines, when the whole file was read         |
| `next_start_line` | `int \| None` | Line to resume from (line reads only)             |
| `error`           | `str \| None` | Error detail when the read failed                 |

### `SessionInfo`

| Field            | Type           | Description                  |
| ---------------- | -------------- | ---------------------------- |
| `session_key`    | `str`          | `user@host:port`             |
| `host`           | `str`          | Resolved hostname            |
| `port`           | `int`          | SSH port                     |
| `username`       | `str`          | SSH username                 |
| `device_family`  | `DeviceFamily` | Device category              |
| `connected_at`   | `str`          | Connection timestamp         |
| `last_active`    | `str`          | Last activity timestamp      |
| `active_command` | `bool`         | Whether a command is running |
| `enable_mode`    | `bool`         | Whether in privileged mode   |

### `SessionDiagnostics`

| Field                | Type          | Description                            |
| -------------------- | ------------- | -------------------------------------- |
| `session_key`        | `str`         | Session identifier                     |
| `connection_health`  | `str`         | `"healthy"`, `"degraded"`, or `"dead"` |
| `shell_type`         | `str`         | Detected shell type                    |
| `prompt_captured`    | `str \| None` | Literal prompt string                  |
| `prompt_pattern`     | `str \| None` | Regex used for prompt matching         |
| `prompt_confidence`  | `float`       | Confidence score 0-100                 |
| `last_activity`      | `str`         | ISO-format timestamp                   |
| `shell_state`        | `dict`        | Internal shell state info              |
| `recent_commands`    | `list[str]`   | Command history (last 10)              |
| `optimization_hints` | `list[str]`   | Suggested improvements                 |

### `DeviceFamily`

Enum for device-specific shell behavior:

`UNIX`, `CISCO`, `JUNIPER`, `MIKROTIK`, `FORTINET`, `ARISTA`, `PALOALTO`, `CHECKPOINT`, `VYOS`, `OPENWRT`, `GENERIC_NETWORK`, `UNKNOWN`

### `ServerConfig`

Tunables for `SSHService`. Powered by **Pydantic `BaseSettings`** - reads `MCP_SSH_*` env vars automatically.

**Priority:** kwargs > env vars > defaults.

| Field                            | Type   | Default                       | Env Variable                             | Description                            |
| -------------------------------- | ------ | ----------------------------- | ---------------------------------------- | -------------------------------------- |
| `default_timeout`                | `int`  | `30`                          | `MCP_SSH_DEFAULT_TIMEOUT`                | Command timeout (seconds)              |
| `max_timeout`                    | `int`  | `300`                         | `MCP_SSH_MAX_TIMEOUT`                    | Hard cap on timeout                    |
| `connect_timeout`                | `int`  | `30`                          | `MCP_SSH_CONNECT_TIMEOUT`                | SSH connect timeout                    |
| `max_workers`                    | `int`  | `10`                          | `MCP_SSH_MAX_WORKERS`                    | Thread pool size                       |
| `max_file_bytes`                 | `int`  | `2_097_152`                   | `MCP_SSH_MAX_FILE_BYTES`                 | Max file read size (2 MB)              |
| `max_output_bytes`               | `int`  | `10_485_760`                  | `MCP_SSH_MAX_OUTPUT_BYTES`               | Max command output (10 MB)             |
| `interactive_mode`               | `bool` | `True`                        | `MCP_SSH_INTERACTIVE_MODE`               | Enable PTY terminal emulation          |
| `pty_aware_validation`           | `bool` | `False`                       | `MCP_SSH_PTY_AWARE_VALIDATION`           | Relax validation for PTY inspection    |
| `mikrotik_auto_paging`           | `bool` | `True`                        | `MCP_SSH_MIKROTIK_AUTO_PAGING`           | Auto-add `without-paging` for MikroTik |
| `terminal_width`                 | `int`  | `100`                         | `MCP_SSH_TERMINAL_WIDTH`                 | PTY columns                            |
| `terminal_height`                | `int`  | `24`                          | `MCP_SSH_TERMINAL_HEIGHT`                | PTY rows                               |
| `log_dir`                        | `str`  | `"/tmp/mcp_ssh_session_logs"` | `MCP_SSH_LOG_DIR`                        | Log directory                          |
| `background_monitor_max_timeout` | `int`  | `300`                         | `MCP_SSH_BACKGROUND_MONITOR_MAX_TIMEOUT` | Background monitor max timeout         |
| `normal_idle_timeout`            | `int`  | `2`                           | `MCP_SSH_NORMAL_IDLE_TIMEOUT`            | Normal command idle timeout            |
| `package_manager_idle_timeout`   | `int`  | `10`                          | `MCP_SSH_PACKAGE_MANAGER_IDLE_TIMEOUT`   | Package manager idle timeout           |
| `async_default_timeout`          | `int`  | `30`                          | `MCP_SSH_ASYNC_DEFAULT_TIMEOUT`          | Async command default timeout          |

```python
# Env var
export MCP_SSH_DEFAULT_TIMEOUT=60

# Python
svc = SSHService(config=ServerConfig(default_timeout=60, max_workers=20))

# ServerConfig() with no args reads env vars automatically
svc = SSHService()  # reads MCP_SSH_* from environment
```

---

## `SSHService` - Python API

### Constructor

```python
SSHService(
    config: ServerConfig | None = None,
    logger: logging.Logger | None = None,
)
```

### `execute` - synchronous command

```python
def execute(
    self,
    conn: ConnectionParams,
    command: str,
    *,
    timeout: int | None = None,
    sudo: bool = False,
) -> CommandResult
```

Returns `CommandResult`. Automatically handles sudo password prompting when `sudo=True` and `conn.sudo_password` is set. For network devices, passes `conn.enable_password`.

**Async transition:** If the command takes longer than `timeout`, the call returns with `status=RUNNING` and a `command_id`. The command continues in the background - poll with `get_command_status`.

```python
r = svc.execute(conn, "apt update && apt install -y nginx", timeout=60, sudo=True)
if r.status == CommandStatus.RUNNING:
    # Poll until done
    while True:
        s = svc.get_command_status(r.command_id)
        if s["status"] != "running":
            break
```

### `execute_async` - background command

```python
def execute_async(
    self,
    conn: ConnectionParams,
    command: str,
    *,
    timeout: int = 300,
) -> str
```

Returns `command_id` immediately. Use `get_command_status` to poll.

### `get_command_status`

```python
def get_command_status(self, command_id: str) -> dict[str, Any]
```

Returns dict: `{"status": "...", "stdout": "...", "stderr": "...", "exit_code": ...}`.

### `send_input`

```python
def send_input(self, command_id: str, text: str) -> tuple[bool, str, str]
```

Send input to an `AWAITING_INPUT` command. Returns `(success, stdout_snapshot, stderr)`.

### `interrupt`

```python
def interrupt(self, command_id: str) -> tuple[bool, str]
```

Send Ctrl+C to a running async command.

### `list_running`

```python
def list_running(self) -> list[dict[str, Any]]
```

List currently-executing async commands.

### `list_history`

```python
def list_history(self, limit: int = 50) -> list[dict[str, Any]]
```

List completed / failed / interrupted commands.

### `read_file`

```python
def read_file(
    self,
    conn: ConnectionParams,
    path: str,
    *,
    encoding: str = "utf-8",
    max_bytes: int | None = None,
    start_line: int = 1,
    offset: int = 0,
    use_sudo: bool = False,
    timeout: int | None = None,
) -> FileContent
```

Read a remote file. Falls back to `sudo cat` if SFTP lacks permissions. Returns `FileContent`.

A read returns at most `max_bytes` bytes and is always trimmed back to a character
boundary, so the decoded `content` is never split mid-character.

- `start_line` starts the read at a 1-based line and makes every window end on a
  line boundary; resume with `next_start_line` to continue without gaps or overlap.
- `offset` starts the read at a byte position and is the cheap resume path for
  large files; resume with `next_offset`.
- `offset` and `start_line > 1` are mutually exclusive. Line windows need a
  newline-compatible encoding (a newline must encode to a single `0x0A` byte).
- The sudo fallback base64-wraps its payload, so the byte cap cannot split a
  character on the wire, and a failed `sudo sed`/`sudo tail` is reported as an
  error instead of empty content.

### `write_file`

```python
def write_file(
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
) -> str
```

Write content to a remote file. Returns a status message string.

### `list_sessions`

```python
def list_sessions(self) -> list[SessionInfo]
```

### `close_session` / `close_all`

```python
def close_session(self, conn: ConnectionParams) -> None
def close_all(self) -> None
```

### `get_diagnostics`

```python
def get_diagnostics(self, conn: ConnectionParams) -> SessionDiagnostics
```

### `reset_prompt`

```python
def reset_prompt(self, conn: ConnectionParams) -> bool
```

Re-detect the shell prompt after context-changing commands (e.g., `ssh` to another host, `sudo -i`).

### `get_health_report` / `get_perf_metrics`

```python
def get_health_report(self) -> dict[str, Any]
def get_perf_metrics(self) -> dict[str, Any]
```

---

## Error Handling

### `SSHError`

Extends `Exception`. All service-layer errors are raised as this type.

| Field         | Type            | Description                              |
| ------------- | --------------- | ---------------------------------------- |
| `category`    | `ErrorCategory` | Machine-readable category                |
| `message`     | `str`           | Human-readable summary                   |
| `detail`      | `str`           | Technical detail (original error string) |
| `hint`        | `str`           | Suggested fix                            |
| `recoverable` | `bool`          | Whether retrying may succeed             |

### `ErrorCategory`

| Value        | Meaning                                  |
| ------------ | ---------------------------------------- |
| `NETWORK`    | Host unreachable, connection refused     |
| `AUTH`       | Bad credentials, key rejected            |
| `TIMEOUT`    | Command or connection timed out          |
| `PERMISSION` | Insufficient privileges (file ops, sudo) |
| `COMMAND`    | Invalid command syntax                   |
| `PROTOCOL`   | SSH protocol mismatch                    |
| `UNKNOWN`    | Uncategorized                            |

```python
try:
    svc.execute(conn, "rm /etc/shadow")
except SSHError as e:
    print(f"{e.category.value}: {e.message}")  # e.g., permission: Cannot read /etc/shadow
```

---

## CLI Reference

```
mcp-ssh-reloaded [MODE]

Modes:
  serve mcp          Start MCP stdio server (default when no mode given)
  exec <host> <cmd>  Execute a command directly (no MCP involved)
  list               List active SSH sessions
  close <host>       Close a specific session
  close-all          Close all sessions
```

### `exec` options

| Flag              | Description                             |
| ----------------- | --------------------------------------- |
| `-u, --user`      | SSH username                            |
| `-p, --port`      | SSH port (default 22)                   |
| `-k, --key`       | Path to SSH private key                 |
| `--sudo-password` | Sudo password                           |
| `-t, --timeout`   | Command timeout in seconds (default 30) |

### `close` options

| Flag         | Description           |
| ------------ | --------------------- |
| `-u, --user` | SSH username          |
| `-p, --port` | SSH port (default 22) |

---

## MCP Server Tools

When running `mcp-ssh-reloaded serve mcp`, the following tools are exposed to AI agents:

| Tool                           | Description                                  |
| ------------------------------ | -------------------------------------------- |
| `execute_command`              | Execute a command on a remote host           |
| `execute_command_async`        | Start a command in background                |
| `get_command_status`           | Poll an async command                        |
| `get_command_status_enhanced`  | Poll with detailed output                    |
| `list_running_commands`        | List active async commands                   |
| `list_command_history`         | List completed / failed commands             |
| `interrupt_command_by_id`      | Send Ctrl+C to a running command             |
| `send_input`                   | Send input to a running async command        |
| `send_input_by_session`        | Send input directly to a session shell       |
| `list_sessions`                | List active SSH sessions                     |
| `close_session`                | Close a specific session                     |
| `close_all_sessions`           | Close all sessions                           |
| `read_file`                    | Read a remote file (SFTP with sudo fallback; resumable windows) |
| `write_file`                   | Write content to a remote file               |
| `read_screen`                  | Capture PTY screen snapshot                  |
| `send_keys`                    | Send keystrokes to PTY (vim, nano, etc.)     |
| `execute_command_enhanced`     | Execute with streaming output support        |
| `get_session_diagnostics`      | Get session health / prompt diagnostics      |
| `reset_session_prompt`         | Re-detect shell prompt                       |
| `get_connection_health_report` | Health report for all sessions               |
| `get_performance_metrics`      | Performance stats                            |

---

## Environment Variable Override System

For production use, you can alias hostnames and store real credentials in environment variables, keeping secrets out of AI context:

| Variable                   | Description             |
| -------------------------- | ----------------------- |
| `OVRD_{alias}_HOST`        | Real hostname / IP      |
| `OVRD_{alias}_PORT`        | SSH port                |
| `OVRD_{alias}_USER`        | SSH username            |
| `OVRD_{alias}_PASS`        | SSH password            |
| `OVRD_{alias}_KEY`         | Path to SSH private key |
| `OVRD_{alias}_SUDO_PASS`   | Sudo password           |
| `OVRD_{alias}_ENABLE_PASS` | Enable password         |

**Example:**

```json
{
  "mcpServers": {
    "ssh-session": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mcp-ssh-reloaded", "serve", "mcp"],
      "env": {
        "OVRD_prod_db_HOST": "192.168.1.100",
        "OVRD_prod_db_USER": "admin",
        "OVRD_prod_db_PASS": "secret123"
      }
    }
  }
}
```

The agent then uses `host="prod_db"` without ever seeing the real credentials.

---

## Architecture

```
 User / AI Agent
       │
       ├─ CLI (__main__.py) ────► SSHService
       │
       ├─ MCP (server.py) ──────► SSHService
       │
       └─ Direct Python ────────► SSHService
                                      │
                    ┌─────────────────┼──────────────────┐
                    ▼                 ▼                   ▼
             api_types.py      SSHSessionManager     datastructures.py
           (pure data layer)   (SSH engine)         (internal types)
```

- **`api_types.py`** - Zero-dependency data declarations (what you want)
- **`SSHService`** - Public API bridging types to engine (how to do it)
- **`server.py`** - Thin MCP translation layer (no business logic)
- **`__main__.py`** - Multi-mode CLI using `SSHService` directly
