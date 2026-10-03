"""Guardrails for OutputBuffer (head+tail rendering and spill-to-disk)."""

import os

from mcp_ssh_reloaded.output_buffer import OutputBuffer


def test_small_output_is_returned_verbatim():
    buf = OutputBuffer(max_size=1000)
    text = ""
    for chunk in ("hello ", "world", "\n"):
        piece, cont = buf.add_chunk(chunk)
        assert cont
        text += piece

    assert buf.truncated is False
    assert buf.spilled_path is None
    assert buf.render(text) == "hello world\n"


def test_large_output_keeps_head_and_tail():
    buf = OutputBuffer(max_size=10_000, head_size=10, tail_size=10, spill_threshold=50)
    text = ""
    for _ in range(50):
        piece, cont = buf.add_chunk("abcdefghij")
        assert cont
        text += piece

    assert buf.truncated is True
    rendered = buf.render(text)
    assert rendered.startswith(text[:10])
    assert text[-10:] in rendered
    assert "bytes omitted" in rendered


def test_spill_file_holds_the_full_stream():
    buf = OutputBuffer(
        max_size=10_000, head_size=5, tail_size=5, spill_threshold=20
    )
    text = ""
    for i in range(20):
        piece, _ = buf.add_chunk(f"chunk-{i};")
        text += piece

    assert buf.spilled_path is not None
    try:
        with open(buf.spilled_path, encoding="utf-8") as handle:
            assert handle.read() == text
        assert buf.spilled_path in buf.render(text)
    finally:
        os.unlink(buf.spilled_path)


def test_hard_cap_reports_and_stops():
    buf = OutputBuffer(max_size=10, head_size=4, tail_size=4, spill_threshold=8)
    piece, cont = buf.add_chunk("0123456789abcdef")
    assert cont is False
    assert buf.hard_capped is True
    assert buf.render(piece)  # still renders something useful
    buf.close()
