"""OSC 633 shell integration: completion and exit codes reported by the shell.

VSCode does the same thing. The shell prints zero-width escape sequences around
each prompt, so the reader never has to guess a prompt out of the byte stream.
Markers are emitted by PROMPT_COMMAND / precmd, never typed, so readline redraw
and program output cannot collide with them.

Every marker carries a per-session nonce. The nonce lives in the shell function
body and is never exported, so a command or child process cannot forge a
completion marker that the reader would believe.
"""

from __future__ import annotations

import re
import uuid

CARRY_BYTES = 64

_INSTALL_TEMPLATE = """__mcp633() { local __r=$?; printf '\\033]633;D;%s;NONCE\\007' "$__r"; printf '\\033]633;A;NONCE\\007'; return $__r; }
if [ -n "${BASH_VERSION:-}" ]; then
  if declare -p PROMPT_COMMAND 2>/dev/null | grep -q '^declare -a'; then
    case " ${PROMPT_COMMAND[*]} " in
      *" __mcp633 "*) ;;
      *) eval 'PROMPT_COMMAND=(__mcp633 "${PROMPT_COMMAND[@]}")' ;;
    esac
  else
    case ";${PROMPT_COMMAND:-};" in
      *";__mcp633;"*) ;;
      *) PROMPT_COMMAND="__mcp633${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
    esac
  fi
elif [ -n "${ZSH_VERSION:-}" ]; then
  case " ${precmd_functions[*]:-} " in
    *" __mcp633 "*) ;;
    *) eval 'precmd_functions=(__mcp633 ${precmd_functions[@]:-})' ;;
  esac
fi
"""


def new_nonce() -> str:
    """Return a fresh session nonce that a child process cannot read."""
    return uuid.uuid4().hex[:16]


def build_install_script(nonce: str) -> str:
    """Return the bash/zsh snippet that emits nonce-tagged markers."""
    return _INSTALL_TEMPLATE.replace("NONCE", nonce)


def done_pattern(nonce: str) -> re.Pattern[str]:
    """Pattern matching the completion marker for one nonce."""
    return re.compile(rf"\x1b\]633;D;(\d+);{re.escape(nonce)}(?:\x07|\x1b\\)")


def marker_pattern(nonce: str) -> re.Pattern[str]:
    """Pattern matching any OSC 633 marker carrying one nonce."""
    return re.compile(rf"\x1b\]633;[^\x07\x1b]*;{re.escape(nonce)}(?:\x07|\x1b\\)")


def has_marker(text: str, nonce: str) -> bool:
    """Whether a nonce-tagged marker is present, used to verify installation."""
    return marker_pattern(nonce).search(text) is not None


def strip_markers(text: str, nonce: str) -> str:
    """Drop every nonce-tagged marker while leaving the command output alone."""
    return marker_pattern(nonce).sub("", text)


def find_completion(text: str, nonce: str) -> tuple[int, int] | None:
    """Return the marker offset and exit code, or None when the shell has not reported."""
    match = done_pattern(nonce).search(text)
    return None if match is None else (match.start(), int(match.group(1)))


def feed(carry: str, chunk: str, nonce: str) -> tuple[str, int | None]:
    """Advance the carry over one chunk and report an exit code if one completed."""
    window = carry + chunk
    match = done_pattern(nonce).search(window)
    return window[-CARRY_BYTES:], None if match is None else int(match.group(1))
