"""FastAPI endpoint tests (no Paint/Gemini)."""

import time

from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["version"] == "2.0.0"


def test_home_page():
    res = client.get("/")
    assert res.status_code == 200
    text = res.text
    assert "Talk2MCP" in text
    assert "Paint MCP" in text
    assert "Gmail MCP" in text
    assert "Run Live" in text
    assert "Dry Run" in text


def test_agents_api():
    res = client.get("/api/agents")
    assert res.status_code == 200
    data = res.json()
    ids = {a["id"] for a in data["agents"]}
    assert ids == {"paint", "gmail"}
    assert "api_key_valid" in data


def test_test_cases_paint():
    res = client.get("/api/test-cases?agent=paint")
    assert res.status_code == 200
    data = res.json()
    assert len(data["cases"]) >= 3
    assert "question" in data["cases"][0]


def test_test_cases_gmail():
    res = client.get("/api/test-cases?agent=gmail")
    assert res.status_code == 200
    data = res.json()
    assert len(data["cases"]) >= 2
    assert "subject" in data["cases"][0]


def test_test_cases_all():
    res = client.get("/api/test-cases")
    assert res.status_code == 200
    data = res.json()
    assert "paint" in data
    assert "gmail" in data


def test_status_idle():
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert data["running"] is False
    assert "gmail_configured" in data
    assert "last_agent" in data


def test_run_paint_dry_run():
    res = client.post(
        "/api/run",
        json={"agent": "paint", "question": "What is MCP?", "dry_run": True},
    )
    assert res.status_code == 200
    assert res.json()["agent"] == "paint"
    time.sleep(0.5)
    status = client.get("/api/status").json()
    assert status["last_result"] is not None
    assert status["last_result"]["agent"] == "paint"


def test_run_gmail_dry_run():
    res = client.post(
        "/api/run",
        json={
            "agent": "gmail",
            "to": "test@example.com",
            "subject": "Hi",
            "body": "Hello",
            "dry_run": True,
        },
    )
    assert res.status_code == 200
    assert res.json()["agent"] == "gmail"
    time.sleep(0.5)
    status = client.get("/api/status").json()
    assert status["last_result"]["agent"] == "gmail"


def test_run_validation_empty_paint():
    res = client.post("/api/run", json={"agent": "paint", "question": "", "dry_run": True})
    assert res.status_code == 422


def test_run_validation_empty_gmail():
    res = client.post(
        "/api/run",
        json={"agent": "gmail", "to": "", "subject": "x", "body": "y", "dry_run": True},
    )
    assert res.status_code == 422


def test_logs_api_all():
    res = client.get("/api/logs?lines=50&agent=all")
    assert res.status_code == 200
    data = res.json()
    assert "paint_agent" in data
    assert "gmail_agent" in data


def test_logs_api_gmail_filter():
    res = client.get("/api/logs?lines=50&agent=gmail")
    assert res.status_code == 200
    assert "gmail_agent" in res.json()


def test_run_live_rejects_placeholder_api_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "your_gemini_api_key_here")
    res = client.post(
        "/api/run",
        json={"agent": "paint", "question": "Test?", "dry_run": False},
    )
    assert res.status_code == 400
    assert "GEMINI_API_KEY" in res.json()["detail"]


def test_run_gmail_live_requires_config(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyValidLookingKey123")
    monkeypatch.setenv("GMAIL_MCP_REPO", "/tmp/fake-gmail-mcp")
    monkeypatch.setenv("GMAIL_CREDS_FILE", "/tmp/fake-creds.json")
    monkeypatch.setenv("GMAIL_TOKEN_FILE", "/tmp/fake-token.json")
    res = client.post(
        "/api/run",
        json={
            "agent": "gmail",
            "to": "a@b.com",
            "subject": "S",
            "body": "B",
            "dry_run": False,
        },
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert any(
        phrase in detail
        for phrase in ("OAuth token", "Gmail", "GMAIL_MCP_REPO", "client_creds")
    )


def test_gmail_setup_status_detects_empty_token(tmp_path, monkeypatch):
    from app import gmail_setup_status

    creds = tmp_path / "client_creds.json"
    token = tmp_path / "app_tokens.json"
    creds.write_text(
        '{"installed":{"client_id":"x","client_secret":"y","redirect_uris":["http://localhost"]}}',
        encoding="utf-8",
    )
    token.write_text("", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setenv("GMAIL_MCP_REPO", str(repo))
    monkeypatch.setenv("GMAIL_CREDS_FILE", str(creds))
    monkeypatch.setenv("GMAIL_TOKEN_FILE", str(token))

    status = gmail_setup_status()
    assert status["creds_ok"] is True
    assert status["token_ok"] is False
    assert status["ready"] is False
    assert "OAuth token" in status["message"]


def test_agents_api_includes_gmail_setup():
    res = client.get("/api/agents")
    assert res.status_code == 200
    data = res.json()
    assert "gmail_setup" in data
    assert "ready" in data["gmail_setup"]
