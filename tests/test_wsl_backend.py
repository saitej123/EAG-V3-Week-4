"""WSL2 / Pillow backend tests."""

from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

import paint_mcp_server as pms


def test_is_wsl_true_in_this_environment():
    assert pms.is_wsl() is True


def test_resolve_backend_auto_is_wsl_on_wsl():
    with patch.dict("os.environ", {"DRAW_BACKEND": "auto"}, clear=False):
        assert pms.resolve_backend() == "wsl"


def test_wsl_open_paint_creates_canvas(tmp_path, monkeypatch):
    monkeypatch.setattr(pms, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pms, "CANVAS_FILE", tmp_path / "canvas.png")
    monkeypatch.setattr(pms, "WSL_OPEN_IN_PAINT", False)

    backend = pms.WslPaintBackend()
    msg = backend.open_paint()
    assert "canvas" in msg.lower()
    assert (tmp_path / "canvas.png").exists()
    img = Image.open(tmp_path / "canvas.png")
    assert img.size == (pms.CANVAS_WIDTH, pms.CANVAS_HEIGHT)


def test_wsl_full_flow_rectangle_and_text(tmp_path, monkeypatch):
    monkeypatch.setattr(pms, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pms, "CANVAS_FILE", tmp_path / "canvas.png")
    monkeypatch.setattr(pms, "WSL_OPEN_IN_PAINT", False)

    backend = pms.WslPaintBackend()
    backend.open_paint()
    backend.draw_rectangle()
    result = backend.add_text_in_paint("What is MCP?")
    assert "What is MCP?" in result
    assert (tmp_path / "canvas.png").exists()


@pytest.mark.asyncio
async def test_mcp_open_paint_on_wsl():
    """Integration: MCP open_paint must succeed on WSL (no Windows Python needed)."""
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = Path(__file__).parent.parent
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(root / "paint_mcp_server.py")],
        cwd=str(root),
        env={"DRAW_BACKEND": "wsl", "WSL_OPEN_IN_PAINT": "false"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("open_paint", arguments={})
            text = result.content[0].text
            assert "WSL2 backend" in text or "canvas" in text.lower()
