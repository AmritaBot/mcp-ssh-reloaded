"""Single owner of every piece of per-session state.

Before this existed the state lived as a dozen loose dicts on
``SSHSessionManager`` and every collaborator reached into them
(``sm._sessions``, ``sm._session_prompts``, ...), which is why the refactor
plan flagged "shared mutable state with no owner" (P8).

The registry keeps the same dicts - so existing call sites keep working - but
they now have one owner, public names, and a documented lifecycle.  It also
hands out a per-session execution lock, so only one reader can hold a shell at
a time.
"""

from __future__ import annotations

import re
import threading
from typing import TYPE_CHECKING, Any

import aiologic

if TYPE_CHECKING:
    import paramiko
    import pyte


class SessionRegistry:
    """Owns per-session state and the per-session execution locks."""

    def __init__(self) -> None:
        self.lock = aiologic.Lock()

        self.sessions: dict[str, paramiko.SSHClient] = {}
        self.shells: dict[str, paramiko.Channel] = {}
        self.shell_types: dict[str, str] = {}
        self.prompt_patterns: dict[str, re.Pattern] = {}
        self.prompts: dict[str, str] = {}
        self.prompt_miss_count: dict[str, int] = {}
        self.enable_mode: dict[str, bool] = {}
        self.emulators: dict[str, tuple[pyte.Screen, pyte.Stream]] = {}
        self.modes: dict[str, str] = {}
        self.active_commands: dict[str, Any] = {}
        self.log_rate_limits: dict[str, float] = {}

        self._exec_locks: dict[str, threading.Lock] = {}
        self._exec_locks_guard = threading.Lock()

    # -- read-only views ---------------------------------------------------

    def session_keys(self) -> list[str]:
        with self.lock:
            return list(self.sessions.keys())

    def client(self, session_key: str) -> paramiko.SSHClient | None:
        return self.sessions.get(session_key)

    def shell(self, session_key: str) -> paramiko.Channel | None:
        return self.shells.get(session_key)

    def prompt(self, session_key: str) -> str | None:
        return self.prompts.get(session_key)

    def shell_type(self, session_key: str) -> str:
        return self.shell_types.get(session_key, "unknown")

    # -- per-session execution lock ---------------------------------------

    def execution_lock(self, session_key: str) -> threading.Lock:
        """Return the lock that serialises readers of one session's shell."""
        with self._exec_locks_guard:
            lock = self._exec_locks.get(session_key)
            if lock is None:
                lock = threading.Lock()
                self._exec_locks[session_key] = lock
            return lock

    # -- lifecycle ---------------------------------------------------------

    def forget(self, session_key: str) -> None:
        """Drop every trace of a session (called when it is closed)."""
        self.shell_types.pop(session_key, None)
        self.prompt_patterns.pop(session_key, None)
        self.prompts.pop(session_key, None)
        self.prompt_miss_count.pop(session_key, None)
        self.emulators.pop(session_key, None)
        self.modes.pop(session_key, None)
        self.active_commands.pop(session_key, None)
        self.enable_mode.pop(session_key, None)
        with self._exec_locks_guard:
            lock = self._exec_locks.get(session_key)
            # Keep the lock while a worker holds it, or a reader could slip in.
            if lock is None or not lock.locked():
                self._exec_locks.pop(session_key, None)
        for key in [
            k
            for k in list(self.log_rate_limits.keys())
            if k.startswith(f"{session_key}_")
        ]:
            del self.log_rate_limits[key]

    def clear(self) -> None:
        """Drop all per-session state (used on shutdown)."""
        self.shells.clear()
        self.sessions.clear()
        self.enable_mode.clear()
        self.shell_types.clear()
        self.prompt_patterns.clear()
        self.prompts.clear()
        self.prompt_miss_count.clear()
        self.emulators.clear()
        self.modes.clear()
        self.active_commands.clear()
        self.log_rate_limits.clear()
        with self._exec_locks_guard:
            self._exec_locks.clear()
