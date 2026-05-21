"""Tests for Talk2Gmail bonus agent (no Gmail API)."""

import pytest

from talk2gmail import (
    REQUIRED_TOOL,
    build_user_prompt,
    dry_run_gmail,
)


def test_build_user_prompt_includes_email_fields():
    prompt = build_user_prompt("a@b.com", "Hello", "Body text")
    assert "a@b.com" in prompt
    assert "Hello" in prompt
    assert "Body text" in prompt
    assert REQUIRED_TOOL in prompt


def test_dry_run_gmail_calls_send_email():
    result = dry_run_gmail("test@example.com", "Subj", "Msg")
    assert result["dry_run"] is True
    assert result["tools_called"] == [REQUIRED_TOOL]
    calls = [t for t in result["transcript"] if t["type"] == "tool_call"]
    assert calls[0]["args"]["recipient_id"] == "test@example.com"


def test_build_gmail_server_params_missing_env():
    from talk2gmail import build_gmail_server_params

    with pytest.raises(RuntimeError, match="GMAIL_MCP_REPO"):
        build_gmail_server_params()
