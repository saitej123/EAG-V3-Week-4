# Talk2MCP

**An LLM agent that controls Microsoft Paint through a custom MCP server.**

Paint has no API and no MCP server. This project builds one, then lets **Gemini 3.5 Flash** decide when to call three tools — in order — to open Paint, draw a rectangle, and write your question inside it.

> **Assignment rule:** You ask a question. The **agent** (not you) must call the Paint tools via MCP. Your job is to prompt and wire I/O correctly so the LLM actually does it.

---

## What it does

1. You provide a question, e.g. `"What is the capital of France?"`
2. The **agent** (`talk2mcp.py`) sends that to Gemini with MCP tool definitions
3. Gemini chooses tools one at a time:
  - `open_paint` → launch / prepare canvas
  - `draw_rectangle` → draw a box
  - `add_text_in_paint(text="...")` → write your question inside
4. Each tool call goes over MCP stdio to `paint_mcp_server.py`, which drives Paint
5. Logs record every `LLM chose tool:` line — proof the model did it, not you

**Output:** A real Paint window (or PNG opened in Paint on WSL) with a rectangle and your text.

---

## How it works

```
┌─────────────────┐     question      ┌──────────────────┐
│  You / Web UI   │ ───────────────►  │   talk2mcp.py    │
│  or CLI         │                   │  (Gemini agent)  │
└─────────────────┘                   └────────┬─────────┘
                                               │
                                    Gemini decides tools
                                               │
                                               ▼
                                    ┌──────────────────┐
                                    │  MCP (stdio)     │
                                    │ paint_mcp_server │
                                    └────────┬─────────┘
                                             │
                         ┌───────────────────┼───────────────────┐
                         ▼                   ▼                   ▼
                   open_paint         draw_rectangle    add_text_in_paint
                         │                   │                   │
                         └───────────────────┴───────────────────┘
                                             │
                         ┌───────────────────┴───────────────────┐
                         ▼                                       ▼
              WSL2: Pillow → output/canvas.png          Windows native:
              → cmd.exe opens Paint                       pywinauto clicks
```

### Agent safeguards (why the LLM actually calls tools)


| Mechanism                 | Purpose                                                                        |
| ------------------------- | ------------------------------------------------------------------------------ |
| **System + user prompts** | Tell Gemini the exact 3-tool sequence and to write the question, not answer it |
| `**mode=ANY`**            | Forces tool use until all 3 tools have been called                             |
| **Order validation**      | Rejects wrong order, duplicates, or extra calls after completion               |
| **Nudge messages**        | If Gemini replies in plain text too early, agent pushes it back to tools       |
| **Text fallback**         | If `add_text_in_paint` omits `text`, agent injects your question               |


---

## Project files


| File                        | Role                                                           |
| --------------------------- | -------------------------------------------------------------- |
| `talk2mcp.py`               | **Submit this to GitHub.** Gemini agent + MCP client loop      |
| `paint_mcp_server.py`       | Custom MCP server: config, WSL/Windows backends, 3 Paint tools |
| `talk2gmail.py`             | **Bonus.** Gemini agent → Gmail via external MCP server        |
| `app.py`                    | FastAPI web UI, test cases, live log viewer                    |
| `.env`                      | API key, backend, canvas coords, monitor settings              |
| `output/canvas.png`         | Final drawing (WSL/Linux path)                                 |
| `logs/talk2mcp.log`         | Agent log — scroll this in your YouTube video                  |
| `logs/paint_mcp_server.log` | Paint automation server log                                    |


---

## Quick start

### 1. Install

