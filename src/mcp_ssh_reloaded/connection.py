"""SSH connection lifecycle management.

Extracted from session_manager.py - handles SSH config, connection
resolution with env overrides, session create/close/list.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import paramiko

if TYPE_CHECKING:
    from .session_manager import SSHSessionManager

try:
    NoValidConnectionsError = paramiko.NoValidConnectionsError  # pyright: ignore[reportAttributeAccessIssue]
except AttributeError:
    from paramiko.ssh_exception import (
        NoValidConnectionsError,
    )


class ConnectionManager:
    """Manages SSH connections: resolution, create, close, list."""

    def __init__(self, sm: SSHSessionManager):
        self._sm = sm  # parent SessionManager for shared state access

    # -- state accessors --

    @property
    def registry(self):
        """The session registry that owns all per-session state."""
        return self._sm.registry

    @property
    def logger(self):
        return self._sm.logger

    @property
    def _ssh_config(self):
        return self._sm._ssh_config

    @property
    def command_executor(self):
        return self._sm.command_executor

    # -- SSH config --

    @staticmethod
    def load_ssh_config() -> paramiko.SSHConfig:
        ssh_config = paramiko.SSHConfig()
        config_path = Path.home() / ".ssh" / "config"
        if config_path.exists():
            with open(config_path) as f:
                ssh_config.parse(f)
        return ssh_config

    # -- connection resolution --

    def resolve_connection(
        self, host: str, username: str | None, port: int | None
    ) -> tuple[dict[str, Any], str, str, int, str]:
        host_config = self._ssh_config.lookup(host)
        resolved_host = host_config.get("hostname", host)
        resolved_username = username or host_config.get(
            "user", os.getenv("USER", "root")
        )
        resolved_port = port or int(host_config.get("port", 22))

        env_prefix = f"OVRD_{host}_"
        if override_host := os.getenv(f"{env_prefix}HOST"):
            resolved_host = override_host
        if override_user := os.getenv(f"{env_prefix}USER"):
            resolved_username = override_user
        if port_str := os.getenv(f"{env_prefix}PORT"):
            try:
                resolved_port = int(port_str)
            except ValueError:
                self.logger.warning(f"Invalid port in {env_prefix}PORT: {port_str}")

        session_key = f"{resolved_username}@{resolved_host}:{resolved_port}"
        return host_config, resolved_host, resolved_username, resolved_port, session_key

    @staticmethod
    def get_env_override(
        host: str, param: str, default: str | None = None
    ) -> str | None:
        return os.getenv(f"OVRD_{host}_{param}", default)

    # -- session create/close/list --

    async def get_or_create_session(
        self,
        host: str,
        username: str | None = None,
        password: str | None = None,
        key_filename: str | None = None,
        port: int | None = None,
    ) -> paramiko.SSHClient:
        logger = self.logger.getChild("get_session")
        host_config, resolved_host, resolved_username, resolved_port, session_key = (
            self.resolve_connection(host, username, port)
        )
        resolved_key = key_filename or host_config.get("identityfile", [None])[0]

        if env_key := self.get_env_override(host, "KEY"):
            resolved_key = env_key
        if env_pass := self.get_env_override(host, "PASS"):
            password = env_pass

        with self.registry.lock:
            if session_key in self.registry.sessions:
                client = self.registry.sessions[session_key]
                try:
                    transport = client.get_transport()
                    if transport and transport.is_active():
                        logger.debug(f"Reusing active session: {session_key}")
                        self._sm._ensure_shell_type(session_key, client)
                        return client
                    else:
                        logger.warning(
                            f"Found dead session, will recreate: {session_key}"
                        )
                except Exception as e:
                    logger.warning(
                        f"Error checking session, will recreate: {session_key} - {e}"
                    )
                self._close_session(session_key)

            logger.info(f"Creating new session: {session_key}")
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs: dict[str, Any] = {
                "hostname": resolved_host,
                "port": resolved_port,
                "username": resolved_username,
                "timeout": 30,
                "banner_timeout": 30,
                "auth_timeout": 30,
            }
            if password:
                connect_kwargs["password"] = password
            elif resolved_key:
                connect_kwargs["key_filename"] = os.path.expanduser(resolved_key)  # noqa: ASYNC240

            try:
                await asyncio.to_thread(client.connect, **connect_kwargs)
                self.registry.sessions[session_key] = client
                logger.info(f"Successfully created new session: {session_key}")
                return client
            except (
                paramiko.AuthenticationException,
                paramiko.SSHException,
                NoValidConnectionsError,
                OSError,
                TimeoutError,
            ) as e:
                logger.error(
                    f"Connection failed to {session_key}: {type(e).__name__}: {e}"
                )
                try:
                    client.close()
                except Exception:
                    pass
                raise ConnectionError(
                    f"Unable to connect to {resolved_host}:{resolved_port} - {e}"
                )
            except Exception as e:
                logger.exception(f"Unexpected error connecting to {session_key}: {e}")
                try:
                    client.close()
                except Exception:
                    pass
                raise ConnectionError(f"Connection failed: {e}")

    async def close_session(
        self, host: str, username: str | None = None, port: int | None = None
    ):
        _, _, _, _, session_key = self.resolve_connection(host, username, port)
        self.logger.info(f"Request to close session: {session_key}")
        async with self.registry.lock:
            await asyncio.to_thread(self._close_session, session_key)

    def _close_session(self, session_key: str):
        logger = self.logger.getChild("internal_close")
        logger.debug(f"Closing session resources for {session_key}")
        logger.debug(f"Clearing commands for {session_key}")
        self.command_executor.clear_session_commands(session_key)

        if session_key in self.registry.shells:
            try:
                self.registry.shells[session_key].close()
            except Exception as e:
                logger.warning(f"Error closing shell for {session_key}: {e}")
            del self.registry.shells[session_key]

        if session_key in self.registry.sessions:
            try:
                self.registry.sessions[session_key].close()
            except Exception as e:
                logger.warning(f"Error closing client for {session_key}: {e}")
            del self.registry.sessions[session_key]

        self.registry.forget(session_key)

        logger.info(f"Session closed: {session_key}")

    async def close_all_sessions(self):
        logger = self.logger.getChild("close_all")
        logger.info("Closing all active sessions and resources.")
        async with self.registry.lock:
            logger.debug("Clearing all commands")
            self.command_executor.clear_all_commands()

            for key, shell in list(self.registry.shells.items()):
                try:
                    shell.close()
                except Exception as e:  # noqa: PERF203
                    logger.warning(f"Error closing shell for {key}: {e}")
            self.registry.shells.clear()

            for key, client in list(self.registry.sessions.items()):
                try:
                    client.close()
                except Exception as e:  # noqa: PERF203
                    logger.warning(f"Error closing client for {key}: {e}")
            self.registry.sessions.clear()
            self.registry.enable_mode.clear()
            self.registry.shell_types.clear()
            self.registry.prompt_patterns.clear()
            self.registry.prompts.clear()
            self.registry.clear()
        logger.info("All sessions closed.")

    async def list_sessions(self) -> list[str]:
        async with self.registry.lock:
            return list(self.registry.sessions.keys())

    def close_all_sessions_sync(self):
        """Synchronous fallback for __del__ (cannot await in destructor)."""
        with self.registry.lock:
            self.command_executor.clear_all_commands()
            for key, shell in list(self.registry.shells.items()):
                try:
                    shell.close()
                except Exception:  # noqa: PERF203
                    pass
            self.registry.shells.clear()
            for key, client in list(self.registry.sessions.items()):
                try:
                    client.close()
                except Exception:  # noqa: PERF203
                    pass
            self.registry.sessions.clear()
            self.registry.enable_mode.clear()
            self.registry.shell_types.clear()
            self.registry.prompt_patterns.clear()
            self.registry.prompts.clear()
            self.registry.clear()
