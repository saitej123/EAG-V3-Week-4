"""Gmail agent and OAuth config tests."""

import json

import pytest

from talk2gmail import (
    GCP_LINKS,
    REQUIRED_TOOL,
    build_gmail_server_params,
    build_user_prompt,
    create_oauth_flow,
    dry_run_gmail,
    gmail_creds_path,
    gmail_oauth_redirect_uri,
    gmail_setup_status,
    gmail_token_path,
    oauth_authorization_url,
    oauth_setup_steps,
    validate_creds_file,
    validate_token_file,
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


def test_build_gmail_server_params_missing_env(monkeypatch):
    for key in (
        "GMAIL_MCP_REPO",
        "GMAIL_OAUTH_CREDS_FILE",
        "GMAIL_OAUTH_TOKEN_FILE",
        "GMAIL_CREDS_FILE",
        "GMAIL_TOKEN_FILE",
    ):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(RuntimeError, match="GMAIL_MCP_REPO"):
        build_gmail_server_params()


def test_legacy_env_aliases(tmp_path, monkeypatch):
    creds = tmp_path / "creds.json"
    token = tmp_path / "token.json"
    creds.write_text(
        '{"installed":{"client_id":"x","client_secret":"y","redirect_uris":["http://localhost"]}}',
        encoding="utf-8",
    )
    token.write_text('{"token":"abc","refresh_token":"def"}', encoding="utf-8")

    monkeypatch.delenv("GMAIL_OAUTH_CREDS_FILE", raising=False)
    monkeypatch.delenv("GMAIL_OAUTH_TOKEN_FILE", raising=False)
    monkeypatch.setenv("GMAIL_CREDS_FILE", str(creds))
    monkeypatch.setenv("GMAIL_TOKEN_FILE", str(token))

    assert gmail_creds_path() == creds.resolve()
    assert gmail_token_path() == token.resolve()


def test_rejects_service_account(tmp_path):
    p = tmp_path / "sa.json"
    p.write_text('{"type":"service_account","client_email":"x@y.iam.gserviceaccount.com"}')
    ok, msg = validate_creds_file(p)
    assert ok is False
    assert "Service account" in msg


def test_validate_token_empty(tmp_path):
    p = tmp_path / "token.json"
    p.write_text("", encoding="utf-8")
    ok, msg = validate_token_file(p)
    assert ok is False
    assert "token" in msg.lower()


def test_gmail_web_redirect_uri_uses_port():
    from talk2gmail import gmail_web_redirect_uri

    assert gmail_web_redirect_uri(8090) == "http://localhost:8090"


def test_oauth_pending_saved_after_auth_url(tmp_path, monkeypatch):
    creds = tmp_path / "creds.json"
    creds.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "x.apps.googleusercontent.com",
                    "client_secret": "y",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )
    token = tmp_path / "token.json"
    monkeypatch.setenv("GMAIL_OAUTH_CREDS_FILE", str(creds))
    monkeypatch.setenv("GMAIL_OAUTH_TOKEN_FILE", str(token))
    monkeypatch.setenv("GMAIL_OAUTH_REDIRECT_URI", "http://localhost")

    from talk2gmail import (
        _load_oauth_pending,
        _save_oauth_pending,
        create_oauth_flow,
        gmail_scopes,
        oauth_authorization_url,
        oauth_pending_path,
    )

    flow = create_oauth_flow(gmail_scopes(), "http://localhost")
    url = oauth_authorization_url(flow)
    _save_oauth_pending(flow, "http://localhost", url)
    pending = _load_oauth_pending()
    assert pending is not None
    assert pending["redirect_uri"] == "http://localhost"
    assert pending["code_verifier"]
    assert pending["state"]
    assert oauth_pending_path().exists()


def test_normalize_redirect_response_accepts_localhost_without_scheme():
    from talk2gmail import normalize_redirect_response

    url = normalize_redirect_response(
        "localhost/?state=abc&code=4/0Atest_code_value_here_1234567890",
        "http://localhost",
    )
    assert "code=4/0Atest_code" in url
    assert url.startswith("http://")


def test_normalize_redirect_response_rejects_bare_localhost():
    from talk2gmail import normalize_redirect_response

    try:
        normalize_redirect_response("http://localhost", "http://localhost")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "code" in str(exc).lower()


def test_normalize_redirect_response_accepts_code_only():
    from talk2gmail import normalize_redirect_response

    url = normalize_redirect_response("abc123def456ghi789jkl012", "http://localhost")
    assert url == "http://localhost?code=abc123def456ghi789jkl012"


def test_oauth_authorization_url_includes_redirect_uri(tmp_path, monkeypatch):
    creds = tmp_path / "creds.json"
    creds.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "x.apps.googleusercontent.com",
                    "client_secret": "y",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GMAIL_OAUTH_CREDS_FILE", str(creds))
    monkeypatch.setenv("GMAIL_OAUTH_REDIRECT_URI", "http://localhost")

    flow = create_oauth_flow(["https://www.googleapis.com/auth/gmail.modify"], "http://localhost")
    url = oauth_authorization_url(flow)
    assert "redirect_uri=" in url
    assert "response_type=code" in url


def test_oauth_setup_steps_available(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    creds = tmp_path / "creds.json"
    token = tmp_path / "token.json"
    creds.write_text(
        '{"installed":{"client_id":"x","client_secret":"y","redirect_uris":["http://localhost"]}}',
        encoding="utf-8",
    )

    monkeypatch.setenv("GMAIL_MCP_REPO", str(repo))
    monkeypatch.setenv("GMAIL_OAUTH_CREDS_FILE", str(creds))
    monkeypatch.setenv("GMAIL_OAUTH_TOKEN_FILE", str(token))

    steps = oauth_setup_steps()
    assert len(steps) >= 3
    assert "setup-oauth" in steps[1]["body"]
    assert "auth_clients" in GCP_LINKS
