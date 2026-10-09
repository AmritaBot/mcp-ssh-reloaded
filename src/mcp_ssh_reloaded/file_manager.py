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
        sudo_password: str | None = None,
        use_sudo: bool = False,
        timeout: int = 30,
        *,
        start_line: int = 1,
        offset: int = 0,
    ) -> FileContent:
        """Read a remote file over SSH using SFTP, with optional sudo fallback.

        A read returns at most ``max_bytes`` bytes.  ``offset`` starts it at that
        byte position; ``start_line`` starts it at that 1-based line and makes
        every window end on a line boundary.  A truncated result carries
        ``next_offset`` (always) and ``next_start_line`` (line windows) so the
        read can be resumed without skipping or repeating content.
        """
        logger = self.logger.getChild("read_file")
        logger.info(
            f"Reading remote file on {host}: {remote_path} "
            f"(start_line={start_line}, offset={offset})"
        )

        if not remote_path:
            logger.error("Remote path must be provided.")
            return FileContent(error="Remote path must be provided")
        if max_bytes is not None and max_bytes <= 0:
            return FileContent(
                path=remote_path, error="max_bytes must be a positive integer"
            )
        if offset < 0:
            return FileContent(path=remote_path, error="offset must not be negative")
        if start_line < 1:
            return FileContent(path=remote_path, error="start_line must be >= 1")
        if offset > 0 and start_line > 1:
            return FileContent(
                path=remote_path, error="Pass either offset or start_line, not both"
            )

        used_encoding = encoding or "utf-8"
        used_errors = errors or "replace"
        # Line windows only make sense when a newline is a single 0x0A byte.
        line_mode = offset == 0 and _newline_is_single_byte(used_encoding)
        if start_line > 1 and not line_mode:
            return FileContent(
                path=remote_path,
                error=(
                    "Line-based reads require a newline-compatible encoding; "
                    "use offset/max_bytes for this encoding"
                ),
            )

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
                if line_mode:
                    window = self._read_line_window(
                        remote_file,
                        start_line=start_line,
                        byte_limit=byte_limit,
                        encoding=used_encoding,
                        errors=used_errors,
                    )
                else:
                    window = self._read_byte_window(
                        remote_file,
                        offset=offset,
                        byte_limit=byte_limit,
                        encoding=used_encoding,
                        errors=used_errors,
                    )

            logger.info(f"Successfully read file {resolved_path} via SFTP.")
            return _to_file_content(window, resolved_path, byte_limit)
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
                offset=offset,
                line_mode=line_mode,
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

    def _read_line_window(
        self,
        remote_file: paramiko.SFTPFile,
        *,
        start_line: int,
        byte_limit: int,
        encoding: str,
        errors: str,
    ) -> _Window:
        """Read whole lines from ``start_line`` until the byte budget runs out."""
        kept: list[bytes] = []
        kept_bytes = 0
        consumed = 0
        last_line = start_line - 1
        seen_line = 0
        truncated = False
        partial = False

        for line_no, raw, complete in self._iter_lines(
            remote_file, max_line_bytes=byte_limit
        ):
            seen_line = line_no
            if line_no < start_line:
                consumed += len(raw)
                continue
            if kept_bytes + len(raw) > byte_limit:
                # A whole line never fits in the remaining budget: stop before it.
                truncated = True
                break
            kept.append(raw)
            kept_bytes += len(raw)
            consumed += len(raw)
            last_line = line_no
            if not complete:
                # Fragment of a physical line longer than the byte cap.
                partial = True
                truncated = True
                break

        text = _trim_partial_char(b"".join(kept), encoding, errors).decode(
            encoding, errors
        )
        if kept:
            end_line = last_line
        else:
            end_line = max(0, min(start_line - 1, seen_line))
        return _Window(
            text=text,
            truncated=truncated,
            bytes_read=kept_bytes,
            offset=0,
            next_offset=consumed if truncated else None,
            start_line=start_line,
            end_line=end_line,
            total_lines=None if truncated else seen_line,
            # A partial line cannot be resumed by line number without looping.
            next_start_line=last_line + 1 if truncated and not partial else None,
        )

    def _read_byte_window(
        self,
        remote_file: paramiko.SFTPFile,
        *,
        offset: int,
        byte_limit: int,
        encoding: str,
        errors: str,
    ) -> _Window:
        """Read a byte window, trimmed back to a character boundary."""
        if offset:
            remote_file.seek(offset)
        data = remote_file.read(byte_limit + 1)
        truncated = len(data) > byte_limit
        if truncated:
            data = data[:byte_limit]
        data = _trim_partial_char(data, encoding, errors)
        text = data.decode(encoding, errors)
        line_count = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        known = offset == 0
        return _Window(
            text=text,
            truncated=truncated,
            bytes_read=len(data),
            offset=offset,
            next_offset=offset + len(data) if truncated else None,
            start_line=1 if known else None,
            end_line=line_count if known else None,
            total_lines=line_count if known and not truncated else None,
            next_start_line=None,
        )

    def _iter_lines(
        self,
        remote_file: paramiko.SFTPFile,
        chunk_size: int = 65536,
        max_line_bytes: int | None = None,
    ) -> Iterator[tuple[int, bytes, bool]]:
        """Yield ``(line_no, raw, complete)`` without buffering the whole file.

        ``complete`` is False only for a fragment of a physical line longer than
        ``max_line_bytes``; fragments keep the same ``line_no`` so they are never
        mistaken for extra lines.
        """
        buffer = b""
        line_no = 0
        in_partial = False
        while True:
            chunk = remote_file.read(chunk_size)
            if not chunk:
                break
            buffer += chunk
            while True:
                idx = buffer.find(b"\n")
                if idx < 0:
                    break
                if not in_partial:
                    line_no += 1
                yield line_no, buffer[: idx + 1], True
                in_partial = False
                buffer = buffer[idx + 1 :]
            if max_line_bytes is not None and len(buffer) > max_line_bytes:
                if not in_partial:
                    line_no += 1
                    in_partial = True
                yield line_no, buffer[:max_line_bytes], False
                buffer = buffer[max_line_bytes:]
        if buffer:
            if not in_partial:
                line_no += 1
            yield line_no, buffer, True

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
        offset: int,
        line_mode: bool,
        sudo_password: str | None,
        timeout: int,
    ) -> FileContent:
        """Read through ``sudo`` when SFTP is denied.

        The payload is base64-wrapped so the byte cap cannot split a character
        on the wire, and a leading ``sudo test -r`` guard makes an unreadable
        file surface as an error instead of an empty success.
        """
        logger = self.logger.getChild("sudo_read")
        quoted = shlex.quote(remote_path)
        if line_mode:
            source = f"sudo sed -n '{start_line},$p' {quoted}"
        else:
            source = f"sudo tail -c +{offset + 1} {quoted}"
        cmd = f"sudo test -r {quoted} && {source} | head -c {byte_limit + 1} | base64"
        logger.debug(f"Sudo fallback command: {cmd}")

        result = await self._session_manager.execute_result(
            host=host,
            username=username,
            command=cmd,
            password=password,
            key_filename=key_filename,
            port=port,
            sudo_password=sudo_password,
            timeout=timeout,
        )
        if result.exit_code != 0:
            logger.error(f"Sudo fallback failed for {remote_path}: {result.stderr}")
            return FileContent(
                path=remote_path,
                error=f"Permission denied and sudo failed: {result.stderr}",
            )
        if result.truncated:
            logger.error(f"Sudo output for {remote_path} exceeded the output cap.")
            return FileContent(
                path=remote_path,
                error=(
                    "Sudo read output exceeded the command output cap; "
                    "reduce max_bytes"
                ),
            )

        try:
            raw = base64.b64decode(result.stdout, validate=False)
        except Exception as e:
            logger.error(f"Failed to decode sudo output for {remote_path}: {e}")
            return FileContent(
                path=remote_path, error=f"Failed to decode sudo output: {e!s}"
            )

        truncated = len(raw) > byte_limit
        if truncated:
            raw = raw[:byte_limit]
        if line_mode and truncated and not raw.endswith(b"\n"):
            # Drop the partial trailing line so the window stays line-aligned.
            idx = raw.rfind(b"\n")
            raw = raw[: idx + 1] if idx >= 0 else b""
        raw = _trim_partial_char(raw, encoding, errors)
        text = raw.decode(encoding, errors)
        line_count = text.count("\n") + (1 if text and not text.endswith("\n") else 0)

        if line_mode:
            end_line = start_line + line_count - 1 if text else start_line - 1
            window = _Window(
                text=text,
                truncated=truncated,
                bytes_read=len(raw),
                offset=0,
                next_offset=None,
                start_line=start_line,
                end_line=end_line,
                total_lines=None if truncated else end_line,
                next_start_line=end_line + 1 if truncated and text else None,
            )
        else:
            known = offset == 0
            window = _Window(
                text=text,
                truncated=truncated,
                bytes_read=len(raw),
                offset=offset,
                next_offset=offset + len(raw) if truncated else None,
                start_line=1 if known else None,
                end_line=line_count if known else None,
                total_lines=line_count if known and not truncated else None,
                next_start_line=None,
            )

        logger.info(f"Successfully read file {remote_path} via sudo fallback.")
        return _to_file_content(window, remote_path, byte_limit)

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
class _Window:
    """Internal carrier for one bounded, character-aligned slice of a file."""

    text: str
    truncated: bool
    bytes_read: int
    offset: int
    next_offset: int | None
    start_line: int | None
    end_line: int | None
    total_lines: int | None
    next_start_line: int | None


def _to_file_content(window: _Window, path: str, byte_limit: int) -> FileContent:
    """Promote an internal window into the public :class:`FileContent`."""
    return FileContent(
        content=window.text,
        path=path,
        truncated=window.truncated,
        max_bytes=byte_limit,
        bytes_read=window.bytes_read,
        offset=window.offset,
        next_offset=window.next_offset,
        start_line=window.start_line,
        end_line=window.end_line,
        total_lines=window.total_lines,
        next_start_line=window.next_start_line,
    )


def _newline_is_single_byte(encoding: str) -> bool:
    """True when a newline encodes to a single ``0x0A`` byte."""
    try:
        return "\n".encode(encoding) == b"\n"
    except (LookupError, UnicodeError):
        return False


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
