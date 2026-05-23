"""Paint MCP server, agent helpers, and backend tests."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

import paint_mcp_server as pms
from paint_mcp_server import MONITOR_INDEX, MonitorInfo, to_screen_with_monitor
from talk2mcp import (
    REQUIRED_TOOLS,
    all_tools_called,
    build_tool_nudge,
    build_user_prompt,
    dry_run_agent,
    extract_function_call_args,
    is_valid_api_key,
    mcp_tools_to_gemini,
    missing_tools,
    next_required_tool,
    run_agent,
    validate_tool_order,
    _sanitize_schema,
)

ROOT = Path(__file__).parent.parent
TALK2MCP = ROOT / "talk2mcp.py"
PAINT_SERVER = ROOT / "paint_mcp_server.py"


# --- Assignment / agent -------------------------------------------------------

def test_required_tools_exactly_three():
    assert REQUIRED_TOOLS == (
        "open_paint",
        "draw_rectangle",
        "add_text_in_paint",
    )


def test_default_second_monitor():
    assert MONITOR_INDEX == 1


def test_second_monitor_coordinate_offset():
    mon = MonitorInfo(index=1, left=1920, top=0, width=1920, height=1080)
    assert to_screen_with_monitor(320, 280, mon) == (2240, 280)


def test_user_prompt_lists_all_three_tools_in_order():
    prompt = build_user_prompt("What is AI?")
    assert "1. open_paint" in prompt
    assert "2. draw_rectangle" in prompt
    assert "3. add_text_in_paint" in prompt
    assert 'text="What is AI?"' in prompt
    idx_open = prompt.index("open_paint")
    idx_rect = prompt.index("draw_rectangle")
    idx_text = prompt.index("add_text_in_paint")
    assert idx_open < idx_rect < idx_text


def test_tool_order_validation():
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


def test_talk2mcp_does_not_import_paint_automation():
    forbidden = _forbidden_imports_in_talk2mcp()
    assert forbidden == [], f"talk2mcp.py must not import: {forbidden}"


def test_talk2mcp_does_not_call_paint_functions_directly():
    tree = ast.parse(TALK2MCP.read_text(encoding="utf-8"))
    paint_calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in REQUIRED_TOOLS:
                paint_calls.append(node.func.id)
    assert paint_calls == [], f"Direct paint calls forbidden in talk2mcp.py: {paint_calls}"


def test_paint_server_defines_three_tools():
    source = PAINT_SERVER.read_text(encoding="utf-8")
    for name in REQUIRED_TOOLS:
        assert f"def {name}" in source


def test_has_windows_and_wsl_backends():
    source = PAINT_SERVER.read_text(encoding="utf-8")
    assert "class WslPaintBackend" in source
    assert "class WindowsPaintBackend" in source
    assert "win32gui" in source
    assert "pywinauto" in source
    assert "Pillow" in source or "PIL" in source


@pytest.mark.asyncio
async def test_mcp_server_exposes_three_tools_via_stdio():
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


def test_build_user_prompt_contains_question():
    prompt = build_user_prompt("What is AI?")
    assert "What is AI?" in prompt


def test_validate_tool_order_rejects_skipping_open_paint():
    err = validate_tool_order(set(), "draw_rectangle")
    assert err is not None
    assert "open_paint" in err


def test_validate_tool_order_rejects_duplicate():
    err = validate_tool_order({"open_paint"}, "open_paint")
    assert err is not None
    assert "already called" in err


def test_validate_tool_order_rejects_after_all_done():
    err = validate_tool_order(set(REQUIRED_TOOLS), "open_paint")
    assert err is not None
    assert "already done" in err.lower() or "do not call" in err.lower()


def test_sanitize_schema_defaults():
    schema = _sanitize_schema(None)
    assert schema["type"] == "object"
    assert "properties" in schema


def test_sanitize_schema_removes_dollar_schema():
    schema = _sanitize_schema({"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"})
    assert "$schema" not in schema


def test_missing_tools_order():
    assert missing_tools(set()) == list(REQUIRED_TOOLS)
    assert missing_tools({"open_paint"}) == ["draw_rectangle", "add_text_in_paint"]
    assert missing_tools(set(REQUIRED_TOOLS)) == []


def test_all_tools_called():
    assert not all_tools_called(set())
    assert all_tools_called(set(REQUIRED_TOOLS))


def test_next_required_tool():
    assert next_required_tool(set()) == "open_paint"
    assert next_required_tool({"open_paint", "draw_rectangle"}) == "add_text_in_paint"
    assert next_required_tool(set(REQUIRED_TOOLS)) is None


def test_build_tool_nudge():
    nudge = build_tool_nudge({"open_paint"})
    assert "draw_rectangle" in nudge
    assert "add_text_in_paint" in nudge


def test_extract_function_call_args_direct():
    fc = SimpleNamespace(args={"text": "hello"}, function_call=None)
    assert extract_function_call_args(fc) == {"text": "hello"}


def test_extract_function_call_args_strips_null():
    fc = SimpleNamespace(args={"text": "hi", "x": None}, function_call=None)
    assert extract_function_call_args(fc) == {"text": "hi"}


def test_is_valid_api_key():
    assert is_valid_api_key("AIzaSyRealKey123") is True
    assert is_valid_api_key("") is False
    assert is_valid_api_key("your_gemini_api_key_here") is False
    assert is_valid_api_key(None) is False


def test_extract_function_call_args_nested():
    fc = SimpleNamespace(
        args=None,
        function_call=SimpleNamespace(args={"x1": 1}),
    )
    assert extract_function_call_args(fc) == {"x1": 1}


def test_mcp_tools_to_gemini():
    tools = [
        SimpleNamespace(
            name="open_paint",
            description="Open Paint",
            inputSchema={"type": "object", "properties": {}},
        )
    ]
    tools_result = SimpleNamespace(tools=tools)
    gemini_tool = mcp_tools_to_gemini(tools_result)
    assert len(gemini_tool.function_declarations) == 1
    assert gemini_tool.function_declarations[0].name == "open_paint"


def test_dry_run_agent_transcript():
    result = dry_run_agent("Hello world")
    assert result["dry_run"] is True
    assert result["model"] == "dry-run"
    tool_calls = [t for t in result["transcript"] if t["type"] == "tool_call"]
    assert [t["name"] for t in tool_calls] == list(REQUIRED_TOOLS)
    text_call = next(t for t in tool_calls if t["name"] == "add_text_in_paint")
    assert text_call["args"]["text"] == "Hello world"


@pytest.mark.asyncio
async def test_run_agent_dry_run_flag():
    result = await run_agent("Test question", dry_run=True)
    assert result["dry_run"] is True
    assert result["final_response"]
    assert len(result["transcript"]) >= 7


# --- Backends -----------------------------------------------------------------

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


def test_is_wsl_true_in_this_environment():
    assert pms.is_wsl() is True


def test_resolve_backend_auto_is_wsl_on_wsl():
    with patch.dict("os.environ", {"DRAW_BACKEND": "auto"}, clear=False):
        assert pms.resolve_backend() == "wsl"


def test_wsl_open_paint_creates_canvas(tmp_path, monkeypatch):
    monkeypatch.setattr(pms, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pms, "CANVAS_FILE", tmp_path / "canvas.png")
    monkeypatch.setattr(pms, "WSL_OPEN_IN_PAINT", False)
    monkeypatch.setattr(pms, "PAINT_AUTO_CLOSE", False)

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
    monkeypatch.setattr(pms, "PAINT_AUTO_CLOSE", False)

    backend = pms.WslPaintBackend()
    backend.open_paint()
    backend.draw_rectangle()
    result = backend.add_text_in_paint("What is MCP?")
    assert "What is MCP?" in result
    assert (tmp_path / "canvas.png").exists()


@pytest.mark.asyncio
async def test_mcp_open_paint_on_wsl():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "paint_mcp_server.py")],
        cwd=str(ROOT),
        env={"DRAW_BACKEND": "wsl", "WSL_OPEN_IN_PAINT": "false"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("open_paint", arguments={})
            text = result.content[0].text
            assert "WSL2 backend" in text or "canvas" in text.lower()
