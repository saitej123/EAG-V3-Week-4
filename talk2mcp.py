"""
Talk2MCP — Gemini 3.5 Flash agent that drives Paint through MCP tools.

The LLM (not this script) decides when to call:
  open_paint → draw_rectangle → add_text_in_paint

Run:
    python talk2mcp.py "What is the capital of France?"
    python talk2mcp.py   # interactive prompt
    python talk2mcp.py --dry-run "Test question"   # no Gemini / no Paint
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types
from loguru import logger
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
MAX_TURNS = int(os.getenv("AGENT_MAX_TURNS", "12"))
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "90"))
HERE = Path(__file__).parent
LOG_DIR = HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)

REQUIRED_TOOLS = ("open_paint", "draw_rectangle", "add_text_in_paint")

logger.remove()
logger.add(sys.stderr, level="INFO", colorize=True)
logger.add(
    LOG_DIR / "talk2mcp.log",
    rotation="2 MB",
    retention=20,
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {message}",
    enqueue=False,
)

SYSTEM_PROMPT = """You are an MCP agent controlling Microsoft Paint.

Paint has NO native MCP/API — you automate it ONLY through these 3 MCP tools:
  1. open_paint       — launch Paint on the configured monitor (second monitor)
  2. draw_rectangle   — draw a rectangle on the canvas
  3. add_text_in_paint — type text inside that rectangle (parameter: text)

ASSIGNMENT RULES (mandatory):
- Call tools in EXACT order: open_paint → draw_rectangle → add_text_in_paint
- Call ONE tool per step when possible
- Do NOT skip any tool
- Do NOT describe actions in prose instead of calling tools
- Do NOT answer the user's question from memory — only write their text inside Paint
- Use default coordinates for draw_rectangle unless explicit pixels were given
- Pass the user's exact question/phrase as add_text_in_paint(text=...)

