# MCP SSH Session - Reloaded

一个 MCP（模型上下文协议）服务器，让 AI 代理能够建立和管理持久化的 SSH 会话。

## 特性

- **智能命令执行** - 超时后自动切换为异步模式
- **持久化会话** - SSH 连接在多次命令间复用
- **异步命令** - 长时间任务非阻塞执行
- **SSH Config 支持** - 自动读取 `~/.ssh/config` 解析别名、端口、密钥
- **多主机** - 同时管理到多个主机的连接
- **网络设备** - 支持 Cisco、Juniper、MikroTik 等设备的 enable 模式
- **Sudo 支持** - Unix/Linux 主机自动处理 sudo 密码
- **文件操作** - 通过 SFTP 读写远程文件（无权限时回退到 sudo）
- **命令中断** - 发送 Ctrl+C 停止运行中的命令
- **线程安全** - 并发操作安全
- **异步原生** - 原生asyncio优先

## 快速开始

### 📦 从 `mcp-ssh-session` 迁移

本项目是原版 [`devnullvoid/mcp-ssh-session`](https://github.com/devnullvoid/mcp-ssh-session) 的即插即用替代。只需两步：

1. **改包名** - `mcp-ssh-session` → `mcp-ssh-reloaded`
2. **环境变量完全继承** - 所有 `OVRD_*` 和 `MCP_SSH_*` 环境变量用法不变
3. **这就完了** - 重连即可

<table>
<tr><th>迁移前（旧）</th><th>迁移后（新）</th></tr>
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

### 安装

```bash
uvx mcp-ssh-reloaded
```

### 开发环境

```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```

### CLI

```bash
# MCP 服务器（默认）
mcp-ssh-reloaded serve mcp

# 直接执行（不走 MCP）
mcp-ssh-reloaded exec myserver "uname -a" -u admin

# 列出 / 关闭会话
mcp-ssh-reloaded list
mcp-ssh-reloaded close-all
```

### MCP 客户端配置

**Claude Code / Desktop**（`~/.claude.json` 或 `claude_desktop_config.json`）：

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

### 快速示例

```json
// SSH config 别名
{ "host": "myserver", "command": "uptime" }

// 显式参数
{ "host": "example.com", "username": "user", "command": "ls -la", "port": 2222 }

// 网络设备（Cisco enable 模式）
{ "host": "router", "username": "admin", "enable_password": "secret", "command": "show run" }

// Unix 使用 sudo
{ "host": "server", "username": "ops", "sudo_password": "secret", "command": "systemctl restart nginx" }
```

---

> **完整 API 参考：** [API-DOCS.md](./API-DOCS.md) - 所有类型、方法、MCP 工具、错误处理、服务器配置项。

## SSH Config

自动读取 `~/.ssh/config`：

```
Host myserver
    HostName example.com
    User myuser
    Port 2222
    IdentityFile ~/.ssh/id_rsa
```

然后只需 `"host": "myserver"` - 其余参数自动解析。

## 凭据隐藏（OVRD\_\*）

生产环境中，将真实凭据存入环境变量，AI 代理只能看到别名：

| 变量                       | 说明            |
| -------------------------- | --------------- |
| `OVRD_{alias}_HOST`        | 真实主机名或 IP |
| `OVRD_{alias}_PORT`        | SSH 端口        |
| `OVRD_{alias}_USER`        | SSH 用户名      |
| `OVRD_{alias}_PASS`        | SSH 密码        |
| `OVRD_{alias}_KEY`         | SSH 私钥路径    |
| `OVRD_{alias}_SUDO_PASS`   | Sudo 密码       |
| `OVRD_{alias}_ENABLE_PASS` | Enable 模式密码 |

**配置示例：**

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

代理使用 `"host": "prod_db"` - 永远看不到真实 IP 或密码。

## 配置

### 超时与服务器设置

所有可调参数集中在 `ServerConfig`（基于 Pydantic `BaseSettings`）。取值优先级：

1. **构造函数参数**（最高）
2. **`MCP_SSH_*` 环境变量**
3. **默认值**（最低）

| 环境变量                                 | 默认值                      | 说明                   |
| ---------------------------------------- | --------------------------- | ---------------------- |
| `MCP_SSH_DEFAULT_TIMEOUT`                | `30`                        | 命令超时（秒）         |
| `MCP_SSH_MAX_TIMEOUT`                    | `300`                       | 超时硬上限             |
| `MCP_SSH_CONNECT_TIMEOUT`                | `30`                        | SSH 连接超时           |
| `MCP_SSH_MAX_WORKERS`                    | `10`                        | 线程池大小             |
| `MCP_SSH_MAX_FILE_BYTES`                 | `2097152`                   | 文件读写上限（2MB）    |
| `MCP_SSH_MAX_OUTPUT_BYTES`               | `10485760`                  | 命令输出上限（10MB）   |
| `MCP_SSH_INTERACTIVE_MODE`               | `true`                      | 启用 PTY 终端仿真      |
| `MCP_SSH_PTY_AWARE_VALIDATION`           | `false`                     | PTY 检查时放宽校验     |
| `MCP_SSH_MIKROTIK_AUTO_PAGING`           | `true`                      | 自动处理 MikroTik 分页 |
| `MCP_SSH_TERMINAL_WIDTH`                 | `100`                       | PTY 列数               |
| `MCP_SSH_TERMINAL_HEIGHT`                | `24`                        | PTY 行数               |
| `MCP_SSH_LOG_DIR`                        | `/tmp/mcp_ssh_session_logs` | 日志目录               |
| `MCP_SSH_BACKGROUND_MONITOR_MAX_TIMEOUT` | `300`                       | 后台监控最大超时       |
| `MCP_SSH_NORMAL_IDLE_TIMEOUT`            | `2`                         | 普通命令空闲超时       |
| `MCP_SSH_PACKAGE_MANAGER_IDLE_TIMEOUT`   | `10`                        | 包管理器空闲超时       |
| `MCP_SSH_ASYNC_DEFAULT_TIMEOUT`          | `30`                        | 异步命令默认超时       |

### CLI 方式（serve 模式）

```bash
mcp-ssh serve mcp --default-timeout 60 --max-workers 20
mcp-ssh serve http --port 8080 --interactive-mode false
mcp-ssh serve sse --port 9000 --connect-timeout 15
```

### API 方式

```python
from mcp_ssh_reloaded import SSHService, ServerConfig

svc = SSHService(config=ServerConfig(default_timeout=60, max_timeout=600))
```

## 工作原理

命令在持久化的交互式 Shell 中运行：

- **目录持久化**：`cd /tmp` 后下次命令仍在 `/tmp`
- **环境变量持久化**：`export FOO=bar` 在多次命令间可见
- **提示符检测**：通过捕获的提示符或空闲超时（2 秒）判断命令完成
- **会话恢复**：多次提示符检测失败后自动 Ctrl+C 重置卡住的 Shell

## 测试

```bash
# 只跑单元测试 - 没有 SSH_TEST_HOST 时集成测试会自动跳过
uv run pytest tests/

# 跑全部，针对一台真实主机
SSH_TEST_HOST=myserver SSH_TEST_USER=admin SSH_TEST_PASSWORD=secret uv run pytest tests/

# 或者起一个一次性的 sshd 容器（与 CI 使用的是同一个镜像）
docker build -t mcp-ssh-test .github/sshd-test
docker run -d --name sshd-test -p 127.0.0.1:2222:22 mcp-ssh-test
SSH_TEST_HOST=127.0.0.1 SSH_TEST_PORT=2222 SSH_TEST_USER=root \
SSH_TEST_PASSWORD=rootpass SSH_TEST_SUDO_PASSWORD=rootpass \
uv run pytest tests/ --ignore=tests/test_mikrotik.py --ignore=tests/test_network_devices.py
```

`test_mikrotik.py` 与 `test_network_devices.py` 需要真实网络设备。

`.github/workflows/integration.yml` 会在每个 PR 上跑容器版集成测试；
`.github/workflows/ci.yml` 负责单元测试、pyright、ruff 与打包，覆盖 Python 3.10-3.13。

## 架构

类型集中声明在单一模块（`models.py`），`api_types.py` 与 `datastructures.py`
退化为向后兼容的 re-export 壳。所有执行都走同一条栈 `CommandExecutor`，
返回结构化的 `ExecutionResult`。模块图、执行流水线与已知遗留见
[docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)。

## 文档

| 文档                                                       | 主题                                                      |
| ---------------------------------------------------------- | --------------------------------------------------------- |
| [API-DOCS.md](./API-DOCS.md)                               | 完整 API 参考 - 类型、SSHService 方法、MCP 工具、错误模型 |
| [docs/AGENT_GUIDE.md](./docs/AGENT_GUIDE.md)               | **代理提示词** - 正确使用工具的模式、异步处理             |
| [docs/ASYNC_COMMANDS.md](./docs/ASYNC_COMMANDS.md)         | 智能执行与异步命令生命周期                                |
| [docs/INTERACTIVE_MODE.md](./docs/INTERACTIVE_MODE.md)     | 终端仿真、屏幕快照、按键发送                              |
| [docs/SAFETY_PROTECTIONS.md](./docs/SAFETY_PROTECTIONS.md) | 限制、超时、会话恢复、错误处理                            |
| [docs/DOCKER.md](./docs/DOCKER.md)                         | Docker 运行指南                                           |
| [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)             | 模块图、执行流水线、已知遗留                              |


## License

MIT - 见 [LICENSE](./LICENSE)。

## Fork

Fork 自 [devnullvoid/mcp-ssh-session](https://github.com/devnullvoid/mcp-ssh-session)，由 [AmritaConstant](https://github.com/AmritaBot) 进行大量重构。
