#!/usr/bin/env python3
"""
Create .google/app_tokens.json for Gmail MCP (WSL-safe).

Uses Google's official InstalledAppFlow. If localhost callback fails in WSL,
falls back to manual paste of the redirect URL from your browser address bar.

Usage:
    cd /mnt/d/Learning/TSAI/EAG-V3/EAG-V3-Week-4
    source .venv/bin/activate
    pip install google-auth-oauthlib google-auth google-api-python-client
    python scripts/gmail_oauth_setup.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _paths() -> tuple[Path, Path]:
    creds = os.getenv("GMAIL_CREDS_FILE", "").strip()
    token = os.getenv("GMAIL_TOKEN_FILE", "").strip()
    if not creds or not token:
        sys.exit(
            "Set GMAIL_CREDS_FILE and GMAIL_TOKEN_FILE in .env first.\n"
            "Example:\n"
            "  GMAIL_CREDS_FILE=/mnt/d/.../EAG-V3-Week-4/.google/client_creds.json\n"
            "  GMAIL_TOKEN_FILE=/mnt/d/.../EAG-V3-Week-4/.google/app_tokens.json"
        )
    return Path(creds).expanduser().resolve(), Path(token).expanduser().resolve()


def _validate_creds(creds_path: Path) -> None:
    if not creds_path.exists():
        sys.exit(f"Credentials file not found: {creds_path}")
    try:
        data = json.loads(creds_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"Invalid JSON in {creds_path}: {exc}")
    if "installed" not in data and "web" not in data:
        sys.exit(
            f"{creds_path} is not OAuth client credentials.\n"
            "Download OAuth Client ID → Desktop app from Google Cloud Console.\n"
            "Do NOT use a service account JSON file."
        )


def _remove_bad_token(token_path: Path) -> None:
    if not token_path.exists():
        return
    text = token_path.read_text(encoding="utf-8").strip()
    if not text:
        token_path.unlink()
        print(f"Removed empty token file: {token_path}")
        return
    try:
        json.loads(text)
    except json.JSONDecodeError:
        token_path.unlink()
        print(f"Removed invalid token file: {token_path}")


def _save_token(creds, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"\n✓ Saved token to: {token_path}")


def _manual_flow(creds_path: Path, token_path: Path):
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    print("\n=== Manual OAuth (works when WSL localhost fails) ===\n")
    print("1. Open this URL in Chrome/Edge on Windows:\n")
    print(auth_url)
    print("\n2. Sign in and click Allow.")
    print("3. Browser may show 'localhost refused to connect' — that is OK.")
    print("4. Copy the FULL address bar URL (starts with http://localhost:...)")
    print("   It contains code=... in the query string.")
    print("\nPaste the full redirect URL here and press Enter:\n")

    response = input("Redirect URL: ").strip()
    if not response:
        sys.exit("No URL pasted — cancelled.")

    flow.fetch_token(authorization_response=response)
    _save_token(flow.credentials, token_path)


def _local_server_flow(creds_path: Path, token_path: Path):
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    creds = flow.run_local_server(
        host="localhost",
        port=8090,
        open_browser=True,
        authorization_prompt_message="Open this URL in your browser:\n{url}",
        success_message="Auth complete. You can close this tab.",
        prompt="consent",
    )
    _save_token(creds, token_path)


def main() -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
    except ImportError:
        sys.exit(
            "Missing packages. Run:\n"
            "  pip install google-auth-oauthlib google-auth google-api-python-client"
        )

    creds_path, token_path = _paths()
    _validate_creds(creds_path)
    _remove_bad_token(token_path)

    if token_path.exists():
        from google.oauth2.credentials import Credentials

        existing = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if existing and existing.valid:
            print(f"Token already valid: {token_path}")
            return
        if existing and existing.expired and existing.refresh_token:
            from google.auth.transport.requests import Request

            existing.refresh(Request())
            _save_token(existing, token_path)
            print("Refreshed existing token.")
            return

    print(f"Creds: {creds_path}")
    print(f"Token: {token_path}")

    mode = os.getenv("GMAIL_OAUTH_MODE", "auto").strip().lower()
    if mode == "manual":
        _manual_flow(creds_path, token_path)
        return
    if mode == "local":
        _local_server_flow(creds_path, token_path)
        return

    print("\nTrying local browser callback on http://localhost:8090 ...")
    print("(Set GMAIL_OAUTH_MODE=manual if this fails on WSL)\n")
    try:
        _local_server_flow(creds_path, token_path)
    except Exception as exc:
        print(f"\nLocal callback failed: {exc}")
        print("Falling back to manual URL paste...\n")
        _manual_flow(creds_path, token_path)

    # Quick sanity check
    data = json.loads(token_path.read_text(encoding="utf-8"))
    if not data.get("token") and not data.get("refresh_token"):
        sys.exit("Token file created but looks incomplete. Retry with GMAIL_OAUTH_MODE=manual")


if __name__ == "__main__":
    main()