After all 3 tools succeed, reply in one short sentence confirming the rectangle and text.
"""


def validate_tool_order(called: set[str], name: str) -> str | None:
    """Return an error message if the LLM calls tools out of assignment order."""
    if all_tools_called(called):
        return (
            "All 3 Paint tools are already done. "
            "Reply with a short confirmation — do not call more tools."
        )
    if name in called:
        expected = next_required_tool(called)
        return (
            f"Tool `{name}` was already called. "
            f"You must call `{expected}` next."
        )
    expected = next_required_tool(called)
    if expected is None:
        return None
    if name != expected:
        return (
            f"Wrong tool order. You must call `{expected}` next, not `{name}`. "
            "Required sequence: open_paint → draw_rectangle → add_text_in_paint."
        )
    return None


def _sanitize_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    clean = deepcopy(schema or {})
    clean.pop("$schema", None)
    clean.setdefault("type", "object")
    clean.setdefault("properties", {})
    return clean


def mcp_tools_to_gemini(tools_result) -> types.Tool:
    declarations: list[types.FunctionDeclaration] = []
    for tool in tools_result.tools:
        declarations.append(
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description or f"MCP tool {tool.name}",
                parameters_json_schema=_sanitize_schema(tool.inputSchema),
            )
        )
    return types.Tool(function_declarations=declarations)


def extract_function_call_args(fc: Any) -> dict[str, Any]:
    raw = getattr(fc, "args", None)
    if raw is None and getattr(fc, "function_call", None) is not None:
        raw = fc.function_call.args
    if not raw:
        return {}
    if hasattr(raw, "items"):
        return {k: v for k, v in dict(raw).items() if v is not None}
    return {}


def is_valid_api_key(key: str | None) -> bool:
    if not key or not key.strip():
        return False
    placeholders = {
        "your_gemini_api_key_here",
        "your_key_here",
        "changeme",
        "xxx",
    }
    return key.strip().lower() not in placeholders


def tool_result_text(result: Any) -> str:
    chunks: list[str] = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            chunks.append(text)
        else:
            chunks.append(str(block))
    return "\n".join(chunks) if chunks else "(empty tool result)"


def build_user_prompt(question: str) -> str:
    return (
        "Draw a rectangle in Microsoft Paint and write this question inside it:\n"
        f'"{question}"\n\n'
        "Call MCP tools in this exact order (do not skip):\n"
        "  1. open_paint\n"
        "  2. draw_rectangle\n"
        f'  3. add_text_in_paint with text="{question}"'
    )


def missing_tools(called: set[str]) -> list[str]:
    return [name for name in REQUIRED_TOOLS if name not in called]


def all_tools_called(called: set[str]) -> bool:
    return not missing_tools(called)


def next_required_tool(called: set[str]) -> str | None:
    for name in REQUIRED_TOOLS:
        if name not in called:
            return name
    return None


def build_tool_nudge(called: set[str]) -> str:
    remaining = missing_tools(called)
    nxt = remaining[0]
    return (
        f"You have not finished. Still required: {', '.join(remaining)}. "
        f"Call `{nxt}` now using the MCP tool — do not reply with plain text yet."
    )


def dry_run_agent(question: str) -> dict[str, Any]:
    """Simulate the LLM tool loop for local testing without Gemini or Paint."""
    transcript: list[dict[str, Any]] = []
    turn = 1
    for name in REQUIRED_TOOLS:
        args: dict[str, Any] = {}
        if name == "add_text_in_paint":
            args = {"text": question}
        transcript.append({"turn": turn, "type": "tool_call", "name": name, "args": args})
        transcript.append(
            {
                "turn": turn,
                "type": "tool_result",
                "name": name,
                "result": f"[dry-run] {name} ok",
            }
        )
        turn += 1
    final = f"[dry-run] Drew rectangle with text: {question!r}"
    transcript.append({"turn": turn, "type": "final", "text": final})
    logger.info("Dry-run completed for question={!r}", question)
    return {
        "question": question,
        "model": "dry-run",
        "final_response": final,
        "transcript": transcript,
        "tools_called": list(REQUIRED_TOOLS),
        "complete": True,
        "dry_run": True,
    }


async def run_agent(question: str, *, dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        return dry_run_agent(question)

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not is_valid_api_key(api_key):
        raise RuntimeError(
            "Set a valid GEMINI_API_KEY in .env (not the placeholder value)."
        )

    client = genai.Client(api_key=api_key)
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(HERE / "paint_mcp_server.py")],
        cwd=str(HERE),
    )

    transcript: list[dict[str, Any]] = []
    final_text = ""
    called_tools: set[str] = set()
    complete = False

    logger.info("Starting Talk2MCP agent | model={} | question={!r}", MODEL, question)

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                gemini_tool = mcp_tools_to_gemini(tools_result)

                tool_names = [t.name for t in tools_result.tools]
                logger.info("Connected to MCP server. Tools: {}", ", ".join(tool_names))

                contents: list[Any] = [
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=build_user_prompt(question))],
                    )
                ]

                for turn in range(1, MAX_TURNS + 1):
                    logger.info("--- LLM turn {} ---", turn)
                    force_tools = not all_tools_called(called_tools)
                    try:
                        response = await asyncio.wait_for(
                            client.aio.models.generate_content(
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
                                            mode="ANY" if force_tools else "AUTO"
                                        )
                                    ),
                                ),
                            ),
                            timeout=GEMINI_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        logger.error(
                            "Gemini API timed out after {}s on turn {}", GEMINI_TIMEOUT, turn
                        )
                        break

                    if not response.candidates:
                        logger.error("No candidates returned from Gemini.")
                        break

                    candidate = response.candidates[0]
                    model_content = candidate.content
                    if model_content:
                        contents.append(model_content)

                    function_calls = response.function_calls or []
                    if function_calls:
                        response_parts: list[types.Part] = []
                        for fc in function_calls:
                            name = fc.name
                            args = extract_function_call_args(fc)
                            if name == "add_text_in_paint" and not str(
                                args.get("text", "")
                            ).strip():
                                args["text"] = question
                                logger.info(
                                    "LLM omitted add_text_in_paint text; using question={!r}",
                                    question,
                                )
                            logger.info("LLM chose tool: {}({})", name, json.dumps(args))
                            transcript.append(
                                {
                                    "turn": turn,
                                    "type": "tool_call",
                                    "name": name,
                                    "args": args,
                                }
                            )

                            order_err = validate_tool_order(called_tools, name)
                            if order_err:
                                logger.warning("Rejected out-of-order tool: {}", order_err)
                                transcript.append(
                                    {
                                        "turn": turn,
                                        "type": "order_error",
                                        "name": name,
                                        "error": order_err,
                                    }
                                )
                                response_parts.append(
                                    types.Part.from_function_response(
                                        name=name,
                                        response={"error": order_err},
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
                                        name=name,
                                        response={"result": result_text},
                                    )
                                )
                            except Exception as exc:
                                err = str(exc)
                                logger.error("Tool {} failed: {}", name, err)
                                transcript.append(
                                    {
                                        "turn": turn,
                                        "type": "tool_error",
                                        "name": name,
                                        "error": err,
                                    }
                                )
                                response_parts.append(
                                    types.Part.from_function_response(
                                        name=name,
                                        response={"error": err},
                                    )
                                )

                        contents.append(types.Content(role="tool", parts=response_parts))
                        if all_tools_called(called_tools):
                            complete = True
                            final_text = f"Drew rectangle in Paint with text: {question!r}"
                            transcript.append(
                                {"turn": turn, "type": "final", "text": final_text}
                            )
                            logger.info(
                                "All 3 Paint tools done — agent finished (no extra LLM turn)."
                            )
                            break
                        continue

                    if not all_tools_called(called_tools):
                        nudge = build_tool_nudge(called_tools)
                        logger.warning("LLM replied with text too early. Nudging: {}", nudge)
                        transcript.append({"turn": turn, "type": "nudge", "text": nudge})
                        contents.append(
                            types.Content(
                                role="user",
                                parts=[types.Part.from_text(text=nudge)],
                            )
                        )
                        continue

                    final_text = (response.text or "").strip()
                    logger.info("LLM final response: {}", final_text or "(empty)")
                    transcript.append({"turn": turn, "type": "final", "text": final_text})
                    break
                else:
                    logger.warning(
                        "Agent stopped after {} turns without a final answer.", MAX_TURNS
                    )

        complete = all_tools_called(called_tools)
        if not complete:
            logger.error(
                "Assignment incomplete — tools called: {}",
                ", ".join(sorted(called_tools)) or "(none)",
            )
    finally:
        await client.aio.aclose()

    return {
        "question": question,
        "model": MODEL,
        "final_response": final_text,
        "transcript": transcript,
        "tools_called": sorted(called_tools),
        "complete": complete,
        "dry_run": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini agent → Paint via MCP")
    parser.add_argument("question", nargs="?", help="Question/text to draw in Paint")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate tool calls without Gemini API or Paint (for testing)",
    )
    args = parser.parse_args()

    question = args.question
    if not question:
        question = input("Question to draw inside Paint: ").strip()
    if not question:
        raise SystemExit("A question is required.")

    result = asyncio.run(run_agent(question, dry_run=args.dry_run))
    print("\n=== Agent finished ===")
    print(result["final_response"] or "(no final text)")
    if result.get("tools_called") is not None:
        print("Tools called:", ", ".join(result["tools_called"]) or "(none)")
    if not result.get("complete", True):
        raise SystemExit(
            "Assignment incomplete — LLM did not call all 3 Paint tools. "
            "Check logs/talk2mcp.log"
        )
    print(f"\nFull log: {LOG_DIR / 'talk2mcp.log'}")


if __name__ == "__main__":
    main()