```bash
cd /mnt/d/Learning/TSAI/EAG-V3/EAG-V3-Week-4
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

On WSL/Linux, Windows-only packages (`pywin32`, `pywinauto`) are skipped automatically.

### 2. Configure

Edit `.env` and set your Gemini key:

```env
GEMINI_API_KEY=your_real_key_here
GEMINI_MODEL=gemini-3.5-flash
DRAW_BACKEND=auto
```

### 3. Verify backend

```bash
python paint_mcp_server.py --info
```

Expected on WSL2:

```
Detected backend: wsl
WSL: True | os.name: posix
Canvas: 900x600
```

### 4. Run the agent (assignment demo)

```bash
python talk2mcp.py "What is the capital of France?"
```

**Do not** run paint tools yourself — only the LLM should.

### 5. Confirm logs

```bash
grep "LLM chose tool" logs/talk2mcp.log
```

You should see:

```
LLM chose tool: open_paint({})
LLM chose tool: draw_rectangle({})
LLM chose tool: add_text_in_paint({"text": "What is the capital of France?"})
```

---

## Examples

### CLI — live run

```bash
python talk2mcp.py "What is MCP?"
```

Gemini calls all 3 tools → Paint shows a rectangle with `What is MCP?` inside.

### CLI — dry run (no API, no Paint)

Useful to test prompts and logging without spending API credits:

```bash
python talk2mcp.py --dry-run "Test question"
```

Simulates the 3 tool calls locally. Logs still show the tool sequence.

### CLI — interactive

```bash
python talk2mcp.py
# prompts: Question to draw inside Paint:
```

### Web UI

```bash
uvicorn app:app --reload --port 8080
```

Open **[http://localhost:8080](http://localhost:8080)**


| Button                 | What happens                                              |
| ---------------------- | --------------------------------------------------------- |
| **Run Agent on Paint** | Live Gemini + real Paint (needs `GEMINI_API_KEY`)         |
| **Dry Run**            | Simulated tools only — no API, no Paint                   |
| **Test Cases** tab     | Preset questions — click Live or Dry                      |
| **Logs** tab           | Live tail of `talk2mcp.log` + `paint_mcp_server.log`      |
| **Features** tab       | Submission checklist, dual-monitor tips, bonus Gmail note |


### Manual MCP test (server only, no LLM)

Tests that the Paint MCP server works before involving Gemini:

```bash
python -c "
import asyncio, sys, os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    p = StdioServerParameters(
        command=sys.executable,
        args=['paint_mcp_server.py'],
        env={**os.environ, 'DRAW_BACKEND': 'wsl', 'WSL_OPEN_IN_PAINT': 'true'},
    )
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for tool, args in [
                ('open_paint', {}),
                ('draw_rectangle', {}),
                ('add_text_in_paint', {'text': 'What is MCP?'}),
            ]:
                res = await s.call_tool(tool, arguments=args)
                print(res.content[0].text)

