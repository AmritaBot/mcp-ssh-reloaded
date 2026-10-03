# MCP SSH Session - Reloaded

An MCP (Model Context Protocol) server that enables AI agents to establish and manage persistent SSH sessions.

## Features

- **Smart Command Execution** - auto-transitions to async mode if timeout is reached
- **Persistent Sessions** - SSH connections reused across commands
- **Async Commands** - non-blocking execution for long-running tasks
- **SSH Config Support** - reads `~/.ssh/config` for aliases, ports, keys
- **Multi-host** - manage connections to multiple hosts simultaneously
- **Network Devices** - enable mode handling for Cisco, Juniper, MikroTik, etc.
- **Sudo Support** - automatic password handling for Unix/Linux hosts
- **File Operations** - read/write remote files via SFTP (sudo fallback)
- **Command Interruption** - send Ctrl+C to stop running commands
- **Thread-safe** - safe for concurrent operations
- **Async Native** - built-in support for asynio.

## Quick Start

### 📦 Migrating from `mcp-ssh-session`

This project is a drop-in replacement for the original [`devnullvoid/mcp-ssh-session`](https://github.com/devnullvoid/mcp-ssh-session). To migrate:

1. **Replace the package name** - `mcp-ssh-session` → `mcp-ssh-reloaded`
2. **Environment variables fully inherited** - all `OVRD_*` and `MCP_SSH_*` env vars work the same
3. **That's it** - reconnect and you're done

<table>
<tr><th>Before (old)</th><th>After (new)</th></tr>
<tr><td>

```json
{
  "mcpServers": {
    "ssh-session": {
      "command": "uvx",
      "args": ["mcp-ssh-session", "serve", "mcp"]
    }
  }
}
```

</td><td>

```json
{
  "mcpServers": {
    "ssh-session": {
      "command": "uvx",
      "args": ["mcp-ssh-reloaded", "serve", "mcp"]
    }
  }
}
```

</td></tr>
</table>

### Install

```bash
uvx mcp-ssh-reloaded
```

### Development

```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```

### CLI

```bash
# MCP server (default)
mcp-ssh-reloaded serve mcp

# Direct execution (no MCP)
mcp-ssh-reloaded exec myserver "uname -a" -u admin

# List / close sessions
mcp-ssh-reloaded list
mcp-ssh-reloaded close-all
```

### MCP Client Config

**Claude Code / Desktop** (`~/.claude.json` or `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "ssh-session": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mcp-ssh-reloaded", "serve", "mcp"],
      "env": {}
    }
  }
}
```

### Quick Examples

```json
// SSH config alias
{ "host": "myserver", "command": "uptime" }

// Explicit params
{ "host": "example.com", "username": "user", "command": "ls -la", "port": 2222 }

// Network device (Cisco enable mode)
{ "host": "router", "username": "admin", "enable_password": "secret", "command": "show run" }

// Unix with sudo
{ "host": "server", "username": "ops", "sudo_password": "secret", "command": "systemctl restart nginx" }
```

---

> **Full API reference:** [API-DOCS.md](./API-DOCS.md) - all types, all methods, all MCP tools, error handling, server config tunables.

## SSH Config

`~/.ssh/config` is read automatically:

```
Host myserver
    HostName example.com
    User myuser
    Port 2222
    IdentityFile ~/.ssh/id_rsa
```

Then use `"host": "myserver"` - the rest is resolved for you.

## Credential Hiding (OVRD\_\*)

For production environments, store real credentials in env vars so AI agents only see aliases:

| Variable                   | Description             |
| -------------------------- | ----------------------- |
| `OVRD_{alias}_HOST`        | Real hostname or IP     |
| `OVRD_{alias}_PORT`        | SSH port                |
| `OVRD_{alias}_USER`        | SSH username            |
| `OVRD_{alias}_PASS`        | SSH password            |
| `OVRD_{alias}_KEY`         | Path to SSH private key |
| `OVRD_{alias}_SUDO_PASS`   | Sudo password           |
| `OVRD_{alias}_ENABLE_PASS` | Enable password         |

**Example config:**

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
        "OVRD_prod_db_PASS": "secret123",
        "OVRD_prod_db_SUDO_PASS": "sudopass"
      }
    }
  }
}
```

The agent uses `"host": "prod_db"` - never sees real IPs or passwords.

## Configuration

### Timeouts & server settings

All tunables live in `ServerConfig`. Values resolve in priority:

1. **Constructor kwargs** (highest)
2. **`MCP_SSH_*` environment variables**
3. **Defaults** (lowest)

| Env Variable                             | Default                     | Description                         |
| ---------------------------------------- | --------------------------- | ----------------------------------- |
| `MCP_SSH_DEFAULT_TIMEOUT`                | `30`                        | Command timeout (seconds)           |
| `MCP_SSH_MAX_TIMEOUT`                    | `300`                       | Hard cap on timeout                 |
| `MCP_SSH_CONNECT_TIMEOUT`                | `30`                        | SSH connect timeout                 |
| `MCP_SSH_MAX_WORKERS`                    | `10`                        | Thread pool size                    |
| `MCP_SSH_MAX_FILE_BYTES`                 | `2097152`                   | Max file read/write (2 MB)          |
| `MCP_SSH_MAX_OUTPUT_BYTES`               | `10485760`                  | Max command output (10 MB)          |
| `MCP_SSH_INTERACTIVE_MODE`               | `true`                      | Enable PTY terminal emulation       |
| `MCP_SSH_PTY_AWARE_VALIDATION`           | `false`                     | Relax validation for PTY inspection |
| `MCP_SSH_MIKROTIK_AUTO_PAGING`           | `true`                      | Auto-handle MikroTik pagers         |
| `MCP_SSH_TERMINAL_WIDTH`                 | `100`                       | PTY columns                         |
| `MCP_SSH_TERMINAL_HEIGHT`                | `24`                        | PTY rows                            |
| `MCP_SSH_LOG_DIR`                        | `/tmp/mcp_ssh_session_logs` | Log directory                       |
| `MCP_SSH_BACKGROUND_MONITOR_MAX_TIMEOUT` | `300`                       | Background monitor max timeout      |
| `MCP_SSH_NORMAL_IDLE_TIMEOUT`            | `2`                         | Normal idle timeout (seconds)       |
| `MCP_SSH_PACKAGE_MANAGER_IDLE_TIMEOUT`   | `10`                        | Package manager idle timeout        |
| `MCP_SSH_ASYNC_DEFAULT_TIMEOUT`          | `30`                        | Async command default timeout       |

### Via CLI (serve modes)

```bash
mcp-ssh serve mcp --default-timeout 60 --max-workers 20
mcp-ssh serve http --port 8080 --interactive-mode false
mcp-ssh serve sse --port 9000 --connect-timeout 15
```

### Via API

```python
from mcp_ssh_reloaded import SSHService, ServerConfig

