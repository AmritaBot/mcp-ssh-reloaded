"""Bounded output buffering: head + tail in memory, everything on disk.

Replaces :class:`~mcp_ssh_reloaded.validation.OutputLimiter`, which simply cut
the stream off mid-way and reported it by borrowing exit code ``124`` (the same
code used for timeouts).

``OutputBuffer`` keeps the same ``add_chunk`` call shape so existing read loops
barely change, but adds:

* ``render()`` - show the head and the tail of a truncated stream instead of
  only the beginning, so the interesting part (the last error, the prompt) is
  still visible;
* spill-to-disk - once the stream outgrows ``spill_threshold`` the full output
  is written to a temporary file and its path is reported back;
* an explicit ``truncated`` flag, so callers stop overloading exit codes.
"""

from __future__ import annotations

import os
import tempfile

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_HEAD_BYTES = 8 * 1024
DEFAULT_TAIL_BYTES = 32 * 1024
DEFAULT_SPILL_THRESHOLD = 64 * 1024


class OutputBuffer:
    """Collect command output with a bounded memory footprint."""

    def __init__(
        self,
        max_size: int = DEFAULT_MAX_BYTES,
        *,
        head_size: int = DEFAULT_HEAD_BYTES,
        tail_size: int = DEFAULT_TAIL_BYTES,
        spill_threshold: int = DEFAULT_SPILL_THRESHOLD,
        spill_dir: str | None = None,
    ):
        self.max_size = max_size
        self.head_size = head_size
        self.tail_size = tail_size
        self.spill_threshold = spill_threshold
        self.spill_dir = spill_dir

        self.current_size = 0
        self.truncated = False
        self.hard_capped = False
        self.spilled_path: str | None = None

        self._pending: list[str] = []
        self._spill = None

    # -- collection --------------------------------------------------------

    def add_chunk(self, chunk: str) -> tuple[str, bool]:
        """Add a chunk. Returns ``(chunk_to_append, should_continue)``."""
        if not chunk:
            return chunk, not self.hard_capped

        size = len(chunk.encode("utf-8"))
        if self.current_size + size > self.max_size:
            remaining = self.max_size - self.current_size
            if remaining <= 0:
                self.hard_capped = True
                self.truncated = True
                return "", False
            chunk = chunk.encode("utf-8")[:remaining].decode("utf-8", "ignore")
            size = len(chunk.encode("utf-8"))
            self.hard_capped = True

        self.current_size += size
        if self.current_size > self.head_size + self.tail_size:
            self.truncated = True

        self._write(chunk)
        return chunk, not self.hard_capped

    def _write(self, chunk: str) -> None:
        if self._spill is not None:
            self._spill.write(chunk)
            self._spill.flush()
            return
        self._pending.append(chunk)
        if self.current_size > self.spill_threshold:
            self._open_spill()

    def _open_spill(self) -> None:
        try:
            fd, path = tempfile.mkstemp(
                prefix="mcp_ssh_output_", suffix=".log", dir=self.spill_dir
            )
            self._spill = os.fdopen(fd, "w", encoding="utf-8", errors="replace")
            self.spilled_path = path
            for piece in self._pending:
                self._spill.write(piece)
            self._pending.clear()
        except OSError:
            # Spilling is best-effort; keep collecting in memory instead.
            self._spill = None
            self.spilled_path = None

    # -- rendering ---------------------------------------------------------

    def render(self, text: str) -> str:
        """Head + tail view of *text*, with the spill path when available."""
        if not self.truncated:
            return text

        head = text[: self.head_size]
        tail = text[-self.tail_size :] if len(text) > self.tail_size else ""
        omitted = max(0, len(text) - len(head) - len(tail))
        rendered = f"{head}\n\n... [{omitted} characters omitted] ...\n\n{tail}"
        if self.spilled_path:
            rendered += f"\n\n[full output saved to {self.spilled_path}]"
        return rendered

    @property
    def limit_message(self) -> str:
        """Human-readable reason to use when the hard cap is hit."""
        if self.spilled_path:
            return (
                f"Output exceeded {self.max_size} bytes; "
                f"full output saved to {self.spilled_path}"
            )
        return f"Output exceeded {self.max_size} bytes"

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._spill is not None:
            try:
                self._spill.close()
            finally:
                self._spill = None
