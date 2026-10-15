"""Tests for the OSC 633 marker layer."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from mcp_ssh_reloaded.shell_integration import (
    OSC633_INSTALL_SCRIPT,
    completion,
    exit_code_from,
    has_marker,
    strip_markers,
)

BEL = "\x07"
ESC = "\x1b"


def _run(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "integ.sh"
    script.write_text(OSC633_INSTALL_SCRIPT)
    return subprocess.run(
        ["bash", "--norc", "-c", f". {script}; {code}"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_parses_bel_terminated_marker() -> None:
    assert exit_code_from(f"out{ESC}]633;D;0{BEL}prompt") == 0


def test_parses_string_terminator_marker() -> None:
    assert exit_code_from(f"out{ESC}]633;D;130{ESC}\\prompt") == 130


def test_returns_none_without_marker() -> None:
    assert exit_code_from("just output\nand more") is None


def test_returns_first_marker_in_window() -> None:
    text = f"{ESC}]633;D;3{BEL}mid{ESC}]633;D;4{BEL}"
    assert exit_code_from(text) == 3


def test_has_marker_sees_prompt_start() -> None:
    assert has_marker(f"{ESC}]633;A{BEL}")


def test_has_marker_ignores_unrelated_osc() -> None:
    assert not has_marker(f"{ESC}]0;window title{BEL}")


def test_strip_markers_keeps_output() -> None:
    text = f"before{ESC}]633;A{BEL}middle{ESC}]633;D;0{BEL}after"
    assert strip_markers(text) == "beforemiddleafter"


def test_marker_fits_inside_carry_window() -> None:
    assert len(f"{ESC}]633;D;255{BEL}") <= 64


def test_install_script_reports_last_exit_code(tmp_path: Path) -> None:
    result = _run("false; __mcp633; true", tmp_path)
    assert exit_code_from(result.stdout) == 1


def test_install_script_wires_prompt_command(tmp_path: Path) -> None:
    result = _run('printf "%s" "$PROMPT_COMMAND"', tmp_path)
    assert "__mcp633" in result.stdout


def test_install_script_keeps_existing_prompt_command(tmp_path: Path) -> None:
    script = tmp_path / "integ.sh"
    script.write_text(OSC633_INSTALL_SCRIPT)
    result = subprocess.run(
        ["bash", "--norc", "-c", f'PROMPT_COMMAND="echo old"; . {script}; printf "%s" "$PROMPT_COMMAND"'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout == "__mcp633;echo old"


def test_install_script_is_idempotent(tmp_path: Path) -> None:
    script = tmp_path / "integ.sh"
    script.write_text(OSC633_INSTALL_SCRIPT)
    result = subprocess.run(
        ["bash", "--norc", "-c", f". {script}; . {script}; printf '%s' \"$PROMPT_COMMAND\""],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.count("__mcp633") == 1


def test_install_script_emits_through_a_real_pty() -> None:
    host = os.environ.get("SSH_TEST_HOST")
    if not host:
        pytest.skip("SSH_TEST_HOST not set")
    paramiko = pytest.importorskip("paramiko")
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
            handle.write(OSC633_INSTALL_SCRIPT)
        sftp.close()
        shell = client.invoke_shell()
        shell.settimeout(10)
        time.sleep(1.0)
        while shell.recv_ready():
            shell.recv(65535)
        shell.send(b". /tmp/mcp633_test.sh; rm -f /tmp/mcp633_test.sh\n")
        seen = ""
        deadline = time.time() + 5.0
        while time.time() < deadline and not has_marker(seen):
            if shell.recv_ready():
                seen += shell.recv(65535).decode("utf-8", errors="ignore")
            else:
                time.sleep(0.1)
        shell.close()
    finally:
        client.close()
    assert exit_code_from(seen) == 0


def test_completion_reports_marker_offset() -> None:
    text = f"payload{ESC}]633;D;9{BEL}prompt"
    assert completion(text) == (len("payload"), 9)


def test_completion_returns_none_without_marker() -> None:
    assert completion("plain output\nsecond line") is None


def test_install_script_is_inert_under_sh(tmp_path: Path) -> None:
    script = tmp_path / "integ.sh"
    script.write_text(OSC633_INSTALL_SCRIPT)
    result = subprocess.run(
        ["sh", "-c", f". {script}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert not has_marker(result.stdout)
