"""
Assignment compliance tests — EAG V3 Week 4 Paint MCP.

Verifies:
- Custom MCP server exposes exactly 3 Paint tools
- Agent (talk2mcp.py) never imports/calls Paint automation directly
- Tool order enforcement matches assignment: open_paint → draw_rectangle → add_text_in_paint
- Second monitor defaults and coordinate helpers
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from paint_mcp_server import MONITOR_INDEX, MonitorInfo, to_screen_with_monitor
from talk2mcp import (
    REQUIRED_TOOLS,
    build_user_prompt,
    validate_tool_order,
)

ROOT = Path(__file__).parent.parent
TALK2MCP = ROOT / "talk2mcp.py"
PAINT_SERVER = ROOT / "paint_mcp_server.py"


def test_assignment_required_tools_exactly_three():
    assert REQUIRED_TOOLS == (
        "open_paint",
        "draw_rectangle",
        "add_text_in_paint",
    )


def test_assignment_default_second_monitor():
    assert MONITOR_INDEX == 1


def test_assignment_second_monitor_coordinate_offset():
    """Second monitor at x=1920 — coords must shift by monitor origin."""
    mon = MonitorInfo(index=1, left=1920, top=0, width=1920, height=1080)
    assert to_screen_with_monitor(320, 280, mon) == (2240, 280)


def test_assignment_user_prompt_lists_all_three_tools_in_order():
    prompt = build_user_prompt("What is AI?")
    assert "1. open_paint" in prompt
    assert "2. draw_rectangle" in prompt
    assert "3. add_text_in_paint" in prompt
    assert 'text="What is AI?"' in prompt
    idx_open = prompt.index("open_paint")
    idx_rect = prompt.index("draw_rectangle")
    idx_text = prompt.index("add_text_in_paint")
    assert idx_open < idx_rect < idx_text


def test_assignment_tool_order_validation():
    assert validate_tool_order(set(), "open_paint") is None
    assert validate_tool_order(set(), "draw_rectangle") is not None
    assert validate_tool_order({"open_paint"}, "draw_rectangle") is None
    assert validate_tool_order({"open_paint"}, "add_text_in_paint") is not None
    assert validate_tool_order({"open_paint", "draw_rectangle"}, "add_text_in_paint") is None


def _forbidden_imports_in_talk2mcp() -> list[str]:
    forbidden = {
        "pyautogui",
        "pywinauto",
        "win32gui",
        "win32con",
        "win32api",
        "paint_mcp_server",
    }
    tree = ast.parse(TALK2MCP.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in forbidden:
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in forbidden:
                    found.append(node.module)
    return found


def test_assignment_talk2mcp_does_not_import_paint_automation():
    """Agent must only reach Paint via MCP — no direct pywin32/pyautogui imports."""
    forbidden = _forbidden_imports_in_talk2mcp()
    assert forbidden == [], f"talk2mcp.py must not import: {forbidden}"


def test_assignment_talk2mcp_does_not_call_paint_functions_directly():
    """Agent must not call open_paint/draw_rectangle/add_text_in_paint as Python functions."""
    tree = ast.parse(TALK2MCP.read_text(encoding="utf-8"))
    paint_calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in REQUIRED_TOOLS:
                paint_calls.append(node.func.id)
    assert paint_calls == [], f"Direct paint calls forbidden in talk2mcp.py: {paint_calls}"


def test_assignment_paint_server_defines_three_tools():
    source = PAINT_SERVER.read_text(encoding="utf-8")
    for name in REQUIRED_TOOLS:
        assert f"def {name}" in source


def test_assignment_has_windows_and_wsl_backends():
    source = PAINT_SERVER.read_text(encoding="utf-8")
    assert "class WslPaintBackend" in source
    assert "class WindowsPaintBackend" in source
    assert "win32gui" in source
    assert "pywinauto" in source
    assert "Pillow" in source or "PIL" in source


@pytest.mark.asyncio
async def test_assignment_mcp_server_exposes_three_tools_via_stdio():
    """MCP server lists exactly the 3 Paint tools (no Paint execution — list_tools only)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(PAINT_SERVER)],
        cwd=str(ROOT),
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            assert names == sorted(REQUIRED_TOOLS)