svc = SSHService(config=ServerConfig(default_timeout=60, max_timeout=600))
```

## How It Works

Commands run inside persistent interactive shells:

- **Directory persists**: `cd /tmp` stays in `/tmp` for the next command
- **Env vars persist**: `export FOO=bar` is visible across commands
- **Prompt detection**: completion detected via captured prompt or idle timeout (2 s)
- **Session recovery**: stuck shells auto-reset after repeated prompt-detection failures

## Testing

```bash
# unit tests only - the integration suites self-skip without SSH_TEST_HOST
uv run pytest tests/

# everything, against a real host
SSH_TEST_HOST=myserver SSH_TEST_USER=admin SSH_TEST_PASSWORD=secret uv run pytest tests/

# or spin up a throw-away sshd container (the same image CI uses)
docker build -t mcp-ssh-test .github/sshd-test
docker run -d --name sshd-test -p 127.0.0.1:2222:22 mcp-ssh-test
SSH_TEST_HOST=127.0.0.1 SSH_TEST_PORT=2222 SSH_TEST_USER=root \
SSH_TEST_PASSWORD=rootpass SSH_TEST_SUDO_PASSWORD=rootpass \
uv run pytest tests/ --ignore=tests/test_mikrotik.py --ignore=tests/test_network_devices.py
```

`test_mikrotik.py` and `test_network_devices.py` need physical network gear.

`.github/workflows/integration.yml` runs the container-backed suite on every
pull request; `.github/workflows/ci.yml` covers unit tests, pyright, ruff and
packaging across Python 3.10-3.13.

## Architecture

Types are declared in a single module (`models.py`); `api_types.py` and
`datastructures.py` are backward-compatible re-export shims. All execution goes
through one stack, `CommandExecutor`, which returns a structured
`ExecutionResult`. See [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) for the
module map, the execution pipeline and the known gaps.

## Docs

| Doc                                                        | Topic                                                                  |
| ---------------------------------------------------------- | ---------------------------------------------------------------------- |
| [API-DOCS.md](./API-DOCS.md)                               | Full API reference - types, SSHService methods, MCP tools, error model |
| [docs/AGENT_GUIDE.md](./docs/AGENT_GUIDE.md)               | **Agent prompt** - patterns for correct tool usage, async handling     |
| [docs/ASYNC_COMMANDS.md](./docs/ASYNC_COMMANDS.md)         | Smart execution & async command lifecycle                              |
| [docs/INTERACTIVE_MODE.md](./docs/INTERACTIVE_MODE.md)     | Terminal emulation, screen snapshots, key sending                      |
| [docs/SAFETY_PROTECTIONS.md](./docs/SAFETY_PROTECTIONS.md) | Limits, timeouts, session recovery, error handling                     |
| [docs/DOCKER.md](./docs/DOCKER.md)                         | Running via Docker                                                     |
| [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)             | Module map, execution pipeline, known gaps                             |


## License

MIT - see [LICENSE](./LICENSE).

## Fork

Fork of [devnullvoid/mcp-ssh-session](https://github.com/devnullvoid/mcp-ssh-session) with significant refactoring by [AmritaConstant](https://github.com/AmritaBot).
