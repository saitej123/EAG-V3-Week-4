"""
Talk2Gmail — Gemini sends email via external Gmail MCP server.

Includes Gmail OAuth config and one-time setup (no separate scripts/).

Run:
    python talk2gmail.py --to you@gmail.com --subject "MCP test" --body "Hello"
    python talk2gmail.py --dry-run --to you@gmail.com --subject "Test" --body "Simulated"
    python talk2gmail.py --setup-oauth
    python talk2gmail.py --setup-oauth-web
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

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
DEFAULT_SCOPE = "https://www.googleapis.com/auth/gmail.modify"

GCP_LINKS = {
    "enable_gmail_api": "https://console.cloud.google.com/apis/library/gmail.googleapis.com",
    "auth_branding": "https://console.cloud.google.com/auth/branding",
    "auth_audience": "https://console.cloud.google.com/auth/audience",
    "auth_scopes": "https://console.cloud.google.com/auth/scopes",
    "auth_clients": "https://console.cloud.google.com/auth/clients",
    "gmail_quickstart": "https://developers.google.com/workspace/gmail/api/quickstart/python",
}

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


# --- Gmail OAuth config -------------------------------------------------------

def _env(*keys: str) -> str:
    for key in keys:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return ""


def is_wsl() -> bool:
    if os.getenv("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def gmail_repo_path() -> Path | None:
    raw = _env("GMAIL_MCP_REPO")
    return Path(raw).expanduser().resolve() if raw else None


def gmail_creds_path() -> Path | None:
    raw = _env("GMAIL_OAUTH_CREDS_FILE", "GMAIL_CREDS_FILE")
    return Path(raw).expanduser().resolve() if raw else None


def gmail_token_path() -> Path | None:
    raw = _env("GMAIL_OAUTH_TOKEN_FILE", "GMAIL_TOKEN_FILE")
    return Path(raw).expanduser().resolve() if raw else None


def gmail_runner() -> str:
    return _env("GMAIL_MCP_RUNNER") or "uv"


def gmail_scopes() -> list[str]:
    scope = _env("GMAIL_OAUTH_SCOPE") or DEFAULT_SCOPE
    return [scope]


def gmail_oauth_mode() -> str:
    mode = (_env("GMAIL_OAUTH_MODE") or ("manual" if is_wsl() else "auto")).lower()
    if mode not in {"manual", "local", "auto"}:
        return "manual" if is_wsl() else "auto"
    return mode


def gmail_oauth_port() -> int:
    try:
        return int(_env("GMAIL_OAUTH_PORT") or "8090")
    except ValueError:
        return 8090


def _creds_redirect_uris(creds_path: Path | None) -> list[str]:
    data = read_json_file(creds_path)
    if not data:
        return []
    block = data.get("installed") or data.get("web") or {}
    return list(block.get("redirect_uris") or [])


def gmail_oauth_redirect_uri(*, manual: bool) -> str:
    """Redirect URI sent to Google — must match the client + pasted callback URL."""
    override = _env("GMAIL_OAUTH_REDIRECT_URI")
    if override:
        return override
    uris = _creds_redirect_uris(gmail_creds_path())
    port = gmail_oauth_port()
    if manual:
        for candidate in ("http://localhost", "http://127.0.0.1"):
            if candidate in uris:
                return candidate
        return "http://localhost"
    for candidate in (
        f"http://localhost:{port}",
        f"http://127.0.0.1:{port}",
        "http://localhost",
        "http://127.0.0.1",
    ):
        if candidate in uris:
            return candidate
    return f"http://localhost:{port}"


def create_oauth_flow(scopes: list[str], redirect_uri: str):
    from google_auth_oauthlib.flow import InstalledAppFlow

    _enable_localhost_oauth(redirect_uri)
    creds_path = gmail_creds_path()
    if not creds_path:
        raise RuntimeError("GMAIL_OAUTH_CREDS_FILE not set")
    return InstalledAppFlow.from_client_secrets_file(
        str(creds_path),
        scopes,
        redirect_uri=redirect_uri,
    )


def _enable_localhost_oauth(redirect_uri: str) -> None:
    """Allow http://localhost token exchange (required by oauthlib for desktop OAuth)."""
    if redirect_uri.startswith("http://"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


def oauth_authorization_url(flow) -> str:
    from urllib.parse import parse_qs, urlparse

    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    params = parse_qs(urlparse(url).query)
    if not params.get("redirect_uri"):
        raise RuntimeError(
            "OAuth URL missing redirect_uri — set GMAIL_OAUTH_REDIRECT_URI in .env "
            "(e.g. http://localhost for WSL manual flow)"
        )
    return url


def read_json_file(path: Path | None) -> dict[str, Any] | None:
    if not path or not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def validate_creds_file(path: Path | None) -> tuple[bool, str]:
    if not path:
        return False, "Set GMAIL_OAUTH_CREDS_FILE in .env"
    if not path.exists():
        return False, f"OAuth creds file not found: {path}"
    data = read_json_file(path)
    if not data:
        return False, f"Invalid or empty OAuth creds JSON: {path}"
    if data.get("type") == "service_account":
        return False, "Service account JSON detected — use Desktop app OAuth client from Auth platform → Clients"
    if "installed" not in data and "web" not in data:
        return False, "Expected Desktop OAuth JSON with an 'installed' block (Google Auth platform → Clients)"
    return True, "OK"


def validate_token_file(path: Path | None) -> tuple[bool, str]:
    if not path:
        return False, "Set GMAIL_OAUTH_TOKEN_FILE in .env"
    if not path.exists():
        return False, "OAuth token missing"
    data = read_json_file(path)
    if not data:
        return False, "OAuth token invalid"
    if not (data.get("token") or data.get("refresh_token")):
        return False, "OAuth token incomplete"
    return True, "OK"


def oauth_setup_command() -> str:
    cmd = "python talk2gmail.py --setup-oauth"
    if is_wsl() and gmail_oauth_mode() != "manual":
        return f"GMAIL_OAUTH_MODE=manual {cmd}"
    return cmd


def oauth_setup_steps() -> list[dict[str, str]]:
    project = Path(__file__).resolve().parent
    token = gmail_token_path()
    test_user = _env("GMAIL_TEST_USER")
    sign_in = f" Sign in as {test_user}." if test_user else " Sign in with your @gmail.com."
    return [
        {
            "title": "GCP checklist",
            "body": (
                "Gmail API enabled · Auth platform → Audience: External + your Gmail as Test user · "
                "Data Access: add gmail.modify scope · Clients: Desktop app JSON → .google/client_creds.json"
            ),
        },
        {
            "title": "Run in WSL",
            "body": f"cd {project} && source .venv/bin/activate && {oauth_setup_command()}",
        },
        {
            "title": "Authorize on Windows",
            "body": (
                "Open the printed URL in Chrome/Edge on Windows (not WSL browser)."
                + sign_in
                + " Click Allow (Advanced → Go to app if unverified)."
            ),
        },
        {
            "title": "Paste redirect URL",
            "body": (
                "Browser may show 'localhost refused' — that is OK. "
                "Copy the full address bar URL (http://localhost...?code=...) and paste into the WSL terminal."
            ),
        },
        {
            "title": "Done",
            "body": f"Token saves to {token}. Reload the UI — Gmail pill should show ✓.",
        },
    ]


def gmail_setup_status() -> dict[str, Any]:
    repo = gmail_repo_path()
    creds = gmail_creds_path()
    token = gmail_token_path()

    env_ok = bool(repo and creds and token)
    repo_ok = bool(repo and repo.exists())
    creds_ok, creds_msg = validate_creds_file(creds)
    token_ok, token_msg = validate_token_file(token)

    ready = env_ok and repo_ok and creds_ok and token_ok
    oauth_cmd = oauth_setup_command()

    if not env_ok:
        message = (
            "Set GMAIL_MCP_REPO, GMAIL_OAUTH_CREDS_FILE, and GMAIL_OAUTH_TOKEN_FILE in .env"
        )
        next_step = "Edit .env with paths from Google Auth platform setup"
    elif not repo_ok:
        message = f"GMAIL_MCP_REPO not found: {repo}"
        next_step = "Clone gmail-mcp-server and set GMAIL_MCP_REPO"
    elif not creds_ok:
        message = creds_msg
        next_step = (
            "Google Auth platform → Clients → Create client → Desktop app → "
            "download JSON to GMAIL_OAUTH_CREDS_FILE"
        )
    elif not token_ok:
        message = token_msg
        next_step = oauth_cmd
    else:
        message = "Gmail MCP ready"
        next_step = ""

    status: dict[str, Any] = {
        "env_ok": env_ok,
        "repo_ok": repo_ok,
        "creds_ok": creds_ok,
        "token_ok": token_ok,
        "ready": ready,
        "message": message,
        "next_step": next_step,
        "oauth_mode": gmail_oauth_mode(),
        "is_wsl": is_wsl(),
    }
    return status


def is_gmail_configured() -> bool:
    return gmail_setup_status()["ready"]


def require_gmail_live() -> None:
    status = gmail_setup_status()
    if not status["ready"]:
        raise RuntimeError(status["message"] + (f" — {status['next_step']}" if status["next_step"] else ""))


# --- OAuth setup --------------------------------------------------------------

def _remove_bad_token(token_path: Path) -> None:
    data = read_json_file(token_path)
    if token_path.exists() and data is None:
        token_path.unlink()
        print(f"Removed invalid token file: {token_path}")


def oauth_pending_path() -> Path | None:
    creds = gmail_creds_path()
    return creds.parent / "oauth_pending.json" if creds else None


def _save_oauth_pending(flow, redirect_uri: str, auth_url: str) -> None:
    path = oauth_pending_path()
    if not path:
        return
    from urllib.parse import parse_qs, urlparse

    params = parse_qs(urlparse(auth_url).query)
    state = (params.get("state") or [None])[0] or getattr(flow.oauth2session, "_state", None)
    pending = {
        "redirect_uri": redirect_uri,
        "code_verifier": flow.code_verifier,
        "state": state,
    }
    path.write_text(json.dumps(pending, indent=2), encoding="utf-8")


def _load_oauth_pending() -> dict[str, Any] | None:
    return read_json_file(oauth_pending_path())


def _clear_oauth_pending() -> None:
    path = oauth_pending_path()
    if path and path.exists():
        path.unlink()


def create_oauth_flow_from_pending(pending: dict[str, Any], scopes: list[str]):
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_path = gmail_creds_path()
    if not creds_path:
        raise RuntimeError("GMAIL_OAUTH_CREDS_FILE not set")
    redirect_uri = pending.get("redirect_uri") or gmail_oauth_redirect_uri(manual=True)
    _enable_localhost_oauth(redirect_uri)
    return InstalledAppFlow.from_client_secrets_file(
        str(creds_path),
        scopes,
        redirect_uri=redirect_uri,
        state=pending.get("state"),
        code_verifier=pending.get("code_verifier"),
        autogenerate_code_verifier=False,
    )


def _save_oauth_token(creds, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    _clear_oauth_pending()
    print(f"\n✓ Saved token to: {token_path}")


def _print_gcp_hint() -> None:
    print("\nGoogle Auth platform links (2025+):")
    print(f"  Audience (test users): {GCP_LINKS['auth_audience']}")
    print(f"  Data Access (scopes):  {GCP_LINKS['auth_scopes']}")
    print(f"  Clients (Desktop app): {GCP_LINKS['auth_clients']}")


def normalize_redirect_response(response: str, redirect_uri: str) -> str:
    """Accept full URL or just the authorization code from the browser."""
    from urllib.parse import parse_qs, urlparse

    text = response.strip().strip('"').strip("'")
    if not text:
        return text
    if text.startswith("localhost") or text.startswith("127.0.0.1"):
        text = "http://" + text
    if text.startswith("http://") or text.startswith("https://"):
        url = text
    elif text.startswith("code="):
        url = f"{redirect_uri}?{text}"
    elif " " not in text and len(text) >= 20:
        url = f"{redirect_uri}?code={text}"
    else:
        url = text

    if url in (redirect_uri, f"{redirect_uri}/"):
        raise ValueError(
            "Paste the full redirect URL with ?code=... from the browser address bar, "
            f"not just {redirect_uri!r}."
        )

    if url.startswith("http://") or url.startswith("https://"):
        code = parse_qs(urlparse(url).query).get("code")
        if not code or not code[0].strip():
            raise ValueError(
                "Redirect URL is missing ?code=... — complete sign-in in Chrome first, "
                f"then copy the full address bar (e.g. {redirect_uri}/?code=4/0A...)."
            )
    return url


def _manual_oauth_flow(creds_path: Path, token_path: Path, scopes: list[str]) -> None:
    redirect_uri = gmail_oauth_redirect_uri(manual=True)
    flow = create_oauth_flow(scopes, redirect_uri)
    auth_url = oauth_authorization_url(flow)
    url_file = creds_path.parent / "oauth_auth_url.txt"
    url_file.write_text(auth_url + "\n", encoding="utf-8")
    _save_oauth_pending(flow, redirect_uri, auth_url)
    pending_path = oauth_pending_path()

    print("\n=== Manual OAuth (recommended on WSL) ===\n")
    print(f"Redirect URI: {redirect_uri}")
    print(f"Auth URL saved to: {url_file}")
    if pending_path:
        print(f"OAuth session saved to: {pending_path}")
        print("  (Required if you paste the code in a separate --oauth-code command.)")
    print("  (Open that file in Windows to copy the full URL — terminal may wrap lines.)")
    print("\n1. Open the auth URL in Chrome/Edge on Windows:\n")
    print(auth_url)
    print("\n2. Sign in with your @gmail.com (must be a Test user in Auth platform → Audience).")
    print("3. Click Allow (Advanced → Go to app if unverified).")
    print("4. Browser shows 'This site can't be reached' / ERR_CONNECTION_CLOSED — THAT IS OK.")
    print("   Do NOT click Reload. Copy the FULL address bar (includes ?code=...).")
    print(f"5. Example: {redirect_uri}/?state=...&code=4/0A...")
    print("   Or paste only the code after code=")
    print("\nPaste redirect URL or code here and press Enter:\n")

    try:
        response = normalize_redirect_response(input("Redirect URL: ").strip(), redirect_uri)
    except ValueError as exc:
        sys.exit(f"\n✗ {exc}")
    if not response:
        sys.exit("No URL pasted — cancelled.")

    try:
        flow.fetch_token(authorization_response=response)
    except Exception as exc:
        err = str(exc)
        print(f"\n✗ Token exchange failed: {err}")
        if "insecure_transport" in err.lower():
            print("Tip: re-run setup — localhost HTTP transport is now enabled automatically.")
        if "redirect_uri" in err.lower():
            print(f"\nTip: redirect URI must be {redirect_uri!r} — paste the full URL from the bar.")
        if "invalid_grant" in err.lower():
            print("Tip: auth codes expire in ~60s and work only once.")
            print("  Run setup-oauth again, open the NEW URL, paste immediately.")
            print("  Or: python talk2gmail.py --oauth-code \"<paste right after Allow>\"")
        print("\nAlso verify in Google Cloud Console (Auth platform):")
        print(f"  Audience (test users): {GCP_LINKS['auth_audience']}")
        print(f"  Data Access (scope):   {GCP_LINKS['auth_scopes']}")
        print(f"  Clients (Desktop app): {GCP_LINKS['auth_clients']}")
        sys.exit(1)
    _save_oauth_token(flow.credentials, token_path)


def exchange_oauth_code(code_or_url: str) -> None:
    """Exchange a browser redirect URL or raw code for a saved token."""
    creds_path = gmail_creds_path()
    token_path = gmail_token_path()
    if not creds_path or not token_path:
        sys.exit("Set GMAIL_OAUTH_CREDS_FILE and GMAIL_OAUTH_TOKEN_FILE in .env")

    ok, msg = validate_creds_file(creds_path)
    if not ok:
        sys.exit(msg)

    if "..." in code_or_url or "4/0A..." in code_or_url:
        sys.exit(
            "✗ That looks like the example placeholder, not your real redirect URL.\n"
            "Copy the full address bar from Chrome after Allow (starts with localhost/?code=4/0A...)."
        )

    pending = _load_oauth_pending()
    if not pending or not pending.get("code_verifier"):
        sys.exit(
            "✗ No OAuth session found. Run this first:\n"
            "  python talk2gmail.py --setup-oauth\n"
            "Open the URL in Chrome, Allow, then paste the redirect URL at the prompt\n"
            "  OR immediately run: python talk2gmail.py --oauth-code \"<your URL>\""
        )

    redirect_uri = pending.get("redirect_uri") or gmail_oauth_redirect_uri(manual=True)
    flow = create_oauth_flow_from_pending(pending, gmail_scopes())
    try:
        response = normalize_redirect_response(code_or_url, redirect_uri)
    except ValueError as exc:
        sys.exit(f"✗ {exc}")

    from urllib.parse import parse_qs, urlparse

    pasted_state = (parse_qs(urlparse(response).query).get("state") or [None])[0]
    if pending.get("state") and pasted_state and pasted_state != pending["state"]:
        sys.exit(
            "✗ State mismatch — this redirect is from a different setup-oauth run.\n"
            "Run: python talk2gmail.py --setup-oauth  and use the NEW auth URL."
        )

    print(f"Redirect URI: {redirect_uri}")
    print("Using saved OAuth session from oauth_pending.json")
    try:
        flow.fetch_token(authorization_response=response)
    except Exception as exc:
        err = str(exc).lower()
        hint = "Re-run setup-oauth, open the NEW URL in Chrome, paste within 60 seconds."
        if "invalid_grant" in err:
            hint = (
                "Code expired or already used. Run setup-oauth again:\n"
                "  1. python talk2gmail.py --setup-oauth\n"
                "  2. Open the printed URL (not an old oauth_auth_url.txt)\n"
                "  3. Allow → copy address bar → paste immediately"
            )
        sys.exit(f"✗ Token exchange failed: {exc}\n{hint}")
    _save_oauth_token(flow.credentials, token_path)


def gmail_web_redirect_uri(port: int | None = None) -> str:
    p = port or gmail_oauth_port()
    return f"http://localhost:{p}"


def _open_in_windows_browser(url: str) -> None:
    if not is_wsl():
        return
    try:
        subprocess.run(["cmd.exe", "/c", "start", "", url], check=False, capture_output=True)
    except OSError:
        pass


def run_oauth_web_setup(port: int | None = None) -> None:
    """Browser OAuth with a local web server — no manual URL paste (best on WSL)."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
    except ImportError:
        sys.exit("Missing google-auth-oauthlib. Run: pip install google-auth-oauthlib")

    creds_path = gmail_creds_path()
    token_path = gmail_token_path()
    if not creds_path or not token_path:
        sys.exit("Set GMAIL_OAUTH_CREDS_FILE and GMAIL_OAUTH_TOKEN_FILE in .env")

    ok, msg = validate_creds_file(creds_path)
    if not ok:
        _print_gcp_hint()
        sys.exit(msg)

    _remove_bad_token(token_path)
    scopes = gmail_scopes()
    port = port or gmail_oauth_port()
    redirect_uri = gmail_web_redirect_uri(port)
    token_path = gmail_token_path()
    done = threading.Event()
    result: dict[str, Any] = {"error": None}

    class OAuthWebHandler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args) -> None:
            return

        def _reply(self, code: int, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _full_url(self) -> str:
            host = self.headers.get("Host", f"localhost:{port}")
            return f"http://{host}{self.path}"

        def do_GET(self) -> None:
            qs = parse_qs(urlparse(self.path).query)
            if qs.get("code"):
                pending = _load_oauth_pending()
                if not pending:
                    result["error"] = "OAuth session missing — restart: python talk2gmail.py --setup-oauth-web"
                    self._reply(
                        400,
                        "<h1>Session expired</h1><p>Close tab and run "
                        "<code>python talk2gmail.py --setup-oauth-web</code> again.</p>",
                    )
                    done.set()
                    return
                try:
                    flow = create_oauth_flow_from_pending(pending, scopes)
                    flow.fetch_token(authorization_response=self._full_url())
                    assert token_path is not None
                    _save_oauth_token(flow.credentials, token_path)
                    self._reply(
                        200,
                        "<h1>Gmail connected</h1><p>Token saved. You can close this tab "
                        "and run <code>python talk2gmail.py --check-gmail</code>.</p>",
                    )
                except Exception as exc:
                    result["error"] = str(exc)
                    self._reply(500, f"<h1>OAuth failed</h1><pre>{exc}</pre>")
                done.set()
                return

            flow = create_oauth_flow(scopes, redirect_uri)
            auth_url = oauth_authorization_url(flow)
            _save_oauth_pending(flow, redirect_uri, auth_url)
            self.send_response(302)
            self.send_header("Location", auth_url)
            self.end_headers()

    print(f"\n=== Web OAuth (auto capture, port {port}) ===\n")
    print(f"Redirect URI: {redirect_uri}")
    print(f"1. Opening http://localhost:{port}/ in Windows browser...")
    print("2. Sign in → Allow in Google.")
    print("3. Browser returns here automatically — token saves to .google/app_tokens.json")
    print("\nWaiting (Ctrl+C to cancel)...\n")

    server = HTTPServer(("0.0.0.0", port), OAuthWebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _open_in_windows_browser(f"http://localhost:{port}/")

    if not done.wait(timeout=300):
        server.shutdown()
        sys.exit("Timed out after 5 minutes. Run --setup-oauth-web again.")

    server.shutdown()
    thread.join(timeout=5)

    if result["error"]:
        sys.exit(f"✗ {result['error']}")
    if not token_path.exists():
        sys.exit("✗ Token was not saved.")
    print("✓ Gmail OAuth complete.")


def _local_server_oauth_flow(
    creds_path: Path, token_path: Path, scopes: list[str], port: int
) -> None:
    from urllib.parse import urlparse

    redirect_uri = gmail_oauth_redirect_uri(manual=False)
    host = urlparse(redirect_uri).hostname or "localhost"
    flow = create_oauth_flow(scopes, redirect_uri)
    wsl = is_wsl()

    print("\n=== Local server OAuth (auto — no paste) ===\n")
    print(f"Redirect URI: {redirect_uri}")
    print(f"Listening on {host}:{port}")
    if wsl:
        print("\nWSL: keep this terminal open.")
        print("When the URL appears below, open it in **Windows Chrome** (not a saved/old URL).")
        print(f"After Allow, redirect goes to {redirect_uri} — token saves here automatically.\n")
    else:
        print("\nBrowser will open automatically. Complete sign-in and Allow.\n")

    try:
        creds = flow.run_local_server(
            host=host,
            port=port,
            redirect_uri_trailing_slash=False,
            open_browser=not wsl,
            authorization_prompt_message=(
                f"Open this URL in Windows Chrome:\n{{url}}\n\n"
                f"Listening for redirect on {redirect_uri}"
            ),
            success_message="Auth complete. You can close this tab.",
            prompt="consent",
        )
    except Exception as exc:
        err = str(exc)
        if "mismatching_state" in err.lower() or "mismatchingstate" in err.lower():
            print("\n✗ State mismatch — you opened an OLD auth URL from a previous run.")
            print("  Fix: run again and open ONLY the URL printed by THIS terminal.")
            print("  Or use manual mode: GMAIL_OAUTH_MODE=manual in .env")
            print("  Or paste redirect: python talk2gmail.py --oauth-code \"localhost/?code=...\"")
        raise
    _save_oauth_token(creds, token_path)


def run_oauth_setup() -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
    except ImportError:
        sys.exit(
            "Missing packages. Run:\n"
            "  pip install google-auth-oauthlib google-auth google-api-python-client"
        )

    creds_path = gmail_creds_path()
    token_path = gmail_token_path()
    if not creds_path or not token_path:
        sys.exit(
            "Set GMAIL_OAUTH_CREDS_FILE and GMAIL_OAUTH_TOKEN_FILE in .env\n"
            "(legacy aliases GMAIL_CREDS_FILE / GMAIL_TOKEN_FILE also work)"
        )

    ok, msg = validate_creds_file(creds_path)
    if not ok:
        _print_gcp_hint()
        sys.exit(msg + "\n\nDownload Desktop app JSON from Google Auth platform → Clients")

    _remove_bad_token(token_path)

    scopes = gmail_scopes()
    mode = gmail_oauth_mode()
    port = gmail_oauth_port()

    if token_path.exists():
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        existing = Credentials.from_authorized_user_file(str(token_path), scopes)
        if existing and existing.valid:
            print(f"Token already valid: {token_path}")
            return
        if existing and existing.expired and existing.refresh_token:
            existing.refresh(Request())
            _save_oauth_token(existing, token_path)
            print("Refreshed existing token.")
            return

    print(f"Creds: {creds_path}")
    print(f"Token: {token_path}")
    print(f"Scope: {scopes[0]}")
    print(f"Mode:  {mode}" + (" (WSL detected)" if is_wsl() else ""))
    print(f"Redirect URI: {gmail_oauth_redirect_uri(manual=(mode == 'manual'))}")

    if mode == "manual":
        _manual_oauth_flow(creds_path, token_path, scopes)
    elif mode == "local":
        _local_server_oauth_flow(creds_path, token_path, scopes, port)
    else:
        print(f"\nTrying local callback on http://localhost:{port} ...")
        print("Set GMAIL_OAUTH_MODE=manual in .env if this fails on WSL.\n")
        try:
            _local_server_oauth_flow(creds_path, token_path, scopes, port)
        except Exception as exc:
            print(f"\nLocal callback failed: {exc}")
            print("Falling back to manual URL paste...\n")
            _manual_oauth_flow(creds_path, token_path, scopes)

    data = json.loads(token_path.read_text(encoding="utf-8"))
    if not data.get("token") and not data.get("refresh_token"):
        sys.exit("Token incomplete. Retry with GMAIL_OAUTH_MODE=manual")


# --- Gmail agent --------------------------------------------------------------

def build_user_prompt(recipient: str, subject: str, body: str) -> str:
    return (
        "Send this email using the Gmail MCP tool `send-email`:\n\n"
        f"  recipient_id: {recipient}\n"
        f"  subject: {subject}\n"
        f"  message: {body}\n\n"
        "Call `send-email` now with these exact values. Do not reply with text only."
    )


def build_gmail_server_params() -> StdioServerParameters:
    repo_path = gmail_repo_path()
    creds_path = gmail_creds_path()
    token_path = gmail_token_path()
    runner = gmail_runner()

    missing = []
    if not repo_path:
        missing.append("GMAIL_MCP_REPO")
    if not creds_path:
        missing.append("GMAIL_OAUTH_CREDS_FILE")
    if not token_path:
        missing.append("GMAIL_OAUTH_TOKEN_FILE")
    if missing:
        raise RuntimeError(
            "Gmail MCP not configured. Set in .env: "
            + ", ".join(missing)
            + ". See README Gmail MCP section."
        )

    if not repo_path.exists():
        raise RuntimeError(f"GMAIL_MCP_REPO not found: {repo_path}")

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

    require_gmail_live()
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


def print_gmail_setup_status(*, verbose: bool = False) -> int:
    """Print setup status. Exit code: 0=ready, 1=needs oauth, 2=config broken."""
    status = gmail_setup_status()
    repo = gmail_repo_path()
    creds = gmail_creds_path()
    token = gmail_token_path()

    print("\n=== Gmail setup status ===")
    print(f"  ready:     {status['ready']}")
    print(f"  repo:      {'✓' if status['repo_ok'] else '✗'}  {repo or '(not set)'}")
    print(f"  creds:     {'✓' if status['creds_ok'] else '✗'}  {creds or '(not set)'}")
    print(f"  token:     {'✓' if status['token_ok'] else '✗'}  {token or '(not set)'}")

    if status["ready"]:
        print("\nGmail MCP is ready.")
        return 0

    if status["creds_ok"] and not status["token_ok"]:
        print("\nNext (easiest on WSL): python talk2gmail.py --setup-oauth-web")
        print("  Opens browser → Allow → token saves automatically (no paste).")
        print("  Manual fallback: python talk2gmail.py --setup-oauth")
        if verbose:
            print("\nDetailed steps:")
            for i, step in enumerate(oauth_setup_steps(), 1):
                print(f"  {i}. {step['title']}: {step['body']}")
            _print_gcp_hint()
        return 1

    print(f"\nFix: {status['message']}")
    if status.get("next_step"):
        print(f"  {status['next_step']}")
    if verbose:
        _print_gcp_hint()
    return 2


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini agent → Gmail via MCP")
    parser.add_argument("--setup-oauth", action="store_true", help="One-time Gmail OAuth (manual paste)")
    parser.add_argument(
        "--setup-oauth-web",
        action="store_true",
        help="One-time Gmail OAuth via browser + local server (recommended on WSL)",
    )
    parser.add_argument(
        "--oauth-code",
        metavar="URL_OR_CODE",
        help="Exchange browser redirect URL or code for token (skip interactive setup)",
    )
    parser.add_argument("--check-gmail", action="store_true", help="Print Gmail OAuth/MCP setup status")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Extra setup details (with --check-gmail)",
    )
    parser.add_argument("--to", help="Recipient email address")
    parser.add_argument("--subject", help="Email subject")
    parser.add_argument("--body", help="Email body / message")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate send-email without Gmail MCP or Gemini API",
    )
    args = parser.parse_args()

    if args.oauth_code:
        exchange_oauth_code(args.oauth_code)
        return

    if args.check_gmail:
        raise SystemExit(print_gmail_setup_status(verbose=args.verbose))

    if args.setup_oauth_web:
        run_oauth_web_setup()
        return

    if args.setup_oauth:
        run_oauth_setup()
        return

    if not args.to or not args.subject or not args.body:
        parser.error("--to, --subject, and --body are required (or use --setup-oauth / --check-gmail)")

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
