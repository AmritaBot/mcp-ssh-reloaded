# Agent Guide: Using mcp-ssh Effectively

You are an AI agent with SSH access to remote hosts via `mcp-ssh`.
This guide covers the patterns you MUST follow to be a good remote operator.

## Golden Rules

1. **One command at a time** per session. Don't submit a second command while the first is still running.
2. **Async = fire-and-forget-until-done**. When a command goes async, DO NOT poll it in a tight loop. Wait a reasonable interval (5-30s depending on the task) before checking status.
3. **Close sessions when done**. Don't leave dangling connections.
4. **Hostnames are aliases**. You see `"prod_db"` - the real IP and credentials are resolved server-side via env vars. Don't ask for them.

## Tool Quick Reference

### Core execution

| Tool                      | When to use                                                                                       |
| ------------------------- | ------------------------------------------------------------------------------------------------- |
| `execute_command`         | **Default choice.** Run a command, wait for result. Auto-goes async if it hits timeout.           |
| `execute_command_async`   | Known long-running commands (backups, builds, large transfers). Returns `command_id` immediately. |
| `get_command_status`      | Check on an async command. **Don't call this in a loop every second.**                            |
| `interrupt_command_by_id` | Ctrl+C a runaway command.                                                                         |
| `send_input`              | Respond to an `AWAITING_INPUT` command (password prompts, confirmations).                         |
| `list_running_commands`   | Find forgotten async commands.                                                                    |
| `list_command_history`    | See recent completed/failed commands.                                                             |

### Sessions

| Tool                 | When to use                                                    |
| -------------------- | -------------------------------------------------------------- |
| `list_sessions`      | Check what's connected.                                        |
| `close_session`      | Done with a host. If you're going to reconnect, just close it. |
| `close_all_sessions` | Cleanup at end of task.                                        |

### Files

| Tool         | When to use                                                         |
| ------------ | ------------------------------------------------------------------- |
| `read_file`  | Read a remote file (SFTP, falls back to `sudo cat`). Long files return a UTF-8-safe line window; resume with `start_line`. |
| `write_file` | Write/create/append a remote file (SFTP, falls back to `sudo tee`). |

### Interactive / PTY

| Tool          | When to use                                                  |
| ------------- | ------------------------------------------------------------ |
| `read_screen` | See what's on the terminal (vim, less, top).                 |
| `send_keys`   | Send keystrokes (`<esc>:wq<enter>`, `<ctrl-c>`, arrow keys). |

### Diagnostics

| Tool                      | When to use                                                              |
| ------------------------- | ------------------------------------------------------------------------ |
| `get_session_diagnostics` | Prompt stopped working? Shell acting weird? Check health.                |
| `reset_session_prompt`    | After `sudo -i`, `ssh jumpbox`, or `su -` the prompt changes - reset it. |

## Pattern: Sync Command (normal)

```
1. execute_command(host="myserver", command="systemctl status nginx")
2. Read stdout.  Done.
```

## Pattern: Async Command (long-running)

### CORRECT

```
1. cmd_id = execute_command_async(host="myserver", command="dnf update -y", timeout=600)
2. Wait 60 seconds (package updates take time)
3. get_command_status(cmd_id)
4. If still "running", wait another 30s and check again.
   If "completed", read stdout.
   If "failed", read stderr.
```

### WRONG - DO NOT DO THIS

```
# Polling every 2 seconds for 5 minutes = 150 useless tool calls
while True:
    s = get_command_status(cmd_id)
    if s["status"] != "running": break
    sleep(2)
```

## Pattern: Sync → Auto-Async

```
1. r = execute_command(host="myserver", command="dnf update -y", timeout=30)
2. If r.status == "RUNNING":
      Wait 60s, then get_command_status(r.command_id)
   Else:
      Already done - read r.stdout.
```

## Pattern: Interactive Prompt

```
1. r = execute_command(host="router", command="clear counters", timeout=10)
2. r.status == "AWAITING_INPUT"  →  "Confirm? [y/N]"
3. send_input(r.command_id, "y\n")
4. Wait a few seconds, then get_command_status(r.command_id)
```

## Pattern: Context-Changing Commands

When you run `sudo -i`, `ssh other-host`, or `su -`, the shell prompt changes. After such commands:

```
1. execute_command(host="myserver", command="sudo -i")
2. reset_session_prompt(host="myserver")
3. Now execute commands normally.
```

## Tips

- **MCP client timeouts**: Claude and similar clients have ~60s HTTP timeouts. If you expect a command to take longer, use `execute_command_async` directly - avoid the client timeout entirely.
- **Package managers**: `apt update && apt install`, `dnf update`, etc. WILL go async. Use `execute_command_async` with a generous timeout (300-600s) and check back every 30-60s.
- **Sudo**: If the host needs `sudo`, set `sudo_password` in the connection parameters. The server handles the password prompt automatically.
- **Network devices**: For Cisco/Juniper/MikroTik, provide `enable_password`. The server auto-enters enable mode.
- **Pagers kill output**: `git log`, `systemctl status`, MikroTik print - these trigger pagers. The server auto-handles them (sends `q`), but if you see `(END)`, use `send_keys(host, keys="q")`.
- **Dead sessions**: If commands start failing, run `get_session_diagnostics`. If `connection_health` is `"dead"`, close and reopen the session.
