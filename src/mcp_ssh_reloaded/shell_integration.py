"""OSC 633 shell integration: completion and exit codes reported by the shell.

VSCode does the same thing. The shell prints zero-width escape sequences around
each prompt, so the reader never has to guess a prompt out of the byte stream.
Markers are emitted by PROMPT_COMMAND / precmd, never typed, so readline redraw
and program output cannot collide with them.
"""

from __future__ import annotations

import re

OSC633_DONE_RE = re.compile(r"\x1b\]633;D;(\d+)(?:\x07|\x1b\\)")
OSC633_ANY_RE = re.compile(r"\x1b\]633;[^\x07\x1b]*(?:\x07|\x1b\\)")

OSC633_INSTALL_SCRIPT = """__mcp633() { local __r=$?; printf '\\033]633;D;%s\\007' "$__r"; printf '\\033]633;A\\007'; }
if [ -n "${BASH_VERSION:-}" ]; then
  case ";${PROMPT_COMMAND:-};" in
    *";__mcp633;"*) ;;
    *) PROMPT_COMMAND="__mcp633${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
  esac
elif [ -n "${ZSH_VERSION:-}" ]; then
  autoload -Uz add-zsh-hook 2>/dev/null && add-zsh-hook precmd __mcp633 2>/dev/null
fi
"""


def completion(text: str) -> tuple[int, int] | None:
    """Return the marker offset and exit code of the last finished command."""
    match = OSC633_DONE_RE.search(text)
    return None if match is None else (match.start(), int(match.group(1)))


def exit_code_from(text: str) -> int | None:
    """Return the exit code the shell reported for the last finished command."""
    found = completion(text)
    return None if found is None else found[1]


def has_marker(text: str) -> bool:
    """Whether any OSC 633 marker is present, used to verify installation."""
    return OSC633_ANY_RE.search(text) is not None


def strip_markers(text: str) -> str:
    """Drop every OSC 633 marker while leaving the command output untouched."""
    return OSC633_ANY_RE.sub("", text)
