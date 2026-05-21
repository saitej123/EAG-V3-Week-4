"""Tests for Talk2MCP agent helpers (no Gemini / no Paint)."""

import asyncio
from types import SimpleNamespace

import pytest

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


def test_build_user_prompt_contains_question():
    prompt = build_user_prompt("What is AI?")
    assert "What is AI?" in prompt
    assert "1. open_paint" in prompt
    assert "2. draw_rectangle" in prompt
    assert "3. add_text_in_paint" in prompt


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
