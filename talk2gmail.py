"""
Talk2Gmail — Bonus agent: Gemini sends email via external Gmail MCP server.

Uses jasonsum/gmail-mcp-server (see README Bonus section):
  https://github.com/jasonsum/gmail-mcp-server
  https://medium.com/@jason.summer/create-a-gmail-agent-with-model-context-protocol-mcp-061059c07777

The LLM (not this script) must call the MCP tool `send-email`.

Run:
    python talk2gmail.py --to you@gmail.com --subject "MCP test" --body "Hello from Gemini"
    python talk2gmail.py --dry-run --to you@gmail.com --subject "Test" --body "Simulated"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types
from loguru import logger
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from talk2mcp import (
    extract_function_call_args,
    is_valid_api_key,
    mcp_tools_to_gemini,
    tool_result_text,
)

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
MAX_TURNS = int(os.getenv("GMAIL_AGENT_MAX_TURNS", "8"))
HERE = Path(__file__).parent
LOG_DIR = HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)

REQUIRED_TOOL = "send-email"

logger.remove()
logger.add(sys.stderr, level="INFO", colorize=True)
logger.add(
    LOG_DIR / "talk2gmail.log",
    rotation="2 MB",
    retention=10,
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {message}",
)

SYSTEM_PROMPT = """You are an MCP agent that sends email through Gmail.

You MUST use the MCP tool `send-email` — do NOT pretend to send mail in plain text.

Tool: send-email
  - recipient_id (string): recipient email address
  - subject (string): email subject
  - message (string): email body