asyncio.run(main())
"
```

> This bypasses the agent — use only for debugging the MCP server, not for assignment submission.

---

## Backends

The server picks a backend automatically (`DRAW_BACKEND=auto`).


| Backend       | When used                  | How Paint is controlled                                                               |
| ------------- | -------------------------- | ------------------------------------------------------------------------------------- |
| `**wsl**`     | WSL2 (recommended for you) | Pillow draws on `output/canvas.png`, then WSL opens it in Windows Paint via `cmd.exe` |
| `**windows**` | Native Windows Python      | `pywin32` + `pywinauto` + `pyautogui` — live mouse clicks in Paint                    |
| `**linux**`   | Pure Linux (no Windows)    | Pillow canvas + optional `xdg-open` viewer                                            |


Force a backend in `.env`:

```env
DRAW_BACKEND=wsl       # WSL2
DRAW_BACKEND=windows   # native Windows only
DRAW_BACKEND=linux     # no Paint
```

---

## Configuration (`.env`)

### Required

```env
GEMINI_API_KEY=your_key
GEMINI_MODEL=gemini-3.5-flash
```

### WSL canvas (Pillow)

```env
WSL_OPEN_IN_PAINT=true
CANVAS_WIDTH=900
CANVAS_HEIGHT=600
PAINT_RECT_X1=100
PAINT_RECT_Y1=80
PAINT_RECT_X2=780
PAINT_RECT_Y2=380
PAINT_TEXT_X=120
PAINT_TEXT_Y=180
```

Tune these if the rectangle or text lands in the wrong place.

### Dual monitor (Windows native backend only)

```env
PAINT_MONITOR_INDEX=1    # 0 = primary, 1 = second monitor
PAINT_RECT_X1=320
PAINT_RECT_Y1=280
PAINT_RECT_X2=920
PAINT_RECT_Y2=520
PAINT_TEXT_X=420
PAINT_TEXT_Y=390
PAINT_RECT_TOOL_X=180
PAINT_RECT_TOOL_Y=145
PAINT_TEXT_TOOL_X=250
PAINT_TEXT_TOOL_Y=145
```

> On WSL, `PAINT_MONITOR_INDEX` does not move the Paint window — the PNG opens on Windows’ default display. Use native Windows Python with `DRAW_BACKEND=windows` for true second-monitor control.

---

## Assignment checklist


| Requirement                    | How to show it                                                       |
| ------------------------------ | -------------------------------------------------------------------- |
| Custom MCP server for Paint    | `paint_mcp_server.py` exposes exactly 3 tools                        |
| Agent calls tools (not manual) | `talk2mcp.py` only uses MCP `call_tool`; logs show `LLM chose tool:` |
| Question written in Paint box  | Pass any question; text appears via `add_text_in_paint`              |
| YouTube video                  | Show Paint with rectangle + text, then scroll `logs/talk2mcp.log`    |
| GitHub link                    | Push repo; share link to `talk2mcp.py`                               |


---

## Bonus: Gmail MCP (+2000 pts)

Same idea as Paint — **the LLM sends email via MCP**, you never call Gmail APIs yourself.

**References:**

- [Create a Gmail Agent with MCP (Medium)](https://medium.com/@jason.summer/create-a-gmail-agent-with-model-context-protocol-mcp-061059c07777)
- [jasonsum/gmail-mcp-server (GitHub)](https://github.com/jasonsum/gmail-mcp-server)

### 1. Google Cloud / Gmail API setup

1. [Create a Google Cloud project](https://console.cloud.google.com/projectcreate)
2. [Enable Gmail API](https://console.cloud.google.com/workspace-api/products)
3. [OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent) → **External** → add your email as **Test user**
4. OAuth scope: `https://www.googleapis.com/auth/gmail.modify`
5. [Create OAuth Client ID](https://console.cloud.google.com/apis/credentials/oauthclient) → **Desktop app**
6. Download JSON credentials → save securely (e.g. `~/.google/client_creds.json`)

### 2. Clone Gmail MCP server

```bash
git clone https://github.com/jasonsum/gmail-mcp-server.git
# Requires uv: https://docs.astral.sh/uv/
```

### 3. Configure `.env`

```env
GMAIL_MCP_REPO=/path/to/gmail-mcp-server
GMAIL_CREDS_FILE=/path/to/client_creds.json
GMAIL_TOKEN_FILE=/path/to/app_tokens.json
GMAIL_MCP_RUNNER=uv
```

First live run opens a browser for OAuth; tokens are saved to `GMAIL_TOKEN_FILE`.

### 4. Run the Gmail agent

```bash
# Dry-run (no Gmail, no API cost)
python talk2gmail.py --dry-run --to you@gmail.com --subject "MCP test" --body "Hello from agent"

# Live — LLM calls send-email via MCP
python talk2gmail.py --to you@gmail.com --subject "MCP test" --body "This email was sent by Gemini via MCP"
```

Check logs:

```bash
grep "LLM chose tool" logs/talk2gmail.log
# LLM chose tool: send-email({"recipient_id": "...", "subject": "...", "message": "..."})
```

### Bonus video checklist

- Show the email arrived in Gmail
- Scroll `logs/talk2gmail.log` proving the **LLM** called `send-email` (not you)

The web UI **Features** tab has the same setup steps with links.

---

## Tests

```bash
pytest tests/ -v
```

42 tests cover agent helpers, API endpoints, WSL backend, and assignment compliance (no direct paint imports in `talk2mcp.py`).

---

## Troubleshooting


| Problem                      | What to try                                                           |
| ---------------------------- | --------------------------------------------------------------------- |
| `Set a valid GEMINI_API_KEY` | Put a real key in `.env` (not a placeholder)                          |
| Paint doesn't open from WSL  | Set `WSL_OPEN_IN_PAINT=true`; run `cmd.exe /c echo ok` in WSL         |
| Blank / wrong drawing        | Open `output/canvas.png` — drawing happens there first on WSL         |
| Wrong backend                | `python paint_mcp_server.py --info` → should say `wsl` on WSL2        |
| LLM skips a tool             | Re-run; agent nudges and enforces order. Check logs for `ORDER ERROR` |
| Rectangle/text misaligned    | Adjust `PAINT_RECT_*` and `PAINT_TEXT_*` in `.env`                    |
| Native Windows clicks        | Run Python on Windows (not WSL), set `DRAW_BACKEND=windows`           |
| Agent exits incomplete       | CLI exits with error if not all 3 tools ran — check logs              |


---

## Submission tips

1. Run one successful **live** demo: `python talk2mcp.py "What is the capital of France?"`
2. Record Paint showing the rectangle and your question text
3. Scroll through `logs/talk2mcp.log` (or Web UI **Logs** tab) showing `LLM chose tool:` lines
4. Push to GitHub and submit the link to `**talk2mcp.py`**

Include `paint_mcp_server.py` in the same repo — the agent starts it automatically at runtime.

---

