"""
FastAPI web UI for Talk2MCP — Paint + Gmail agents.

Run:
    uvicorn app:app --reload --port 8080
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field, model_validator

from talk2gmail import run_gmail_agent
from talk2mcp import LOG_DIR, is_valid_api_key, run_agent

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"

PAINT_TEST_CASES = [
    {
        "id": "capital",
        "title": "Geography",
        "question": "What is the capital of France?",
        "hint": "Classic demo question for Paint text box.",
    },
    {
        "id": "mcp",
        "title": "Concept",
        "question": "What is MCP?",
        "hint": "Explains Model Context Protocol inside the rectangle.",
    },
    {
        "id": "speed",
        "title": "Science",
        "question": "What is the speed of light?",
        "hint": "Short science question — fits well in the box.",
    },
    {
        "id": "agent",
        "title": "AI",
        "question": "How does an LLM agent work?",
        "hint": "Good for showing multi-step tool calls in logs.",
    },
    {
        "id": "paint",
        "title": "Assignment",
        "question": "Can Paint be controlled without an API?",
        "hint": "Meta question — perfect for Week 4 submission video.",
    },
]

GMAIL_TEST_CASES = [
    {
        "id": "mcp-intro",
        "title": "Intro",
        "to": "",
        "subject": "What is MCP?",
        "body": "This email was composed and sent by a Gemini LLM agent using Gmail MCP — not manually.",
        "hint": "Enter your email in To, then run Live.",
    },
    {
        "id": "assignment",
        "title": "Assignment",
        "to": "",
        "subject": "Talk2MCP Week 4 complete",
        "body": "Paint was controlled without an API. This email proves the same MCP pattern works for Gmail.",
        "hint": "Show inbox + talk2gmail.log in your demo.",
    },
    {
        "id": "short",
        "title": "Quick test",
        "to": "",
        "subject": "MCP agent test",
        "body": "Hello from Talk2Gmail — LLM chose send-email via MCP.",
        "hint": "Use Dry Run first to verify UI transcript.",
    },
]

AGENT_META = {
    "paint": {
        "name": "Paint MCP",
        "description": "Draw a rectangle in Paint and write your question inside.",
        "tools": ["open_paint", "draw_rectangle", "add_text_in_paint"],
        "log_file": "talk2mcp.log",
        "cli": 'python talk2mcp.py "Your question"',
    },
    "gmail": {
        "name": "Gmail MCP",
        "description": "LLM sends email via external Gmail MCP server.",
        "tools": ["send-email"],
        "log_file": "talk2gmail.log",
        "cli": 'python talk2gmail.py --to you@email.com --subject "Hi" --body "..."',
    },
}

app = FastAPI(title="Talk2MCP", version="2.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

_state: dict = {
    "running": False,
    "running_agent": None,
    "last_agent": "paint",
    "last_label": "",
    "last_result": None,
    "last_error": None,
    "run_history": [],
    "history_seq": 0,
}


class RunRequest(BaseModel):
    agent: Literal["paint", "gmail"] = "paint"
    dry_run: bool = False
    question: str | None = Field(None, max_length=500)
    to: str | None = Field(None, max_length=320)
    subject: str | None = Field(None, max_length=200)
    body: str | None = Field(None, max_length=5000)

    @model_validator(mode="after")
    def validate_agent_fields(self) -> RunRequest:
        if self.agent == "paint":
            if not self.question or not self.question.strip():
                raise ValueError("question is required for the Paint agent")
        else:
            missing = [
                name
                for name, val in [
                    ("to", self.to),
                    ("subject", self.subject),
                    ("body", self.body),
                ]
                if not val or not str(val).strip()
            ]
            if missing:
                raise ValueError(
                    f"Gmail agent requires: {', '.join(missing)}"
                )
        return self


class RunResponse(BaseModel):
    status: str
    message: str
    agent: str


def is_gmail_configured() -> bool:
    return all(
        os.getenv(key, "").strip()
        for key in ("GMAIL_MCP_REPO", "GMAIL_CREDS_FILE", "GMAIL_TOKEN_FILE")
    )


def _api_key_valid() -> bool:
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    return is_valid_api_key(key)


def _run_label(agent: str, payload: dict) -> str:
    if agent == "paint":
        return payload.get("question", "")[:80]
    return f"{payload.get('to', '')} — {payload.get('subject', '')}"[:80]


def _append_history(
    agent: str,
    label: str,
    dry_run: bool,
    success: bool,
    detail: str,
) -> None:
    _state["history_seq"] += 1
    entry = {
        "id": _state["history_seq"],
        "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "agent": agent,
        "label": label[:80] + ("…" if len(label) > 80 else ""),
        "mode": "dry-run" if dry_run else "live",
        "success": success,
        "detail": detail[:120],
    }
    _state["run_history"].insert(0, entry)
    _state["run_history"] = _state["run_history"][:30]


async def _run_paint_job(question: str, dry_run: bool) -> None:
    _state["running"] = True
    _state["running_agent"] = "paint"
    _state["last_agent"] = "paint"
    _state["last_label"] = question
    _state["last_error"] = None
    try:
        result = await run_agent(question, dry_run=dry_run)
        result["agent"] = "paint"
        _state["last_result"] = result
        tools = ", ".join(result.get("tools_called") or [])
        ok = result.get("complete", True)
        if not ok:
            _state["last_error"] = (
                "Paint agent finished without all 3 tools. "
                f"Called: {tools or '(none)'}"
            )
        _append_history(
            "paint",
            question,
            dry_run,
            ok,
            f"Tools: {tools or 'simulated'}" if not dry_run else "Simulated 3 tool calls",
        )
        logger.info("Paint job done question={!r} dry_run={}", question, dry_run)
    except Exception as exc:
        _state["last_error"] = str(exc)
        _state["last_result"] = None
        _append_history("paint", question, dry_run, False, str(exc))
        logger.exception("Paint job failed")
    finally:
        _state["running"] = False
        _state["running_agent"] = None


async def _run_gmail_job(to: str, subject: str, body: str, dry_run: bool) -> None:
    _state["running"] = True
    _state["running_agent"] = "gmail"
    _state["last_agent"] = "gmail"
    _state["last_label"] = f"{to} — {subject}"
    _state["last_error"] = None
    try:
        result = await run_gmail_agent(to, subject, body, dry_run=dry_run)
        result["agent"] = "gmail"
        _state["last_result"] = result
        tools = ", ".join(result.get("tools_called") or [])
        ok = result.get("complete", True)
        if not ok:
            _state["last_error"] = (
                "Gmail agent did not call send-email. "
                f"Called: {tools or '(none)'}"
            )
        _append_history(
            "gmail",
            _state["last_label"],
            dry_run,
            ok,
            f"Tools: {tools or 'simulated'}" if not dry_run else "Simulated send-email",
        )
        logger.info("Gmail job done to={!r} dry_run={}", to, dry_run)
    except Exception as exc:
        _state["last_error"] = str(exc)
        _state["last_result"] = None
        _append_history("gmail", _state["last_label"], dry_run, False, str(exc))
        logger.exception("Gmail job failed")
    finally:
        _state["running"] = False
        _state["running_agent"] = None


def _tail_log(path: Path, lines: int = 250) -> str:
    if not path.exists():
        return f"(log not found: {path})"
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "talk2mcp", "version": "2.0.0"}


@app.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.get("/api/agents")
async def agents() -> JSONResponse:
    items = []
    for agent_id, meta in AGENT_META.items():
        configured = True if agent_id == "paint" else is_gmail_configured()
        items.append(
            {
                "id": agent_id,
                "name": meta["name"],
                "description": meta["description"],
                "tools": meta["tools"],
                "log_file": meta["log_file"],
                "cli": meta["cli"],
                "configured": configured,
            }
        )
    return JSONResponse(
        {
            "agents": items,
            "api_key_valid": _api_key_valid(),
            "model": os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
        }
    )


@app.get("/api/test-cases")
async def test_cases(agent: str | None = None) -> JSONResponse:
    if agent == "paint":
        return JSONResponse({"agent": "paint", "cases": PAINT_TEST_CASES})
    if agent == "gmail":
        return JSONResponse({"agent": "gmail", "cases": GMAIL_TEST_CASES})
    return JSONResponse(
        {
            "paint": PAINT_TEST_CASES,
            "gmail": GMAIL_TEST_CASES,
            "cases": PAINT_TEST_CASES,
        }
    )


@app.get("/api/logs")
async def logs(lines: int = 250, agent: str = "all") -> JSONResponse:
    line_count = max(50, min(lines, 1000))
    paint_agent = LOG_DIR / "talk2mcp.log"
    paint_server = LOG_DIR / "paint_mcp_server.log"
    gmail_agent = LOG_DIR / "talk2gmail.log"

    payload: dict[str, str] = {}
    if agent in {"all", "paint"}:
        payload["paint_agent"] = _tail_log(paint_agent, line_count)
        payload["paint_server"] = _tail_log(paint_server, line_count)
        payload["agent"] = payload["paint_agent"]
        payload["paint"] = payload["paint_server"]
    if agent in {"all", "gmail"}:
        payload["gmail_agent"] = _tail_log(gmail_agent, line_count)

    payload["paths"] = {
        "paint_agent": str(paint_agent),
        "paint_server": str(paint_server),
        "gmail_agent": str(gmail_agent),
    }
    return JSONResponse(payload)


@app.get("/api/status")
async def status() -> JSONResponse:
    return JSONResponse(
        {
            "running": _state["running"],
            "running_agent": _state["running_agent"],
            "last_agent": _state["last_agent"],
            "last_label": _state["last_label"],
            "last_result": _state["last_result"],
            "last_error": _state["last_error"],
            "run_history": _state["run_history"],
            "api_key_valid": _api_key_valid(),
            "gmail_configured": is_gmail_configured(),
        }
    )


@app.post("/api/run", response_model=RunResponse)
async def run_agent_job(body: RunRequest, background: BackgroundTasks) -> RunResponse:
    if _state["running"]:
        raise HTTPException(
            status_code=409,
            detail=f"Agent already running ({_state['running_agent']}). Wait for it to finish.",
        )

    if not body.dry_run and not _api_key_valid():
        raise HTTPException(
            status_code=400,
            detail="Set a valid GEMINI_API_KEY in .env before a live run.",
        )

    if body.agent == "gmail" and not body.dry_run and not is_gmail_configured():
        raise HTTPException(
            status_code=400,
            detail=(
                "Gmail MCP not configured. Set GMAIL_MCP_REPO, GMAIL_CREDS_FILE, "
                "and GMAIL_TOKEN_FILE in .env (see Features tab)."
            ),
        )

    if body.agent == "paint":
        preview = body.question.strip()
        if len(preview) > 48:
            preview = preview[:45] + "…"
        if body.dry_run:
            msg = (
                f'Paint dry-run started. Simulating 3 MCP tools for: "{preview}". '
                "Check Agent Output for the tool sequence."
            )
        else:
            msg = (
                f'Paint agent started. Gemini will call open_paint → draw_rectangle → '
                f'add_text_in_paint with your text: "{preview}". '
                "Watch Paint open and check the Logs tab for LLM chose tool: lines."
            )
        background.add_task(_run_paint_job, body.question.strip(), body.dry_run)
    else:
        if body.dry_run:
            msg = "Gmail dry-run started — simulated send-email, no Gmail MCP or Gemini."
        else:
            msg = "Gmail agent started — Gemini will call send-email via MCP."
        background.add_task(
            _run_gmail_job,
            body.to.strip(),
            body.subject.strip(),
            body.body.strip(),
            body.dry_run,
        )

    return RunResponse(status="started", message=msg, agent=body.agent)
