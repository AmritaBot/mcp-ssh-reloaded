"""SSH session manager - composition root that delegates to sub-modules.

connection.py  → ConnectionManager (SSH config, resolve, create, close, list)
enable.py       → EnableMode       (enable mode for network devices)

The remaining shell/prompt/exec/emulator methods live here for now;
they may be extracted to shell.py in a future refactoring pass.
"""

import asyncio
import logging
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any

import paramiko

try:
    NoValidConnectionsError = paramiko.NoValidConnectionsError  # pyright: ignore[reportAttributeAccessIssue]
except AttributeError:
    pass

import pyte

from .command_executor import CommandExecutor
from .connection import ConnectionManager
from .datastructures import CommandStatus
from .enable import EnableMode
from .enhanced_executor import EnhancedCommandExecutor
from .file_manager import FileManager
from .logging_manager import get_context_logger, get_logger
from .models import FileContent
from .session_diagnostics import ConnectionProfileManager, SessionDiagnosticsProvider
from .session_registry import SessionRegistry
from .validation import CommandValidator


class SSHSessionManager:
    """Manages persistent SSH sessions with safety protections."""

    # Default timeouts
    DEFAULT_COMMAND_TIMEOUT = 30
    MAX_COMMAND_TIMEOUT = 300  # 5 minutes maximum

    # Enable mode timeout
    ENABLE_MODE_TIMEOUT = 10

    # Thread pool for timeout enforcement
    MAX_WORKERS = 10

    # Time (seconds) to wait for new output before switching sync commands to async
    SYNC_IDLE_TO_ASYNC = 2.0

    # Maximum bytes allowed for file read/write operations (2MB)
    MAX_FILE_TRANSFER_SIZE = 2 * 1024 * 1024

    def __init__(self, config=None):
        # All per-session state lives here; see session_registry.py
        self.registry = SessionRegistry()
        from .api_types import ServerConfig

        self.config = config if config is not None else ServerConfig()
        self._ssh_config = ConnectionManager.load_ssh_config()
        self._command_validator = CommandValidator()
        self._max_completed_commands = 100  # Keep last 100 completed commands

        # Terminal emulator support (enabled by default in v0.2.0+)
        self._interactive_mode = os.environ.get("MCP_SSH_INTERACTIVE_MODE", "1") == "1"
        self._pty_aware_validation = (
            os.environ.get("MCP_SSH_PTY_AWARE_VALIDATION", "0") == "1"
        )
        self._mikrotik_auto_without_paging = (
            os.environ.get("MCP_SSH_MIKROTIK_AUTO_WITHOUT_PAGING", "1") == "1"
        )

        # Setup optimized logging
        self.logger = get_logger("ssh_session")
        self.context_logger = get_context_logger("ssh_session")
        self.logger.info("SSHSessionManager initialized with enhanced logging")

        #  Extracted sub-modules
        self.connection = ConnectionManager(self)
        self.enable_mode = EnableMode(self)

        # Initialize enhanced components
        self.enhanced_executor = EnhancedCommandExecutor(self)
        self.session_diagnostics = SessionDiagnosticsProvider(self)
        self.connection_profiles = ConnectionProfileManager(self)

        if self._interactive_mode:
            self.logger.info("Interactive PTY mode enabled")
        if self._pty_aware_validation:
            self.logger.info(
                "PTY-aware command validation enabled (MCP_SSH_PTY_AWARE_VALIDATION=1)"
            )
        if self._mikrotik_auto_without_paging:
            self.logger.info(
                "MikroTik auto without-paging enabled (MCP_SSH_MIKROTIK_AUTO_WITHOUT_PAGING=1)"
            )
        self.logger.info("SSHSessionManager initialized")

        self.command_executor = CommandExecutor(self, config=self.config)
        self.file_manager = FileManager(self)

    #  Read-only views (collaborators used to reach into privates)

    @property
    def command_validator(self):
        """The shared command validator."""
        return self._command_validator

    @property
    def interactive_mode(self) -> bool:
        """Whether the PTY terminal emulator is enabled."""
        return self._interactive_mode

    @property
    def pty_aware_validation(self) -> bool:
        """Whether validation is relaxed for PTY inspection commands."""
        return self._pty_aware_validation

    #  Delegates to ConnectionManager

    def _load_ssh_config(self):
        return ConnectionManager.load_ssh_config()

    def _resolve_connection(self, host, username, port):
        return self.connection.resolve_connection(host, username, port)

    def _get_env_override(self, host, param, default=None):
        return ConnectionManager.get_env_override(host, param, default)

    async def get_or_create_session(self, *a, **kw):
        return await self.connection.get_or_create_session(*a, **kw)

    async def close_session(self, host, username=None, port=None):
        await self.connection.close_session(host, username, port)

    def _close_session(self, session_key):
        self.connection._close_session(session_key)

    async def close_all_sessions(self):
        await self.connection.close_all_sessions()

    def __del__(self):
        if not hasattr(self, "logger"):
            return  # __init__ never completed (e.g. failed during tests)
        self.logger.info("SSHSessionManager destroyed, ensuring cleanup.")
        try:
            self.connection.close_all_sessions_sync()
        except Exception as e:
            self.logger.logger.error(
                f"Error during __del__ cleanup: {e}", exc_info=True
            )  # pyright: ignore[reportPossiblyUnboundVariable]
        try:
            self.command_executor.shutdown()
        except Exception as e:
            self.logger.logger.error(
                f"Error shutting down executor: {e}", exc_info=True
            )  # pyright: ignore[reportPossiblyUnboundVariable]

    async def list_sessions(self) -> list[str]:
        return await self.connection.list_sessions()

    #  Delegates to EnableMode

    async def _enter_enable_mode(
        self,
        session_key,
        client,
        enable_password,
        enable_command="enable",
        timeout=None,
    ):
        return await self.enable_mode.enter(
            session_key,
            client,
            enable_password,
            enable_command,
            timeout or self.ENABLE_MODE_TIMEOUT,
        )

    #  Shell / PTY / prompt / exec internals

    def _feed_emulator(self, session_key: str, data: str) -> None:
        """Feed data to terminal emulator if interactive mode is enabled."""
        if self._interactive_mode and session_key in self.registry.emulators:
            _, stream = self.registry.emulators[session_key]
            stream.feed(data)
            self._infer_mode_from_screen(session_key)

    def _log_debug_rate_limited(
        self, logger: logging.Logger | Any, key: str, msg: str, interval: float = 5.0
    ):
        """Log a debug message only if enough time has passed since last log with this key."""
        now = time.time()
        last_time = self.registry.log_rate_limits.get(key, 0.0)
        if now - last_time >= interval:
            self.registry.log_rate_limits[key] = now
            logger.debug(msg)

    def _infer_mode_from_screen(self, session_key: str) -> str:
        """Infer the current mode from screen content.

        Returns:
            Mode string: 'editor', 'pager', 'password_prompt', 'shell', or 'unknown'
        """
        if not self._interactive_mode or session_key not in self.registry.emulators:
            return "unknown"

        screen, _ = self.registry.emulators[session_key]

        # Get screen content
        lines = []
        for y in range(screen.lines):
            line = screen.display[y].rstrip()
            if line:
                lines.append(line)

        if not lines:
            mode = "unknown"
        else:
            last_line = lines[-1] if lines else ""
            screen_text = "\n".join(lines)

            # Check for editor (vim, nano)
            # Vim: status line with -- INSERT --, -- VISUAL --, or many ~ lines at the start of lines
            if any(
                marker in screen_text
                for marker in ["-- INSERT --", "-- VISUAL --", "-- REPLACE --"]
            ):
                mode = "editor"
            elif screen_text.count("~") > 5 and any(
                line.lstrip().startswith("~") for line in lines[-10:]
            ):
                # Many tildes at the start of lines in last 10 lines suggests vim
                mode = "editor"
            elif "GNU nano" in screen_text or "^G Get Help" in screen_text:
                mode = "editor"
            # Check for pager (less, more)
            elif "(END)" in last_line or last_line.strip() == ":":
                mode = "pager"
            elif "--More--" in last_line or "-- [Q quit|D dump" in last_line:
                mode = "pager"
            # Check for password prompt
            elif re.search(r'password[^:=\n"\']*:?\s*$', last_line, re.IGNORECASE):
                mode = "password_prompt"
            elif re.search(r'passphrase[^:=\n"\']*:?\s*$', last_line, re.IGNORECASE):
                mode = "password_prompt"
            # Check for shell prompt (has prompt pattern)
            elif session_key in self.registry.prompts:
                prompt = self.registry.prompts[session_key]
                # Handle wildcard prompts
                if "*" in prompt or "[" in prompt:
                    # Convert wildcard to regex
                    pattern_str = re.escape(prompt).replace(r"\*", ".*?")
                    pattern_str = pattern_str.replace(r"\[>#\]", "[>#]").replace(
                        r"\[\$#\]", "[$#]"
                    )
                    if re.search(pattern_str + r"\s*$", last_line):
                        mode = "shell"
                    else:
                        mode = "unknown"
                elif last_line.endswith(prompt.rstrip()):
                    mode = "shell"
                else:
                    mode = "unknown"
            else:
                mode = "unknown"

        # Store the mode
        self.registry.modes[session_key] = mode
        return mode

    def _get_screen_snapshot(self, session_key: str, max_lines: int = 24) -> dict:
        """Get a snapshot of the terminal screen state.

        Returns:
            dict with keys: lines (list of strings), cursor_x, cursor_y, width, height
        """
        if not self._interactive_mode or session_key not in self.registry.emulators:
            return {
                "error": "Interactive mode not enabled or session not found",
                "lines": [],
                "cursor_x": 0,
                "cursor_y": 0,
                "width": 0,
                "height": 0,
            }

        screen, _ = self.registry.emulators[session_key]

        # Get screen lines (pyte stores them as a dict keyed by line number)
        lines = []
        for y in range(min(max_lines, screen.lines)):
            line = screen.display[y]
            lines.append(line.rstrip())

        return {
            "lines": lines,
            "cursor_x": screen.cursor.x,
            "cursor_y": screen.cursor.y,
            "width": screen.columns,
            "height": screen.lines,
        }

    #  Shell / PTY / prompt / exec internals

    def _get_or_create_shell(
        self, session_key: str, client: paramiko.SSHClient
    ) -> paramiko.Channel:
        """Get or create (or recreate) a persistent shell for a session."""
        logger = self.logger.getChild("shell")

        if session_key in self.registry.shells:
            shell = self.registry.shells[session_key]
            try:
                transport = (
                    shell.get_transport() if hasattr(shell, "get_transport") else None
                )
                if shell.closed or not transport or not transport.is_active():
                    logger.info(f"Shell for {session_key} is dead, recreating")
                    del self.registry.shells[session_key]
                else:
                    client_ref = self.registry.sessions.get(session_key)
                    if client_ref:
                        self._ensure_shell_type(session_key, client_ref)
                        # Recapture prompt if not available
                        if session_key not in self.registry.prompts:
                            self._capture_prompt(session_key, shell)
                    return shell
            except Exception as exc:
                logger.warning(
                    f"Error checking shell for {session_key}: {exc}. Recreating."
                )
                if session_key in self.registry.shells:
                    del self.registry.shells[session_key]

        logger.info(f"Creating new persistent shell for {session_key}")
        shell = client.invoke_shell()
        shell.resize_pty(width=100, height=24)

        # Create terminal emulator if interactive mode is enabled
        if self._interactive_mode:
            screen = pyte.Screen(100, 24)
            stream = pyte.Stream(screen)
            self.registry.emulators[session_key] = (screen, stream)
            logger.debug(f"Created terminal emulator for {session_key}")

        time.sleep(2)  # Give shell time to initialize
        initial_output = ""
        # Wait up to 5 seconds for initial output/banner (some routers are slow)
        start_wait = time.time()
        while time.time() - start_wait < 5.0:
            if shell.recv_ready():
                chunk = shell.recv(4096).decode("utf-8", errors="ignore")
                logger.debug(f"Initial shell output chunk: {chunk!r}")
                initial_output += chunk
                # Feed to emulator if enabled
                if self._interactive_mode and session_key in self.registry.emulators:
                    _, stream = self.registry.emulators[session_key]
                    stream.feed(chunk)
            elif initial_output and (time.time() - start_wait > 1.0):
                # We got some output and it's quiet, maybe it's done
                break
            time.sleep(0.2)

        self.registry.shells[session_key] = shell

        # Build device profile from shell output instead of exec_command
        self._build_device_profile(session_key, initial_output)

        # Capture the actual prompt for this session
        self._capture_prompt(session_key, shell)

        # For non-POSIX Unix shells, start bash to avoid compatibility issues
        # We do this AFTER capturing the initial prompt to ensure the shell is responsive
        device_type = self.registry.shell_types.get(session_key, "unknown")
        if device_type == "unix_shell":
            # Detect non-POSIX shells (fish, nushell, elvish, etc.)
            is_non_posix = any(
                indicator in initial_output.lower()
                for indicator in ["fish", "nushell", "elvish", "xonsh"]
            )

            # If not detected from banner, specifically probe for fish shell
            if not is_non_posix:
                logger.debug(f"Probing for fish shell on {session_key}")
                # Use a probe that works in both bash and fish but produces different output
                # In fish, $FISH_VERSION is set. In bash, it's usually not.
                shell.send(b'echo "FISH_CHECK:$FISH_VERSION"\n')

                probe_output = ""
                start_time = time.time()
                while time.time() - start_time < 2.0:
                    if shell.recv_ready():
                        probe_output += shell.recv(4096).decode(
                            "utf-8", errors="ignore"
                        )
                        if "FISH_CHECK:" in probe_output and "\n" in probe_output:
                            break
                    time.sleep(0.1)

                logger.debug(f"Probe output: {probe_output!r}")

                # Look for version number pattern after FISH_CHECK:
                # Fish: FISH_CHECK:3.6.1
                # Bash: FISH_CHECK:
                if re.search(r"FISH_CHECK:\d+\.", probe_output):
                    logger.info(
                        f"Detected fish shell via probe, starting bash for {session_key}"
                    )
                    is_non_posix = True

            if is_non_posix:
                logger.info(
                    f"Starting bash for {session_key} (non-POSIX shell detected)"
                )
                shell.send(b"bash\n")
                time.sleep(0.5)
                if shell.recv_ready():
                    shell.recv(4096)  # Clear bash startup output

                # Recapture prompt for the new bash shell
                logger.info(f"Recapturing prompt for bash shell on {session_key}")
                self._capture_prompt(session_key, shell)

        logger.info(f"New shell for {session_key} is ready")
        return shell

    def _build_device_profile(self, session_key: str, initial_output: str):
        """Build device profile incrementally from shell output."""
        self.logger.getChild("device_profile")

        # Detect device type from initial output
        device_type = "unknown"
        if initial_output:
            output_lower = initial_output.lower()

            # Network device vendors
            if "mikrotik" in output_lower or "routeros" in output_lower:
                device_type = "mikrotik"
            elif "edgeswitch" in output_lower or "ubiquiti" in output_lower:
                device_type = "edgeswitch"
            elif "cisco" in output_lower or "ios" in output_lower:
                device_type = "cisco"
            elif "juniper" in output_lower or "junos" in output_lower:
                device_type = "juniper"
            elif (
                "fortinet" in output_lower
                or "fortigate" in output_lower
                or "fortios" in output_lower
            ):
                device_type = "fortinet"
            elif "arista" in output_lower or "eos" in output_lower:
                device_type = "arista"
            elif "palo alto" in output_lower or "pan-os" in output_lower:
                device_type = "paloalto"
            elif "checkpoint" in output_lower or "gaia" in output_lower:
                device_type = "checkpoint"
            elif "vyos" in output_lower or "vyatta" in output_lower:
                device_type = "vyos"
            elif "openwrt" in output_lower or "lede" in output_lower:
                device_type = "openwrt"
            # Unix/Linux shells - check for shell indicators or prompt characters
            elif any(
                indicator in output_lower
                for indicator in [
                    "fish",
                    "bash",
                    "zsh",
                    "ubuntu",
                    "debian",
                    "centos",
                    "redhat",
                    "fedora",
                    "linux",
                    "bsd",
                ]
            ) or any(prompt in initial_output for prompt in ["$", "#", "❯"]):
                device_type = "unix_shell"
            # Generic network device fallback
            elif any(
                keyword in output_lower
                for keyword in ["switch", "router", "firewall", "gateway"]
            ):
                device_type = "network_device"
            else:
                device_type = "unknown"

        self.registry.shell_types[session_key] = device_type

        # Set up prompt pattern based on device type and actual output
        self._ensure_prompt_pattern(session_key, None, initial_output)  # pyright: ignore[reportArgumentType]

    def _capture_prompt(self, session_key: str, shell: paramiko.Channel) -> str | None:
        """Capture the actual prompt string for this session by sending a marker command.

        This provides the most reliable prompt detection by capturing the exact prompt
        that appears after a known marker, regardless of custom themes or ANSI codes.

        Handles different device types:
        - Unix/Linux shells: Uses echo command with marker
        - Network devices: Sends newline and captures response
        - Generalizes prompts to handle directory changes

        Args:
            session_key: Session identifier
            shell: Interactive shell to capture prompt from

        Returns:
            Captured prompt string (ANSI-stripped), or None if capture failed
        """
        logger = self.logger.getChild("capture_prompt")

        try:
            device_type = self.registry.shell_types.get(session_key, "unknown")
            output = ""
            marker = None

            # Strategy depends on device type
            if device_type in (
                "cisco",
                "juniper",
                "fortinet",
                "arista",
                "paloalto",
                "checkpoint",
                "mikrotik",
                "edgeswitch",
                "vyos",
                "openwrt",
                "network_device",
            ):
                # Network devices: just send newline and capture what comes back
                shell.send(b"\n")
                time.sleep(0.3)

                if shell.recv_ready():
                    output = shell.recv(4096).decode("utf-8", errors="ignore")
                    logger.debug(f"Capture prompt received: {output!r}")
            else:
                # Unix/Linux shells: try echo with marker
                # Use leading space to avoid history pollution
                marker = f"__MCP_PROMPT_MARKER_{uuid.uuid4().hex[:8]}__"
                shell.send(f' echo "{marker}"\n'.encode())
                time.sleep(0.5)

                # Collect output
                start_time = time.time()
                timeout = 10.0

                while time.time() - start_time < timeout:
                    if shell.recv_ready():
                        chunk = shell.recv(4096).decode("utf-8", errors="ignore")
                        logger.debug(f"Capture prompt received chunk: {chunk!r}")
                        output += chunk

                        # Check if we've received the marker and subsequent prompt
                        if marker in output:
                            # Give a bit more time for the prompt to appear
                            time.sleep(0.3)
                            if shell.recv_ready():
                                final_chunk = shell.recv(4096).decode(
                                    "utf-8", errors="ignore"
                                )
                                output += final_chunk
                            break
                    time.sleep(0.1)

                # If marker not found, fall back to newline method
                if marker and marker not in output:
                    logger.warning(
                        f"Marker not found, trying newline method for {session_key}"
                    )
                    # Try simple newline approach
                    shell.send(b"\n")
                    time.sleep(0.5)
                    if shell.recv_ready():
                        fallback_output = shell.recv(4096).decode(
                            "utf-8", errors="ignore"
                        )
                        logger.debug(
                            f"Capture prompt fallback received: {fallback_output!r}"
                        )
                        output += fallback_output
                        marker = None  # Disable marker processing

                    # Check if we can identify the device type from the accumulated output
                    output_lower = output.lower()
                    if (
                        "mikrotik" in output_lower
                        or "routeros" in output_lower
                        or re.search(r"\[.+@.+\]\s*>", output)
                    ):
                        logger.info(
                            f"Detected MikroTik device from fallback output for {session_key}"
                        )
                        self.registry.shell_types[session_key] = "mikrotik"
                    elif "edgeswitch" in output_lower or "ubiquiti" in output_lower:
                        self.registry.shell_types[session_key] = "edgeswitch"
                    elif any(c in output_lower for c in ["cisco", "ios", ">", "#"]):
                        # Very basic check for other network devices
                        if not any(s in output_lower for s in ["bash", "zsh", "fish"]):
                            logger.info(
                                f"Suspect network device for {session_key} based on prompt/output"
                            )
                            # Don't set to mikrotik, but maybe generic network_device
                            if device_type == "unknown":
                                self.registry.shell_types[session_key] = (
                                    "network_device"
                                )

            if not output:
                logger.warning(f"No output received for {session_key}")
                return None

            # Extract the prompt
            prompt = None
            if marker and marker in output:
                # Extract prompt after marker
                parts = output.split(marker)
                if len(parts) >= 2:
                    after_marker = parts[-1]
                    clean_after = self._strip_ansi(after_marker)
                    lines = [line for line in clean_after.split("\n") if line.strip()]
                    if lines:
                        prompt = lines[-1].strip()
            else:
                # Extract prompt from simple output (no marker)
                clean_output = self._strip_ansi(output)
                lines = [line for line in clean_output.split("\n") if line.strip()]
                if lines:
                    # Last line is typically the prompt
                    prompt = lines[-1].strip()

            if not prompt:
                logger.warning(f"Empty prompt extracted for {session_key}")
                return None

            # Generalize the prompt to handle context changes (directory, etc.)
            generalized_prompt = self._generalize_prompt(prompt, logger)

            logger.info(f"Captured prompt for {session_key}: {prompt!r}")
            if generalized_prompt != prompt:
                logger.debug(f"Generalized to: {generalized_prompt!r}")

            self.registry.prompts[session_key] = generalized_prompt
            return generalized_prompt

        except Exception as exc:
            logger.logger.error(
                f"Failed to capture prompt for {session_key}: {exc}", exc_info=True
            )
            return None

    def _generalize_prompt(self, prompt: str, logger) -> str:
        """Generalize a captured prompt to handle context changes.

        Makes prompts flexible for:
        - Directory changes: [user@host ~/dir]$ -> [user@host *]$
        - Path changes: user@host:/path$ -> user@host:*$
        - MikroTik submenus: [user@host] /ip> -> [user@host] *>

        Args:
            prompt: The literal captured prompt
            logger: Logger instance

        Returns:
            Generalized prompt pattern (still a literal string with wildcards)
        """

        # Pattern 1: [user@host] path> (MikroTik style)
        if "[" in prompt and "]" in prompt and "@" in prompt:
            # Match bracketed part and whatever follows until prompt char
            match = re.search(r"^(\[[^\]]*@[^\]]*\]).*?([>#\$%]\s*)$", prompt)
            if match:
                return match.group(1) + "*" + match.group(2)

        # Pattern 2: [user@host directory]$ or [user@host directory]# (Standard Unix style)
        if "[" in prompt and "]" in prompt and ("@" in prompt or " " in prompt):
            match = re.search(r"(\[[^\]]*[@\s][^\]]*)\]([>#\$%])", prompt)
            if match:
                # Find the last space or path separator in the bracket
                bracket_content = match.group(1)
                prompt_char = match.group(2)
                if " " in bracket_content:
                    parts = bracket_content.rsplit(" ", 1)
                    return parts[0] + " *]" + prompt_char
                return bracket_content + "]" + prompt_char

        # Pattern 3: user@host:/path$ or user@host:~$ or user@host:~/path$
        # Generalize: user@host:*$ or user@host:*#
        if ":" in prompt and "@" in prompt:
            # Replace path after : with *
            parts = prompt.rsplit(":", 1)
            if len(parts) == 2:
                # Keep the prompt char at the end
                prompt_char_match = re.search(r"([>#\$%]\s*)$", parts[1])
                if prompt_char_match:
                    prompt_char = prompt_char_match.group(1)
                    generalized = parts[0] + ":*" + prompt_char
                    return generalized
                # If no prompt char found but there's content after colon, still generalize
                elif parts[1].strip():
                    # Assume last character is prompt char
                    content = parts[1].rstrip()
                    if content and content[-1] in ">#$%":
                        prompt_char = content[-1]
                        generalized = parts[0] + ":*" + prompt_char
                        return generalized

        # Pattern 4: user@host directory$ or user@host directory#
        # Generalize: user@host *$ or user@host *#
        if "@" in prompt and " " in prompt:
            match = re.search(r"(@[^\s]+\s+)(.+)([>#\$%]\s*)$", prompt)
            if match:
                prefix = match.group(1)
                prompt_char = match.group(3)
                # Extract user part
                user_part = prompt.split("@")[0]
                generalized = user_part + prefix + "*" + prompt_char
                return generalized

        # No generalization needed
        return prompt

    def _ensure_shell_type(self, session_key: str, client: paramiko.SSHClient) -> str:
        """Legacy method - now handled by _build_device_profile."""
        if session_key in self.registry.shell_types:
            return self.registry.shell_types[session_key]

        # Fallback for cases where profile wasn't built
        self.registry.shell_types[session_key] = "unknown"
        return "unknown"

    def _ensure_prompt_pattern(
        self,
        session_key: str,
        client: paramiko.SSHClient,
        initial_output: str | None = None,
        shell: paramiko.Channel | None = None,
    ) -> re.Pattern:
        """Detect and cache shell prompt pattern for reliable command completion detection.

        Args:
            session_key: Session identifier
            client: SSH client (used for exec_command fallback)
            initial_output: Initial shell output to analyze
            shell: Interactive shell (preferred for reading PS1)
        """
        if session_key in self.registry.prompt_patterns:
            return self.registry.prompt_patterns[session_key]

        logger = self.logger.getChild("detect_prompt")
        pattern: re.Pattern | None = None

        # Try to detect shell type
        shell_type = self.registry.shell_types.get(session_key, "unknown").lower()

        # For Fish shell, use a more specific pattern to avoid false positives
        if "fish" in shell_type:
            # Fish prompts typically have context before the prompt character
            pattern = re.compile(r"(\S+\s+)?[>#\$]\s*$")
            logger.debug("Using Fish shell prompt pattern")
        elif shell_type in (
            "cisco",
            "juniper",
            "fortinet",
            "arista",
            "paloalto",
            "checkpoint",
            "mikrotik",
            "edgeswitch",
            "vyos",
            "openwrt",
            "network_device",
        ):
            logger.debug(f"Skipping PS1 check for network device type: {shell_type}")
        else:
            # Try to read $PS1 from interactive shell (preferred) or exec_command (fallback)
            if shell:
                try:
                    # Use markers to extract PS1 from shell output
                    shell.send(b'echo "___PS1_START___$PS1___PS1_END___"\n')
                    time.sleep(0.5)

                    output = ""
                    start_time = time.time()
                    while time.time() - start_time < 3:
                        if shell.recv_ready():
                            chunk = shell.recv(4096).decode("utf-8", errors="ignore")
                            output += chunk
                            if "___PS1_END___" in output:
                                break
                        time.sleep(0.1)

                    # Extract PS1 between markers
                    match = re.search(
                        r"___PS1_START___(.+?)___PS1_END___", output, re.DOTALL
                    )
                    if match:
                        prompt = match.group(1).strip()
                        if prompt and prompt != "$PS1":
                            pattern = self._convert_ps1_to_pattern(prompt, logger)
                except Exception as exc:
                    logger.warning(
                        f"Failed to read PS1 from shell for {session_key}: {exc}"
                    )

            # Fallback to exec_command if shell method didn't work
            if pattern is None and client:
                try:
                    _stdin, stdout, _stderr = client.exec_command(
                        "echo $PS1", timeout=10
                    )
                    prompt = stdout.read().decode("utf-8").strip()
                    if prompt and prompt != "$PS1":
                        pattern = self._convert_ps1_to_pattern(prompt, logger)
                except Exception as exc:
                    logger.warning(f"Failed to read $PS1 for {session_key}: {exc}")

        # Fallback: extract from initial output
        if pattern is None and initial_output:
            fallback = self._extract_prompt_from_output(initial_output)
            if fallback:
                # Make extracted prompt flexible for directory changes
                if "[" in fallback and "]" in fallback:
                    # Support both [user@host dir]$ and [host]$ patterns
                    flexible_pattern = r"\[[^@\]]+(@[^\]]+)?\][$#]\s*$"
                    pattern = re.compile(flexible_pattern)
                else:
                    escaped = re.escape(fallback)
                    pattern = re.compile(rf"{escaped}\s*$")

        # Enhanced fallback: try common prompt patterns with scoring
        if pattern is None:
            common_patterns = [
                # Network device prompts (more specific first)
                r"\([^)]+\)\s*[>#]\s*$",  # (hostname)> or (hostname)#
                r"[^@\s]+[>#]\s*$",  # hostname> or hostname#
                r"\[[^@]+@[^\]]+\]\s*[>#$]\s*$",  # [user@host]>
                # Unix shell prompts
                r"\[[^@]+@[^\\s\]]+\s+[^\]]*\][$#]\s*$",  # [user@host dir]$ or [user@host dir]#
                r"[^@]+@[^:]+:[^$#]*[$#]\s*$",  # user@host:path$
                r"[^@]+@[^\s]+\s+[^$#]*[$#]\s*$",  # user@host path$
                # Generic prompts (least specific last)
                r"[>#\$%]\s*$",  # Generic prompt chars
            ]

            # Test patterns against initial output if available
            if initial_output:
                clean_output = self._strip_ansi(initial_output)

                # Score patterns by specificity (longer match = more specific)
                pattern_scores = []
                for i, p in enumerate(common_patterns):
                    test_pattern = re.compile(p)
                    match = test_pattern.search(clean_output)
                    if match:
                        # Score based on matched text length (more specific = higher score)
                        score = len(match.group(0))
                        pattern_scores.append((score, i, test_pattern, p))

                if pattern_scores:
                    # Use most specific (highest score) pattern
                    score, _best_idx, pattern, _pattern_str = max(pattern_scores)

            # Final fallback if no pattern matched
            if pattern is None:
                pattern = re.compile(r"[>#\$]\s*$")

        self.registry.prompt_patterns[session_key] = pattern
        self.registry.prompt_miss_count[session_key] = 0  # Reset miss count
        return pattern

    def _convert_ps1_to_pattern(self, prompt: str, logger) -> re.Pattern:
        """Convert PS1 prompt string to regex pattern."""
        # Convert PS1 variables to flexible regex patterns
        pattern_str = prompt
        pattern_str = pattern_str.replace("\\u", "[^@\\s]+")  # username
        pattern_str = pattern_str.replace("\\h", "[^\\s\\]]+")  # hostname
        pattern_str = pattern_str.replace("\\H", "[^\\s\\]]+")  # full hostname
        pattern_str = pattern_str.replace("\\W", "[^\\]\\s]*")  # working dir basename
        pattern_str = pattern_str.replace("\\w", "[^\\]\\s]*")  # full working dir
        pattern_str = pattern_str.replace("\\$", "[$#]")  # $ or #

        # Now escape special regex chars, but preserve our bracket patterns
        # First mark our patterns to protect them
        pattern_str = pattern_str.replace("[^@\\s]+", "___USERNAME___")
        pattern_str = pattern_str.replace("[^\\s\\]]+", "___HOSTNAME___")
        pattern_str = pattern_str.replace("[^\\]\\s]*", "___DIRNAME___")
        pattern_str = pattern_str.replace("[$#]", "___PROMPT___")

        # Escape everything else
        pattern_str = re.escape(pattern_str)

        # Restore our patterns
        pattern_str = pattern_str.replace("___USERNAME___", "[^@\\s]+")
        pattern_str = pattern_str.replace("___HOSTNAME___", "[^\\s\\]]+")
        pattern_str = pattern_str.replace("___DIRNAME___", "[^\\]\\s]*")
        pattern_str = pattern_str.replace("___PROMPT___", "[$#]")

        pattern = re.compile(rf"{pattern_str}\s*$")
        return pattern

    @staticmethod
    def _strip_ansi(text: str) -> str:
        """Strip all ANSI escape sequences including CSI, OSC, and other types."""
        # Remove CSI sequences: \x1b[...
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        # Remove OSC sequences: \x1b]...(\x07|\x1b\\)
        text = re.sub(r"\x1b\][^\x07]*\x07", "", text)
        text = re.sub(r"\x1b\][^\x1b]*\x1b\\", "", text)
        # Remove other escape sequences
        text = re.sub(r"\x1b[PX^_][^\x1b]*\x1b\\", "", text)
        # Remove terminal UI noise like <N> (fish iTerm integration)
        text = re.sub(r"<\d+>", "", text)
        # Remove special characters that appear in terminal output (␤, ⏎, etc.)
        text = re.sub(
            r"[\r\x00\u240c\u23ce]", "", text
        )  # CR, NUL, form feed symbol, return symbol
        # Remove any remaining single control characters
        text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
        return text

    @staticmethod
    def _extract_prompt_from_output(output: str) -> str | None:
        """Extract prompt from shell output by finding last line ending with prompt character.

        Uses comprehensive ANSI stripping to handle all escape sequence types.
        """
        lines = [line.rstrip() for line in output.splitlines() if line.strip()]
        for line in reversed(lines):
            # Use comprehensive ANSI stripping instead of basic CSI-only pattern
            stripped = SSHSessionManager._strip_ansi(line)
            if stripped and stripped[-1] in ("$", "#", ">", "%"):
                return stripped.strip()
        return None

    def _build_sentinel_command(self, marker: str, shell_path: str) -> str:
        lower = shell_path.lower()
        if "fish" in lower:
            return (
                " set -l __mcp_status $status; "
                f"printf '\\n{marker}%d\\n' $__mcp_status\n"
            )
        if lower.endswith("csh") or "tcsh" in lower:
            return f' set __mcp_status=$status; echo "{marker}$__mcp_status"\n'
        return (
            " __mcp_status=$?; "
            f'printf \'\\n{marker}%d\\n\' "$__mcp_status" 2>/dev/null || echo "{marker}$__mcp_status"\n'
        )

    def _build_command_with_sentinel(
        self, command: str, marker: str, shell_path: str = ""
    ) -> str:
        """Build command text with trailing sentinel in a heredoc-safe form."""
        sentinel_command = self._build_sentinel_command(marker, shell_path)
        return f"{command}\n{sentinel_command}"

    def _strip_sentinel(self, output: str, sentinel: str | None) -> str:
        """Strip the sentinel command and marker from output if present."""
        if not sentinel or sentinel not in output:
            return output

        # Strip the entire sentinel command block if it's visible
        # It typically looks like: __mcp_status=$?; printf '\nMARKER%d\n' ...
        # Or it might be partially visible.

        # First, try to find the start of the sentinel command
        # We look for the assignment to __mcp_status which is the start of our sentinel block
        sentinel_start = output.find("__mcp_status=$?")
        if sentinel_start != -1:
            return output[:sentinel_start].rstrip()

        # Fallback: if we only see the marker string itself
        marker_start = output.find(sentinel)
        if marker_start != -1:
            return output[:marker_start].rstrip()

        return output

    def _maybe_rewrite_mikrotik_command(self, session_key: str, command: str) -> str:
        """Append MikroTik 'without-paging' for print commands when safe."""
        if not self._mikrotik_auto_without_paging:
            return command

        if self.registry.shell_types.get(session_key) != "mikrotik":
            return command

        # Avoid rewriting multiline/script commands.
        if "\n" in command:
            return command

        if re.search(r"(^|\s)without-paging(\s|$)", command, re.IGNORECASE):
            return command

        if not re.search(r"(^|\s)print(\s|$)", command, re.IGNORECASE):
            return command

        command_starts_with_slash = command.lstrip().startswith("/")
        prompt = self.registry.prompts.get(session_key, "")
        menu_context = bool(re.search(r"\]\s+/[^>\s]*>\s*$", prompt))

        if not (command_starts_with_slash or menu_context):
            return command

        rewritten = f"{command.rstrip()} without-paging"
        self.logger.debug(
            f"Rewrote MikroTik print command to include without-paging: {rewritten}"
        )
        return rewritten

    def _execute_with_thread_timeout(
        self, func, timeout: int, *args, **kwargs
    ) -> tuple[str, str, int]:
        """Legacy wrapper retained for compatibility (no additional timeout logic)."""
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            logger = self.logger.getChild("thread_timeout")
            logger.logger.error(f"Error during execution: {exc}", exc_info=True)
            return "", f"Error: {exc}", 1

    def _execute_sudo_command(
        self,
        client: paramiko.SSHClient,
        command: str,
        sudo_password: str,
        timeout: int = 30,
    ) -> tuple[str, str, int]:
        """Compatibility wrapper around the sudo execution helper."""
        return self._execute_with_thread_timeout(
            self.command_executor._execute_sudo_command_internal,
            timeout,
            client,
            command,
            sudo_password,
            timeout,
        )

    def _check_prompt_completion(
        self, session_key: str, raw_output: str, clean_output: str
    ) -> tuple[bool, str]:
        """Check if output indicates command completion by detecting the prompt.

        Args:
            session_key: Session identifier
            raw_output: Raw output with ANSI codes
            clean_output: ANSI-stripped output

        Returns:
            Tuple of (is_complete, cleaned_output_without_prompt)
        """
        logger = self.logger.getChild("prompt_check")

        # Strategy 1: Check for captured literal/generalized prompt (most reliable)
        if session_key in self.registry.prompts:
            literal_prompt = self.registry.prompts[session_key]

            # Optimization: Only check the end of the output for prompt match
            # Most prompts are on the last line or within a few hundred chars.
            # 4096 is more than enough context even for complex multi-line prompts.
            if len(clean_output) > 4096:
                check_buffer = clean_output[-4096:].rstrip()
                buffer_offset = len(clean_output) - 4096
            else:
                check_buffer = clean_output.rstrip()
                buffer_offset = 0

            # Check if prompt contains wildcards or character classes (generalized)
            if "*" in literal_prompt or "[" in literal_prompt:
                # Convert to pattern for wildcard matching
                # Escape special regex chars except * and []
                pattern_str = re.escape(literal_prompt).replace(r"\*", ".*?")
                # Un-escape specific character classes we use (like [>#] from enable mode)
                # Do NOT unescape all brackets as that breaks literal brackets in prompts
                pattern_str = pattern_str.replace(r"\[>#\]", "[>#]").replace(
                    r"\[\$#\]", "[$#]"
                )
                # Ensure it matches at end of output
                pattern = re.compile(re.escape("").join([pattern_str, r"\s*$"]))

                # Debug: show what we're matching against
                last_100 = (
                    check_buffer[-100:] if len(check_buffer) > 100 else check_buffer
                )
                self._log_debug_rate_limited(
                    logger,
                    f"{session_key}_prompt_check",
                    f"Checking wildcard pattern '{literal_prompt}' (regex: '{pattern.pattern}') against last 100 chars: {last_100!r}",
                )

                match = pattern.search(check_buffer)
                if match:
                    # Remove the matched prompt from output
                    # We need to use the full clean_output for the final result
                    final_match_pos = buffer_offset + match.start()
                    output = clean_output[:final_match_pos].rstrip()
                    logger.debug(
                        f"Wildcard pattern matched! Matched text: {match.group()!r}"
                    )
                    return True, output
                else:
                    self._log_debug_rate_limited(
                        logger,
                        f"{session_key}_prompt_nomatch",
                        "Wildcard pattern did not match",
                    )
            else:
                # Exact literal match
                if check_buffer.endswith(literal_prompt):
                    # Remove the prompt from output
                    output = clean_output.rstrip()
                    if output.endswith(literal_prompt):
                        output = output[: -len(literal_prompt)].rstrip()
                    return True, output

        # Strategy 2: Fall back to pattern matching
        if session_key in self.registry.prompt_patterns:
            prompt_pattern = self.registry.prompt_patterns[session_key]

            # Only check the end of clean_output
            if len(clean_output) > 4096:
                check_buffer = clean_output[-4096:]
                buffer_offset = len(clean_output) - 4096
            else:
                check_buffer = clean_output
                buffer_offset = 0

            match = prompt_pattern.search(check_buffer)
            if match:
                final_match_pos = buffer_offset + match.start()
                output = clean_output[:final_match_pos].rstrip()
                return True, output

        return False, clean_output

    def _detect_awaiting_input(
        self, output: str, session_key: str = "global"
    ) -> str | None:
        """Detect if command is waiting for user input.

        Returns string describing what input is needed, or None if not awaiting input.
        """
        logger = self.logger.getChild("awaiting_input")

        # Mode-aware gating (only when interactive mode is enabled)
        if self._interactive_mode and session_key in self.registry.modes:
            mode = self.registry.modes.get(session_key, "unknown")

            # If in editor mode, don't flag as awaiting input
            # Editors handle their own input and shouldn't be interrupted
            if mode == "editor":
                self._log_debug_rate_limited(
                    logger,
                    f"{session_key}_editor_skip_awaiting",
                    "In editor mode, skipping awaiting_input detection",
                )
                return None

            # For pager mode, allow pager detection to proceed
            # For shell/password_prompt/unknown, use normal detection

        # Only process the last 4096 characters of output to avoid O(N^2) performance issues
        # with very large output buffers (like long-running command output).
        # Most prompts and input requests appear at the very end of the output.
        if len(output) > 4096:
            output_to_check = output[-4096:]
        else:
            output_to_check = output

        last_100 = (
            output_to_check[-100:] if len(output_to_check) > 100 else output_to_check
        )
        self._log_debug_rate_limited(
            logger,
            f"{session_key}_awaiting_input",
            f"Checking for awaiting input, last 100 chars: {last_100!r}",
        )

        clean_output = self._strip_ansi(output_to_check)
        lines = [line for line in clean_output.splitlines() if line.strip()]
        last_line = lines[-1].strip() if lines else ""

        # Common password prompts - match various formats like "password:", "password for user:", etc.
        # Note: We do NOT use re.MULTILINE so $ matches only the end of the string
        # We also exclude newlines, =, ", and ' from the wildcard to prevent matching
        # across lines, URL parameters, or JSON keys
        if re.search(r'password[^:=\n"\']*:?\s*$', last_line, re.IGNORECASE):
            logger.debug("Detected password prompt")
            return "password"
        if re.search(r'passphrase[^:=\n"\']*:?\s*$', last_line, re.IGNORECASE):
            logger.debug("Detected passphrase prompt")
            return "passphrase"

        # Pager prompts (less, more, MikroTik)
        # Match (END) with optional line numbers before it, or : on the last line
        # Strip ANSI codes from the end to properly detect pager prompts
        if re.search(r"(?:^|[\r\n]).*?\(END\)\s*$", clean_output):
            logger.debug("Detected pager (END) prompt")
            return "pager"
        if last_line == ":":
            # Common pager prompt when less/most waits for input
            logger.debug("Detected pager ':' prompt")
            return "pager"

        # MikroTik pager prompt
        if re.search(r"--\s*\[Q quit\|D dump\|.*?\]\s*$", output):
            return "pager"

        # SSH host key confirmation
        if re.search(
            r"Are you sure you want to continue connecting.*\(yes/no",
            output,
            re.IGNORECASE,
        ):
            return "ssh_host_key"

        # Yes/no prompts
        if re.search(
            r"\(y/n\)[:\s]*$|\(yes/no\)[:\s]*$|\[y/N\][:\s]*$|\[Y/n\][:\s]*$",
            last_line,
            re.IGNORECASE,
        ):
            return "yes_no"

        # Press any key / continue
        if re.search(
            r"(?:press any key|press enter|to continue)[:\.]*\s*$",
            last_line,
            re.IGNORECASE,
        ):
            return "press_key"

        # Generic prompt at end (anything ending with ? or prompt-like)
        if last_line.endswith("?") and len(last_line) <= 80 and "|" not in last_line:
            if not re.search(
                r"https?://|\bselect\b|\bfrom\b", last_line, re.IGNORECASE
            ):
                return "user_input"
        if re.search(r"\benter\b[^:]{0,80}:\s*$", last_line, re.IGNORECASE):
            return "user_input"

        return None

    def _is_context_changing_command(self, command: str) -> bool:
        """Detect if a command is likely to change the shell context/prompt.

        Commands that change the shell context include:
        - sudo -i, sudo -s, sudo su (root shell)
        - su, su - (switch user)
        - ssh (nested SSH)
        - docker exec -it, kubectl exec -it (container shells)
        - screen, tmux (terminal multiplexers)
        - bash, sh, zsh, fish (spawning new shell)

        Args:
            command: The command to check

        Returns:
            True if command likely changes shell context
        """
        # Extract base command (first word)
        cmd_lower = command.strip().lower()
        cmd_lower.split()[0] if cmd_lower else ""

        # Check for context-changing patterns
        context_changers = [
            r"^sudo\s+(-i|su|-s)",  # sudo -i, sudo su, sudo -s
            r"^su\b",  # su, su -, su user
            r"^ssh\b",  # ssh to another host
            r"^docker\s+exec.*-it",  # docker exec -it
            r"^kubectl\s+exec.*-it",  # kubectl exec -it
            r"^podman\s+exec.*-it",  # podman exec -it
            r"^screen\b",  # screen
            r"^tmux\b",  # tmux
            r"^(bash|sh|zsh|fish|ksh|csh|tcsh)\s*$",  # spawning new shell
            r"^/\.?\.\b",  # MikroTik menu up (/.. or /.)
            r"^/[a-z-]+(\s+[a-z-]+)*$",  # MikroTik menu change (/ip, /interface bridge, etc.)
        ]

        for pattern in context_changers:
            if re.search(pattern, cmd_lower):
                return True

        return False

    async def send_input_by_session(
        self,
        host: str,
        input_text: str,
        username: str | None = None,
        port: int | None = None,
    ) -> tuple[bool, str, str]:
        """Send input to the active shell for a session."""
        logger = self.logger.getChild("send_input_session")
        _, _, _, _, session_key = self._resolve_connection(host, username, port)
        logger.info(f"Sending input to session: {session_key}")

        with self.registry.lock:
            shell = self.registry.shells.get(session_key)

        if not shell:
            logger.error(f"No active shell for session: {session_key}")
            return False, "", "No active shell for this session"

        try:
            logger.debug(f"Sending text to shell: {input_text!r}")
            shell.send(input_text.encode("utf-8"))
            await asyncio.sleep(0.2)

            output = ""
            if getattr(shell, "recv_ready", lambda: False)():
                output = shell.recv(65535).decode("utf-8", errors="replace")
                logger.debug(f"Received {len(output)} bytes of new output.")

            return True, output, ""
        except Exception as exc:
            logger.logger.error(
                f"Failed to send input to session {session_key}: {exc}", exc_info=True
            )
            return False, "", f"Failed to send input: {exc}"

    async def read_file(
        self,
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
        timeout: int = 30,
        *,
        start_line: int = 1,
        offset: int = 0,
    ) -> FileContent:
        """Delegate remote file reads to the FileManager helper."""
        return await self.file_manager.read_file(
            host=host,
            remote_path=remote_path,
            username=username,
            password=password,
            key_filename=key_filename,
            port=port,
            encoding=encoding,
            errors=errors,
            max_bytes=max_bytes,
            sudo_password=sudo_password,
            use_sudo=use_sudo,
            timeout=timeout,
            start_line=start_line,
            offset=offset,
        )

    async def write_file(
        self,
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
        timeout: int = 30,
    ) -> tuple[str, str, int]:
        """Delegate remote file writes to the FileManager helper."""
        return await self.file_manager.write_file(
            host=host,
            remote_path=remote_path,
            content=content,
            username=username,
            password=password,
            key_filename=key_filename,
            port=port,
            encoding=encoding,
            errors=errors,
            append=append,
            make_dirs=make_dirs,
            permissions=permissions,
            max_bytes=max_bytes,
            sudo_password=sudo_password,
            use_sudo=use_sudo,
            timeout=timeout,
        )

    async def execute_result(self, *args, **kwargs):
        """Structured-result variant of :meth:`execute_command`."""
        return await self.command_executor.execute_result(*args, **kwargs)

    async def execute_command(
        self,
        host: str,
        username: str | None = None,
        command: str = "",
        password: str | None = None,
        key_filename: str | None = None,
        port: int | None = None,
        enable_password: str | None = None,
        enable_command: str = "enable",
        sudo_password: str | None = None,
        timeout: int = 30,
    ) -> tuple[str, str, int]:
        """Execute a command on a host using persistent session."""
        return await self.command_executor.execute_command(
            host,
            username,
            command,
            password,
            key_filename,
            port,
            enable_password,
            enable_command,
            sudo_password,
            timeout,
        )

    async def execute_command_enhanced(
        self,
        host: str,
        username: str | None = None,
        command: str = "",
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
        """Execute command with enhanced features."""
        return await self.enhanced_executor.execute_command_enhanced(
            host,
            username,
            command,
            password,
            key_filename,
            port,
            enable_password,
            enable_command,
            sudo_password,
            timeout,
            auto_extend_timeout,
            max_timeout,
            streaming_mode,
            progress_callback,
        )

    def get_session_diagnostics(
        self, host: str, username: str | None = None, port: int | None = None
    ):
        """Get session diagnostics."""
        return self.session_diagnostics.get_session_diagnostics(host, username, port)

    def reset_session_prompt(
        self, host: str, username: str | None = None, port: int | None = None
    ) -> bool:
        """Reset session prompt detection."""
        return self.session_diagnostics.reset_session_prompt_detection(
            host, username, port
        )

    def get_connection_health_report(self):
        """Get connection health report."""
        return self.session_diagnostics.get_connection_health_report()

    def get_performance_metrics(self):
        """Get performance metrics from logging."""
        return self.logger.get_performance_report()

    async def execute_command_async(
        self,
        host: str,
        username: str | None = None,
        command: str = "",
        password: str | None = None,
        key_filename: str | None = None,
        port: int | None = None,
        sudo_password: str | None = None,
        enable_password: str | None = None,
        enable_command: str = "enable",
        timeout: int = 300,
    ) -> str:
        """Execute a command asynchronously without blocking."""
        return await self.command_executor.execute_command_async(
            host,
            username,
            command,
            password,
            key_filename,
            port,
            sudo_password,
            enable_password,
            enable_command,
            timeout,
        )

    def get_command_status(self, command_id: str) -> dict:
        """Get the status and output of an async command."""
        return self.command_executor.get_command_status(command_id)

    def interrupt_command_by_id(self, command_id: str) -> tuple[bool, str]:
        """Interrupt a running async command by its ID."""
        return self.command_executor.interrupt_command_by_id(command_id)

    async def send_input(
        self, command_id: str, input_text: str
    ) -> tuple[bool, str, str]:
        """Send input to a running command and return any new output."""
        return await self.command_executor.send_input(command_id, input_text)

    def list_running_commands(self) -> list[dict]:
        """List all running async commands."""
        return self.command_executor.list_running_commands()

    def list_command_history(self, limit: int = 50) -> list[dict]:
        """List recent command history (completed, failed, interrupted)."""
        return self.command_executor.list_command_history(limit)

    def _cleanup_old_commands(self):
        """Remove old completed commands, keeping only recent ones."""
        logger = self.logger.getChild("cleanup")
        executor = self.command_executor
        with executor._lock:
            completed = [
                (cmd_id, cmd)
                for cmd_id, cmd in executor._commands.items()
                if cmd.status
                in (
                    CommandStatus.COMPLETED,
                    CommandStatus.FAILED,
                    CommandStatus.INTERRUPTED,
                )
            ]
            if len(completed) > self._max_completed_commands:
                logger.info(
                    f"Found {len(completed)} completed commands, exceeding limit of {self._max_completed_commands}. Cleaning up."
                )
                completed.sort(key=lambda x: x[1].end_time or datetime.min)
                to_remove = completed[: -self._max_completed_commands]
                for cmd_id, _ in to_remove:
                    del executor._commands[cmd_id]
            else:
                logger.debug(
                    f"Cleanup check: {len(completed)} completed commands within limit of {self._max_completed_commands}."
                )
