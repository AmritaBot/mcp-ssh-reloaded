# Troubleshooting

## Git diff / log 卡在 pager（less）无法退出

**现象**：在 SSH session 中执行 `git diff` 或 `git log` 时，输出被 less 接管，即使设置了 `GIT_PAGER=cat` 或使用管道 `| cat` 也可能无法绕过，导致命令挂起。

**原因**：部分环境下 Git 的 pager 配置优先级较高，`GIT_PAGER` 环境变量在非交互式 shell 中不一定生效，管道也可能被 pager 截获。

**解决方案**：使用输出重定向 `>` 写到临时文件再读取，或直接使用 `git --no-pager` 参数。

```bash
# 方案一：--no-pager 参数（推荐）
git --no-pager diff

# 方案二：重定向到文件
git diff > /tmp/diff_output && cat /tmp/diff_output
```

**相关记录**：v0.2.0 → v0.2.1 版本对比时踩坑，`GIT_PAGER=cat git diff` 和 `git diff | cat` 均无效，最终通过重定向解决。

## 集成测试全部 skipped

**现象**：`uv run pytest tests/` 全绿，但输出里大量用例是 skipped。

**原因**：集成测试以 `SSH_TEST_HOST` 是否存在作为开关，未设置时整类跳过。默认 CI（`.github/workflows/ci.yml`）不设置它，所以只跑单元测试。

**解决方案**：指向一台真实主机，或起一个本地 sshd 容器（见下节）。CI 里由
`.github/workflows/integration.yml` 自动提供目标。

## 本地 Docker sshd 测试目标

测试用镜像定义在 `.github/sshd-test/Dockerfile`。本地复现：

```bash
docker build -t mcp-ssh-test .github/sshd-test
docker run -d --name sshd-test -p 127.0.0.1:2222:22 mcp-ssh-test

export SSH_TEST_HOST=127.0.0.1 SSH_TEST_PORT=2222 \
       SSH_TEST_USER=root SSH_TEST_PASSWORD=rootpass \
       SSH_TEST_SUDO_PASSWORD=rootpass
uv run pytest tests/ \
    --ignore=tests/test_mikrotik.py \
    --ignore=tests/test_network_devices.py
```

几个容易踩的点：

- **登录 shell 必须是 bash**。`/bin/sh`（busybox ash）不支持 `{1..50}` 花括号展开，`test_large_output` 与 `test_streaming_async_output` 会因此失败。镜像里已把 root 的登录 shell 改成 `/bin/bash`。
- **`MaxStartups` 要调大**。并发用例会同时开多个会话，默认值会让 sshd 拒绝连接，表现为 `Error reading SSH protocol banner` / `EOFError`。镜像里设为 `100:30:200`。
- **`test_streaming_async_output` 只传 host、不带凭据**，走的是 `~/.ssh/config` 解析路径。需要把 `127.0.0.1` 指向容器并配好密钥登录，否则它会去连本机的 22 端口：

  ```
  Host 127.0.0.1
      HostName 127.0.0.1
      User root
      Port 2222
      IdentityFile ~/.ssh/mcp_ssh_test
      IdentitiesOnly yes
  ```

- **MikroTik / 网络设备用例需要真机**，本地容器跑不了，用 `--ignore` 排除。

## stdout 里残留命令回显与提示符

**现象**：返回的 stdout 末尾可能带上命令回显、shell 提示符，以及一段
`__mcp_status=$?; printf ...` 的 sentinel 命令。

**原因**：完成检测在 sentinel 分支按 marker 的位置截断，而 shell 回显里同样包含该 marker，于是回显的前半段被保留了下来。

**状态**：已知问题，属于输出处理（`OutputBuffer`）待重构的范畴，暂未修复。用例断言目前只做包含判断，不受影响。
