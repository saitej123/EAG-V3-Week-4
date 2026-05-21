"""Tests for Linux canvas backend."""

from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

import paint_mcp_server as pms


def test_linux_add_text_in_paint(tmp_path, monkeypatch):
    monkeypatch.setattr(pms, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pms, "CANVAS_FILE", tmp_path / "canvas.png")
    monkeypatch.setattr(pms, "LINUX_OPEN_VIEWER", False)

    backend = pms.LinuxCanvasBackend()
    backend.open_paint()
    backend.draw_rectangle()
    result = backend.add_text_in_paint("Hello Linux")
    assert "Hello Linux" in result
    assert (tmp_path / "canvas.png").exists()


def test_linux_add_text_requires_non_empty():
    backend = pms.LinuxCanvasBackend()
    with pytest.raises(ValueError, match="text must not be empty"):
        backend.add_text_in_paint("   ")