Rules:
- Call `send-email` exactly once with the recipient, subject, and message provided by the user
- Do NOT skip the tool call
- Do NOT describe sending email without calling the tool
- After the tool succeeds, reply in one short sentence confirming the email was sent
"""


def build_user_prompt(recipient: str, subject: str, body: str) -> str:
    return (
        "Send this email using the Gmail MCP tool `send-email`:\n\n"
        f"  recipient_id: {recipient}\n"
        f"  subject: {subject}\n"
        f"  message: {body}\n\n"
        "Call `send-email` now with these exact values. Do not reply with text only."
    )


def build_gmail_server_params() -> StdioServerParameters:
    repo = os.getenv("GMAIL_MCP_REPO", "").strip()
    creds = os.getenv("GMAIL_CREDS_FILE", "").strip()
    token = os.getenv("GMAIL_TOKEN_FILE", "").strip()
    runner = os.getenv("GMAIL_MCP_RUNNER", "uv").strip()

    missing = [
        name
        for name, val in [
            ("GMAIL_MCP_REPO", repo),
            ("GMAIL_CREDS_FILE", creds),
            ("GMAIL_TOKEN_FILE", token),
        ]
        if not val
    ]
    if missing:
        raise RuntimeError(
            "Gmail MCP not configured. Set in .env: "
            + ", ".join(missing)
            + ". See README Bonus section and Features tab in the web UI."
        )

    repo_path = Path(repo).expanduser().resolve()
    creds_path = Path(creds).expanduser().resolve()
    token_path = Path(token).expanduser().resolve()

    if runner == "uv":
        return StdioServerParameters(
            command="uv",
            args=[
                "--directory",
                str(repo_path),
                "run",
                "gmail",
                "--creds-file-path",
                str(creds_path),
                "--token-path",
                str(token_path),
            ],
        )

    # Fallback: python module path inside cloned repo
    server_py = repo_path / "src" / "gmail" / "server.py"
    if not server_py.exists():
        server_py = repo_path / "server.py"
    return StdioServerParameters(
        command=sys.executable,
        args=[
            str(server_py),
            "--creds-file-path",
            str(creds_path),
            "--token-path",
            str(token_path),
        ],
        cwd=str(repo_path),
    )


def dry_run_gmail(recipient: str, subject: str, body: str) -> dict[str, Any]:
    args = {"recipient_id": recipient, "subject": subject, "message": body}
    transcript = [
        {"turn": 1, "type": "tool_call", "name": REQUIRED_TOOL, "args": args},
        {"turn": 1, "type": "tool_result", "name": REQUIRED_TOOL, "result": "[dry-run] send-email ok"},
        {"turn": 2, "type": "final", "text": f"[dry-run] Email to {recipient!r} simulated."},
    ]
    logger.info("Gmail dry-run completed for recipient={!r}", recipient)
    return {
        "recipient": recipient,
        "subject": subject,
        "body": body,
        "model": "dry-run",
        "final_response": transcript[-1]["text"],
        "transcript": transcript,
        "tools_called": [REQUIRED_TOOL],
        "complete": True,
        "dry_run": True,
    }


async def run_gmail_agent(
    recipient: str,
    subject: str,
    body: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    if dry_run:
        return dry_run_gmail(recipient, subject, body)

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not is_valid_api_key(api_key):
        raise RuntimeError("Set a valid GEMINI_API_KEY in .env.")

    client = genai.Client(api_key=api_key)
    server_params = build_gmail_server_params()

    transcript: list[dict[str, Any]] = []
    final_text = ""
    called_tools: set[str] = set()
    complete = False

    logger.info(
        "Starting Talk2Gmail | model={} | to={!r} | subject={!r}",
        MODEL,
        recipient,
        subject,
    )

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                gemini_tool = mcp_tools_to_gemini(tools_result)
                tool_names = [t.name for t in tools_result.tools]
                logger.info("Gmail MCP tools: {}", ", ".join(tool_names))

                if REQUIRED_TOOL not in tool_names:
                    raise RuntimeError(
                        f"Gmail MCP server missing `{REQUIRED_TOOL}`. Found: {tool_names}"
                    )

                contents: list[Any] = [
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_text(
                                text=build_user_prompt(recipient, subject, body)
                            )
                        ],
                    )
                ]

                for turn in range(1, MAX_TURNS + 1):
                    logger.info("--- LLM turn {} ---", turn)
                    force_tool = REQUIRED_TOOL not in called_tools
                    response = await client.aio.models.generate_content(
                        model=MODEL,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            system_instruction=SYSTEM_PROMPT,
                            temperature=0.1,
                            tools=[gemini_tool],
                            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                                disable=True
                            ),
                            tool_config=types.ToolConfig(
                                function_calling_config=types.FunctionCallingConfig(
                                    mode="ANY" if force_tool else "AUTO"
                                )
                            ),
                        ),
                    )

                    if not response.candidates:
                        break

                    if response.candidates[0].content:
                        contents.append(response.candidates[0].content)

                    function_calls = response.function_calls or []
                    if function_calls:
                        response_parts: list[types.Part] = []
                        for fc in function_calls:
                            name = fc.name
                            args = extract_function_call_args(fc)
                            if name == REQUIRED_TOOL:
                                args.setdefault("recipient_id", recipient)
                                args.setdefault("subject", subject)
                                args.setdefault("message", body)
                            logger.info("LLM chose tool: {}({})", name, json.dumps(args))
                            transcript.append(
                                {"turn": turn, "type": "tool_call", "name": name, "args": args}
                            )

                            if name != REQUIRED_TOOL:
                                err = f"Use `{REQUIRED_TOOL}` only. Wrong tool: `{name}`."
                                transcript.append(
                                    {"turn": turn, "type": "order_error", "name": name, "error": err}
                                )
                                response_parts.append(
                                    types.Part.from_function_response(
                                        name=name, response={"error": err}
                                    )
                                )
                                continue

                            if name in called_tools:
                                err = "`send-email` already called. Reply with confirmation text."
                                response_parts.append(
                                    types.Part.from_function_response(
                                        name=name, response={"error": err}
                                    )
                                )
                                continue

                            try:
                                mcp_result = await session.call_tool(name, arguments=args)
                                result_text = tool_result_text(mcp_result)
                                called_tools.add(name)
                                logger.success("Tool {} → {}", name, result_text)
                                transcript.append(
                                    {
                                        "turn": turn,
                                        "type": "tool_result",
                                        "name": name,
                                        "result": result_text,
                                    }
                                )
                                response_parts.append(
                                    types.Part.from_function_response(
                                        name=name, response={"result": result_text}
                                    )
                                )
                            except Exception as exc:
                                err = str(exc)
                                logger.error("Tool {} failed: {}", name, err)
                                transcript.append(
                                    {"turn": turn, "type": "tool_error", "name": name, "error": err}
                                )
                                response_parts.append(
                                    types.Part.from_function_response(
                                        name=name, response={"error": err}
                                    )
                                )

                        contents.append(types.Content(role="tool", parts=response_parts))
                        continue

                    if REQUIRED_TOOL not in called_tools:
                        nudge = (
                            f"You have not called `{REQUIRED_TOOL}` yet. "
                            "Call it now with recipient_id, subject, and message."
                        )
                        logger.warning("Nudging LLM: {}", nudge)
                        transcript.append({"turn": turn, "type": "nudge", "text": nudge})
                        contents.append(
                            types.Content(
                                role="user",
                                parts=[types.Part.from_text(text=nudge)],
                            )
                        )
                        continue

                    final_text = (response.text or "").strip()
                    transcript.append({"turn": turn, "type": "final", "text": final_text})
                    break

                complete = REQUIRED_TOOL in called_tools
                if not complete:
                    logger.error("Gmail agent incomplete — send-email was not called.")
    finally:
        await client.aio.aclose()

    return {
        "recipient": recipient,
        "subject": subject,
        "body": body,
        "model": MODEL,
        "final_response": final_text,
        "transcript": transcript,
        "tools_called": sorted(called_tools),
        "complete": complete,
        "dry_run": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gemini agent → Gmail via MCP (bonus assignment)"
    )
    parser.add_argument("--to", required=True, help="Recipient email address")
    parser.add_argument("--subject", required=True, help="Email subject")
    parser.add_argument("--body", required=True, help="Email body / message")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate send-email without Gmail MCP or Gemini API",
    )
    args = parser.parse_args()

    result = asyncio.run(
        run_gmail_agent(args.to, args.subject, args.body, dry_run=args.dry_run)
    )
    print("\n=== Gmail agent finished ===")
    print(result["final_response"] or "(no final text)")
    print("Tools called:", ", ".join(result["tools_called"]) or "(none)")
    print(f"\nFull log: {LOG_DIR / 'talk2gmail.log'}")
    if not result.get("complete", True):
        raise SystemExit("Incomplete — LLM did not call send-email. Check talk2gmail.log")


if __name__ == "__main__":
    main()
