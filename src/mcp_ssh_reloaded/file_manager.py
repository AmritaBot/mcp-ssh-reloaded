"""File management for SSH sessions."""

from __future__ import annotations

import base64
import codecs
import logging
import os
import posixpath
import shlex
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import paramiko

from .models import FileContent

if TYPE_CHECKING:
    from mcp_ssh_reloaded.session_manager import SSHSessionManager


class FileManager:
    """Manages file operations on SSH sessions."""

    def __init__(self, session_manager: SSHSessionManager):
        self._session_manager = session_manager
        self.logger = logging.getLogger("ssh_session.file_manager")

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
        start_line: int = 1,
        max_lines: int | None = None,
        sudo_password: str | None = None,
        use_sudo: bool = False,
        timeout: int = 30,
    ) -> FileContent:
        """Read a remote file over SSH using SFTP, with optional sudo fallback.

        The result is always cut on a line boundary, so a truncated read never
        splits a UTF-8 character; pass the reported ``next_start_line`` back as
        ``start_line`` to fetch the next chunk.
        """
        logger = self.logger.getChild("read_file")
        logger.info(
            f"Reading remote file on {host}: {remote_path} "
            f"(start_line={start_line}, max_lines={max_lines})"
        )

        if not remote_path:
            logger.error("Remote path must be provided.")
            return FileContent(error="Remote path must be provided")

        start_line = max(1, start_line)
        if max_lines is not None and max_lines < 1:
            max_lines = None

        if not sudo_password and use_sudo:
            sudo_password = os.getenv(f"OVRD_{host}_SUDO_PASS")

        _, _, _, _, session_key = self._session_manager._resolve_connection(
            host, username, port
        )
        client = await self._session_manager.get_or_create_session(
            host, username, password, key_filename, port
        )

        byte_limit = self._session_manager.MAX_FILE_TRANSFER_SIZE
        if max_bytes is not None:
            byte_limit = min(max_bytes, self._session_manager.MAX_FILE_TRANSFER_SIZE)
        logger.debug(f"Byte limit set to {byte_limit}")

        used_encoding = encoding or "utf-8"
        used_errors = errors or "replace"

        sftp = None
        permission_denied = False
        try:
            logger.debug("Attempting to read file via SFTP.")
            sftp = client.open_sftp()
            resolved_path = self._resolve_sftp_path(sftp, remote_path)
            attrs = sftp.stat(resolved_path)
            if attrs.st_mode is None:
                raise paramiko.SSHException(
                    f"Failed to stat remote file {resolved_path}: {attrs}"
                )
            if stat.S_ISDIR(attrs.st_mode):
                logger.error(f"Remote path is a directory: {resolved_path}")
                return FileContent(
                    path=resolved_path,
                    error=f"Remote path is a directory: {resolved_path}",
                )

            with sftp.file(resolved_path, "rb") as remote_file:
                window = self._read_window(
                    remote_file,
                    start_line=start_line,
                    max_lines=max_lines,
                    byte_limit=byte_limit,
                    encoding=used_encoding,
                    errors=used_errors,
                )

            logger.info(f"Successfully read file {resolved_path} via SFTP.")
            return FileContent(
                content=window.text,
                path=resolved_path,
                truncated=window.truncated,
                max_bytes=byte_limit,
                start_line=window.start_line,
                end_line=window.end_line,
                total_lines=window.total_lines,
                next_start_line=window.next_start_line,
                bytes_read=window.bytes_read,
            )
        except FileNotFoundError:
            logger.error(f"Remote file not found: {remote_path}")
            return FileContent(
                path=remote_path, error=f"Remote file not found: {remote_path}"
            )
        except PermissionError:
            logger.warning(f"SFTP permission denied for {remote_path}.")
            permission_denied = True
        except Exception as e:
            if "permission denied" in str(e).lower():
                logger.warning(f"SFTP permission denied for {remote_path}.")
                permission_denied = True
            else:
                logger.error(
                    f"Error reading file {remote_path} on {session_key}: {e!s}",
                    exc_info=True,
                )
                return FileContent(
                    path=remote_path, error=f"Error reading remote file: {e!s}"
                )
        finally:
            if sftp:
                try:
                    sftp.close()
                except Exception:
                    pass

        if permission_denied and (use_sudo or sudo_password):
            logger.info(
                f"SFTP permission denied, falling back to sudo cat for {remote_path}"
            )
            return await self._read_via_sudo(
                host=host,
                remote_path=remote_path,
                username=username,
                password=password,
                key_filename=key_filename,
                port=port,
                encoding=used_encoding,
                errors=used_errors,
                byte_limit=byte_limit,
                start_line=start_line,
                max_lines=max_lines,
                sudo_password=sudo_password,
                timeout=timeout,
            )
        if permission_denied:
            logger.error(
                f"Permission denied reading {remote_path} and no sudo fallback specified."
            )
            return FileContent(
                path=remote_path,
                error=(
                    "Permission denied reading file. Set use_sudo=True or provide "
                    "sudo_password to retry with sudo."
                ),
            )

        logger.error("Unexpected error in read_file logic.")
        return FileContent(path=remote_path, error="Unexpected error in read_file")

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
        """Write content to a remote file over SSH using SFTP, with optional sudo fallback."""
        logger = self.logger.getChild("write_file")
        logger.info(f"Writing remote file on {host}: {remote_path} (append={append})")

        if not remote_path:
            logger.error("Remote path must be provided.")
            return "", "Remote path must be provided", 1

        # Apply environment variable override for sudo password
        if not sudo_password and use_sudo:
            sudo_password = os.getenv(f"OVRD_{host}_SUDO_PASS")

        used_encoding = encoding or "utf-8"
        used_errors = errors or "strict"

        try:
            data = content.encode(used_encoding, used_errors)
        except Exception as e:
            logger.error(
                f"Failed to encode content using encoding '{used_encoding}': {e}"
            )
            return (
                "",
                f"Failed to encode content using encoding '{used_encoding}': {e!s}",
                1,
            )

        byte_limit = self._session_manager.MAX_FILE_TRANSFER_SIZE
        if max_bytes is not None:
            byte_limit = min(max_bytes, self._session_manager.MAX_FILE_TRANSFER_SIZE)
        logger.debug(f"Byte limit set to {byte_limit}")

        if len(data) > byte_limit:
            logger.error(f"Content size {len(data)} exceeds limit {byte_limit}.")
            return (
                "",
                (
                    f"Content size {len(data)} bytes exceeds maximum allowed {byte_limit} bytes. "
                    "Split the write into smaller chunks."
                ),
                1,
            )

        _, _, _, _, session_key = self._session_manager._resolve_connection(
            host, username, port
        )
        client = await self._session_manager.get_or_create_session(
            host, username, password, key_filename, port
        )

        # Try SFTP first if not explicitly using sudo
        if not use_sudo and not sudo_password:
            sftp = None
            try:
                logger.debug("Attempting to write file via SFTP.")
                sftp = client.open_sftp()
                resolved_path = self._resolve_sftp_path(sftp, remote_path)

                if make_dirs:
                    directory = posixpath.dirname(resolved_path)
                    self._ensure_remote_dirs(sftp, directory)

                mode = "ab" if append else "wb"
                logger.debug(f"Opening remote file in mode '{mode}'.")
                with sftp.file(resolved_path, mode) as remote_file:
                    remote_file.write(data)
                    remote_file.flush()

                if permissions is not None:
                    logger.debug(f"Setting permissions to {oct(permissions)}.")
                    sftp.chmod(resolved_path, permissions)

                message = f"Wrote {len(data)} bytes to {resolved_path}"
                if append:
                    message += " (append)"
                logger.info(f"Successfully wrote file via SFTP: {message}")
                return message, "", 0
            except FileNotFoundError:
                logger.error(f"Remote path not found: {remote_path}")
                return "", f"Remote path not found: {remote_path}", 1
            except PermissionError:
                logger.warning(
                    f"SFTP permission denied for {remote_path}. Will try sudo if configured."
                )
                return (
                    "",
                    "Permission denied writing file. Set use_sudo=True or provide sudo_password to retry with sudo.",
                    1,
                )
            except Exception as e:
                if "permission denied" in str(e).lower():
                    logger.warning(
                        f"SFTP permission denied for {remote_path}. Will try sudo if configured."
                    )
                    return (
                        "",
                        "Permission denied writing file. Set use_sudo=True or provide sudo_password to retry with sudo.",
                        1,
                    )
                logger.error(
                    f"Error writing file {remote_path} on {session_key}: {e!s}",
                    exc_info=True,
                )
                return "", f"Error writing remote file: {e!s}", 1
            finally:
                if sftp:
                    try:
                        sftp.close()
                    except Exception:
                        pass

        # Use sudo shell commands
        logger.info(f"Using sudo to write {remote_path}")

        # Helper to execute with or without password
        async def exec_sudo(cmd: str) -> tuple[str, str, int]:
            return await self._session_manager.execute_command(
                host=host,
                username=username,
                password=password,
                key_filename=key_filename,
                port=port,
                command=cmd,
                sudo_password=sudo_password,
                timeout=timeout,
            )

        # Create parent directories if needed
        if make_dirs:
            directory = posixpath.dirname(remote_path)
            if directory and directory != "/":
                mkdir_cmd = f"sudo mkdir -p {shlex.quote(directory)}"
                logger.debug(f"Executing mkdir command: {mkdir_cmd}")
                _, stderr, exit_code = await exec_sudo(mkdir_cmd)
                if exit_code != 0:
                    logger.error(f"Failed to create directories with sudo: {stderr}")
                    return "", f"Failed to create directories: {stderr}", exit_code

        # Write content using tee (supports both write and append)
        # Use base64 encoding to avoid shell escaping issues with special characters
        try:
            encoded_content = base64.b64encode(
                content.encode(used_encoding, used_errors)
            ).decode("ascii")

            if append:
                cmd = f'echo "{encoded_content}" | base64 -d | sudo tee -a {shlex.quote(remote_path)} > /dev/null'
            else:
                cmd = f'echo "{encoded_content}" | base64 -d | sudo tee {shlex.quote(remote_path)} > /dev/null'
            logger.debug(f"Executing write command (base64 encoded): {cmd[:100]}...")
        except Exception as e:
            logger.error(f"Failed to encode content for safe writing: {e}")
            return "", f"Failed to encode content for safe writing: {e}", 1

        _stdout, stderr, exit_code = await exec_sudo(cmd)

        if exit_code != 0:
            logger.error(f"Failed to write file with sudo: {stderr}")
            return "", f"Failed to write file with sudo: {stderr}", exit_code

        # Set permissions if specified
        if permissions is not None:
            chmod_cmd = f"sudo chmod {oct(permissions)[2:]} {shlex.quote(remote_path)}"
            logger.debug(f"Executing chmod command: {chmod_cmd}")
            _, stderr, exit_code = await exec_sudo(chmod_cmd)
            if exit_code != 0:
                logger.warning(f"Failed to set permissions: {stderr}")

        message = f"Wrote {len(data)} bytes to {remote_path} using sudo"
        if append:
            message += " (append)"
        if not sudo_password:
            message += " (passwordless)"
        logger.info(f"Successfully wrote file via sudo: {message}")
        return message, "", 0

    def _read_window(
        self,
        remote_file: paramiko.SFTPFile,
        *,
        start_line: int,
        max_lines: int | None,
        byte_limit: int,
        encoding: str,
        errors: str,
    ) -> _LineWindow:
        """Stream lines into a byte-bounded, character-aligned window."""
        kept: list[str] = []
        kept_bytes = 0
        line_no = 0
        truncated = False

        for raw in self._iter_lines(remote_file):
            line_no += 1
            if line_no < start_line:
                continue
            if max_lines is not None and len(kept) >= max_lines:
                truncated = True
                break
            if kept_bytes + len(raw) > byte_limit:
                room = byte_limit - kept_bytes
                if room > 0:
                    piece = _trim_partial_char(raw[:room], encoding, errors)
                    kept.append(piece.decode(encoding, errors))
                    kept_bytes += len(piece)
                truncated = True
                break
            kept.append(raw.decode(encoding, errors))
            kept_bytes += len(raw)

        end_line = start_line + len(kept) - 1 if kept else max(0, start_line - 1)
        return _LineWindow(
            text="".join(kept),
            truncated=truncated,
            start_line=start_line,
            end_line=end_line,
            total_lines=None if truncated else line_no,
            next_start_line=end_line + 1 if truncated else None,
            bytes_read=kept_bytes,
        )

    def _iter_lines(
        self, remote_file: paramiko.SFTPFile, chunk_size: int = 65536
    ) -> Iterator[bytes]:
        """Yield raw lines (newline kept) without buffering the whole file."""
        buffer = b""
        cap = self._session_manager.MAX_FILE_TRANSFER_SIZE
        while True:
            chunk = remote_file.read(chunk_size)
            if not chunk:
                break
            buffer += chunk
            while True:
                idx = buffer.find(b"\n")
                if idx < 0:
                    break
                yield buffer[: idx + 1]
                buffer = buffer[idx + 1 :]
            # A binary file with no newlines must not grow the buffer forever.
            if len(buffer) > cap:
                yield buffer
                buffer = b""
        if buffer:
            yield buffer

    async def _read_via_sudo(
        self,
        *,
        host: str,
        remote_path: str,
        username: str | None,
        password: str | None,
        key_filename: str | None,
        port: int | None,
        encoding: str,
        errors: str,
        byte_limit: int,
        start_line: int,
        max_lines: int | None,
        sudo_password: str | None,
        timeout: int,
    ) -> FileContent:
        """Read through ``sudo`` when SFTP is denied.

        The payload is base64-wrapped so the byte cap cannot split a character
        on the wire, and ``sed`` selects the requested line window.
        """
        logger = self.logger.getChild("sudo_read")
        last_line = start_line + max_lines - 1 if max_lines is not None else "$"
        cmd = (
            f"sudo sed -n '{start_line},{last_line}p' {shlex.quote(remote_path)} "
            f"| head -c {byte_limit} | base64"
        )
        logger.debug(f"Sudo fallback command: {cmd}")

        stdout, stderr, exit_code = await self._session_manager.execute_command(
            host=host,
            username=username,
            password=password,
            key_filename=key_filename,
            port=port,
            command=cmd,
            sudo_password=sudo_password,
            timeout=timeout,
        )
        if exit_code != 0:
            logger.error(f"Sudo fallback failed for {remote_path}: {stderr}")
            return FileContent(
                path=remote_path, error=f"Permission denied and sudo failed: {stderr}"
            )

        try:
            raw = base64.b64decode(stdout, validate=False)
        except Exception as e:
            logger.error(f"Failed to decode sudo output for {remote_path}: {e}")
            return FileContent(
                path=remote_path, error=f"Failed to decode sudo output: {e!s}"
            )

        truncated = len(raw) >= byte_limit
        if truncated:
            raw = _trim_partial_char(raw[:byte_limit], encoding, errors)
        content = raw.decode(encoding, errors)
        num_lines = content.count("\n") + (
            1 if content and not content.endswith("\n") else 0
        )
        end_line = start_line + num_lines - 1 if num_lines else max(0, start_line - 1)

        logger.info(f"Successfully read file {remote_path} via sudo fallback.")
        return FileContent(
            content=content,
            path=remote_path,
            truncated=truncated,
            max_bytes=byte_limit,
            start_line=start_line,
            end_line=end_line,
            total_lines=None,
            next_start_line=end_line + 1 if truncated else None,
            bytes_read=len(raw),
        )

    def _resolve_sftp_path(self, sftp: paramiko.SFTPClient, remote_path: str) -> str:
        """Resolve SFTP path, including '~' expansion."""
        if remote_path == "~":
            return sftp.normalize(".")
        if remote_path.startswith("~/"):
            home = sftp.normalize(".")
            return posixpath.join(home, remote_path[2:])
        return remote_path

    def _ensure_remote_dirs(self, sftp: paramiko.SFTPClient, remote_dir: str):
        """Ensure remote directory structure exists when writing files."""
        logger = self.logger.getChild("ensure_dirs")
        if not remote_dir or remote_dir in (".", "/"):
            return

        logger.debug(f"Ensuring remote directory exists: {remote_dir}")
        directories = []
        current = remote_dir

        while current and current not in (".", "/"):
            directories.append(current)
            next_dir = posixpath.dirname(current)
            if next_dir == current:
                break
            current = next_dir

        for directory in reversed(directories):
            try:
                attrs = sftp.stat(directory)
                if attrs.st_mode is not None and not stat.S_ISDIR(attrs.st_mode):
                    logger.error(
                        f"Remote path exists and is not a directory: {directory}"
                    )
                    raise OSError(
                        f"Remote path exists and is not a directory: {directory}"
                    )
            except FileNotFoundError:  # noqa: PERF203
                logger.info(f"Creating remote directory: {directory}")
                sftp.mkdir(directory)


@dataclass
class _LineWindow:
    """Internal carrier for one bounded, line-aligned slice of a file."""

    text: str
    truncated: bool
    start_line: int
    end_line: int
    total_lines: int | None
    next_start_line: int | None
    bytes_read: int


def _trim_partial_char(data: bytes, encoding: str, errors: str) -> bytes:
    """Drop a trailing partial multi-byte sequence so *data* decodes cleanly."""
    try:
        decoder = codecs.getincrementaldecoder(encoding)(errors)
        decoder.decode(data, final=False)
        pending = decoder.getstate()[0]
    except (UnicodeDecodeError, LookupError, AttributeError):
        return data
    if pending:
        return data[: len(data) - len(pending)]
    return data
