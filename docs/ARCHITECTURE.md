# Architecture

## Module layout

```
server.py                  MCP tools (fastmcp) - args <-> models, text rendering only
  └─ services.py           SSHService facade - ConnectionParams -> CommandResult
       └─ session_manager.py   SSHSessionManager - session lifecycle facade
            ├─ connection.py          ConnectionManager - ssh config, resolve, create/close
            ├─ command_executor.py    CommandExecutor - the single execution stack
            ├─ enable.py              EnableMode - network device enable mode
            ├─ file_manager.py        FileManager - SFTP read/write with sudo fallback
            ├─ enhanced_executor.py   EnhancedCommandExecutor - options layer
            ├─ session_diagnostics.py diagnostics + connection profiles
            └─ models.py              single source of truth for every data type
```

`models.py` is the only place data types are declared. `api_types.py` and
`datastructures.py` are kept as thin re-export shims so existing imports keep
working; new code should import from `models`.

## Execution pipeline

```
CommandExecutor.execute_result()              -> ExecutionResult
  execute_command_async()                     -> command_id
    (registers RunningCommand, submits _execute_command_async_worker to a thread pool)
      _execute_standard_command_internal()        unix shells: sentinel + prompt detection
      _execute_sudo_command_internal()            sudo password prompt handling
      _execute_enable_mode_command_internal()     network device enable mode
```

`execute_command()` is a thin legacy renderer: it calls `execute_result()` and
maps the structured result back onto the historical
`(stdout, stderr, exit_code)` tuple, so the MCP tool output is unchanged.

`EnhancedCommandExecutor` no longer runs its own send/collect/detect loop. It
calls `CommandExecutor` with options (`auto_extend_timeout`, `max_timeout`,
`streaming_mode`, `progress_callback`) and only contributes waiting and
formatting. `auto_extend_timeout` is purely a caller-side decision: the executor
already keeps a timed-out command alive in background monitoring.

## Result model

```python
ExecutionResult(
    status, stdout, stderr, exit_code, command_id,
    awaiting_input, sentinel, truncated, spilled_path, long_running, duration_ms,
)
```

`CommandStatus` distinguishes:

| Status           | Meaning                                                     |
| ---------------- | ----------------------------------------------------------- |
| `RUNNING`        | outlived the caller's timeout, still alive in the background |
| `AWAITING_INPUT` | blocked on a password / yes-no / pager prompt                |
| `COMPLETED`      | finished, `exit_code` carries the result                     |
| `FAILED`         | could not be executed at all                                 |
| `INTERRUPTED`    | cancelled by the caller                                      |
| `STREAMING`      | long-running command with streaming output                   |

## Known gaps

- The three `_execute_*_internal` methods still emit the legacy `124` exit code
  and the `"Command requires input: "` stderr prefix at their return boundary.
  `_result_from_legacy()` is the single place that interprets them; nothing
  downstream looks at magic numbers or string prefixes any more.
- The sentinel branch truncates at the marker position, so the echoed sentinel
  command line can leak into `stdout`.
- `CompletionStrategy` / `PromptDetector` / `InputDetector` have not been
  extracted yet; the heuristics still live in `CommandExecutor`.
- `OutputBuffer` (head + tail truncation with spill to disk) is not implemented;
  `OutputLimiter` truncates in place and borrows exit code `124`.
- Per-session state is still held directly by `SSHSessionManager` rather than a
  `SessionRegistry` with read-only accessors.
