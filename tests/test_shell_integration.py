"""Tests for the OSC 633 marker layer."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from mcp_ssh_reloaded.shell_integration import (
    build_install_script,
    feed,
    find_completion,
    has_marker,
    new_nonce,
    strip_markers,
)

BEL = "\x07"
ESC = "\x1b"
NONCE = "0123456789abcdef"


def _stage(tmp_path: Path, nonce: str = NONCE) -> Path:
    script = tmp_path / "integ.sh"
    script.write_text(build_install_script(nonce))
    return script


def _bash(code: str, script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "--norc", "-c", f". {script}; {code}"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_nonce_is_hex_and_short() -> None:
    nonce = new_nonce()
    assert len(nonce) == 16
    assert int(nonce, 16) >= 0


def test_install_script_carries_the_nonce(tmp_path: Path) -> None:
    assert NONCE in _stage(tmp_path).read_text()


def test_finds_marker_with_matching_nonce() -> None:
    text = f"out{ESC}]633;D;0;{NONCE}{BEL}prompt"
    assert find_completion(text, NONCE) == (len("out"), 0)


def test_ignores_marker_with_other_nonce() -> None:
    text = f"out{ESC}]633;D;0;deadbeefdeadbeef{BEL}prompt"
    assert find_completion(text, NONCE) is None


def test_parses_string_terminator_marker() -> None:
    text = f"out{ESC}]633;D;130;{NONCE}{ESC}\\prompt"
    assert find_completion(text, NONCE) == (len("out"), 130)


def test_strip_markers_keeps_output() -> None:
    text = f"before{ESC}]633;A;{NONCE}{BEL}mid{ESC}]633;D;0;{NONCE}{BEL}after"
    assert strip_markers(text, NONCE) == "beforemidafter"


def test_has_marker_ignores_untagged_osc() -> None:
    assert not has_marker(f"{ESC}]0;window title{BEL}", NONCE)


def test_feed_finds_marker_split_across_every_byte() -> None:
    stream = f"out{ESC}]633;D;7;{NONCE}{BEL}prompt"
    carry = ""
    hits = []
    for index, char in enumerate(stream):
        carry, code = feed(carry, char, NONCE)
        if code is not None:
            hits.append((index, code))
    assert hits[0] == (stream.index(BEL), 7)
    assert {code for _, code in hits} == {7}


def test_feed_reports_nothing_without_a_marker() -> None:
    carry, code = feed("", "plain output", NONCE)
    assert code is None
    assert carry == "plain output"


def test_install_script_preserves_command_status(tmp_path: Path) -> None:
    result = _bash('false; __mcp633; printf "%s" "$?"', _stage(tmp_path))
    assert strip_markers(result.stdout, NONCE) == "1"


def test_install_script_wires_scalar_prompt_command(tmp_path: Path) -> None:
    script = _stage(tmp_path)
    result = subprocess.run(
        ["bash", "--norc", "-c", f'PROMPT_COMMAND="echo old"; . {script}; printf "%s" "$PROMPT_COMMAND"'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout == "__mcp633;echo old"


def test_install_script_keeps_array_prompt_command(tmp_path: Path) -> None:
    script = _stage(tmp_path)
    result = subprocess.run(
        [
            "bash",
            "--norc",
            "-c",
            f'PROMPT_COMMAND=(first second); . {script}; printf "%s" "${{PROMPT_COMMAND[*]}}"',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.split() == ["__mcp633", "first", "second"]


def test_install_script_is_idempotent(tmp_path: Path) -> None:
    script = _stage(tmp_path)
    result = subprocess.run(
        ["bash", "--norc", "-c", f". {script}; . {script}; printf '%s' \"$PROMPT_COMMAND\""],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.count("__mcp633") == 1


def test_install_script_prepends_on_zsh(tmp_path: Path) -> None:
    if shutil.which("zsh") is None:
        pytest.skip("zsh not installed")
    script = _stage(tmp_path)
    result = subprocess.run(
        [
            "zsh",
            "-f",
            "-c",
            f'precmd_functions=(other); . {script}; printf "%s" "${{precmd_functions[*]}}"',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.split() == ["__mcp633", "other"]


def test_install_script_is_inert_under_sh(tmp_path: Path) -> None:
    result = subprocess.run(
        ["sh", "-c", f". {_stage(tmp_path)}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert not has_marker(result.stdout, NONCE)


def test_install_script_emits_through_a_real_pty() -> None:
    host = os.environ.get("SSH_TEST_HOST")
    if not host:
        pytest.skip("SSH_TEST_HOST not set")
    paramiko = pytest.importorskip("paramiko")
    nonce = new_nonce()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=int(os.environ.get("SSH_TEST_PORT", "22")),
        username=os.environ.get("SSH_TEST_USER", "root"),
        password=os.environ.get("SSH_TEST_PASSWORD"),
        look_for_keys=False,
        allow_agent=False,
    )
    try:
        sftp = client.open_sftp()
        with sftp.open("/tmp/mcp633_test.sh", "w") as handle:
            handle.write(build_install_script(nonce))
        sftp.close()
        shell = client.invoke_shell()
        shell.settimeout(10)
        time.sleep(1.0)
        while shell.recv_ready():
            shell.recv(65535)
        shell.send(b". /tmp/mcp633_test.sh; rm -f /tmp/mcp633_test.sh\n")
        seen = ""
        deadline = time.time() + 5.0
        while time.time() < deadline and not has_marker(seen, nonce):
            if shell.recv_ready():
                seen += shell.recv(65535).decode("utf-8", errors="ignore")
            else:
                time.sleep(0.1)
        shell.close()
    finally:
        client.close()
    found = find_completion(seen, nonce)
    assert found is not None
    assert found[1] == 0
