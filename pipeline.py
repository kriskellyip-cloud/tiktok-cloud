"""
Universal Content Pipeline
Format: BIT_LOC_DESC_ID.mp4
Config-driven. Add new bits to config.json — no code changes needed.
"""

import os
import json
import csv
import re
import requests
from datetime import datetime
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────────────────────

DRIVE_FOLDER_ID  = os.environ["DRIVE_FOLDER_ID"]        # Google Drive folder ID
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]       # Claude caption generation
ZERNIO_KEY       = os.environ["ZERNIO_API_KEY"]          # Unified posting API
ZERNIO_ENDPOINT  = "https://api.zernio.com/v1/publish"   # Zernio unified post endpoint

def resolve_account_ids(accounts: dict) -> list[str]:
    """Swap placeholder names from config.json for real values from environment variables."""
    resolved = []
    for platform, placeholder in accounts.items():
        value = os.environ.get(placeholder)
        if not value:
            print(f"  ⚠  Missing env var: {placeholder} — skipping {platform}")
            continue
        resolved.append(value)
    return resolved
CONFIG_PATH      = Path("config.json")
HISTORY_PATH     = Path("history.csv")
CREDENTIALS_PATH = Path("service_account.json")

FILENAME_PATTERN = re.compile(
    r"^(?P<bit>[A-Z]+)_(?P<loc>[A-Z]+)_(?P<desc>[a-z0-9]+)_(?P<id>\d+)\.mp4$",
    re.IGNORECASE,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_history() -> set:
    """Return set of already-posted unique IDs (bit_id pairs)."""
    if not HISTORY_PATH.exists():
        return set()
    with open(HISTORY_PATH, newline="") as f:
        return {row["uid"] for row in csv.DictReader(f)}


def record_history(uid: str, filename: str, bit: str, platform_response: dict):
    write_header = not HISTORY_PATH.exists()
    with open(HISTORY_PATH, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["uid", "filename", "bit", "posted_at", "response"])
        if write_header:
            writer.writeheader()
        writer.writerow({
            "uid":       uid,
            "filename":  filename,
            "bit":       bit,
            "posted_at": datetime.utcnow().isoformat(),
            "response":  json.dumps(platform_response),
        })


# ── Step 1: Scanner ───────────────────────────────────────────────────────────

def scan_drive_folder(folder_id: str) -> list[dict]:
    """Return list of {id, name, webContentLink} for .mp4 files in the folder."""
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_PATH,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    service = build("drive", "v3", credentials=creds)
    results = service.files().list(
        q=f"'{folder_id}' in parents and mimeType='video/mp4' and trashed=false",
        fields="files(id, name, webContentLink)",
    ).execute()
    return results.get("files", [])


# ── Step 2: Parser ────────────────────────────────────────────────────────────

def parse_filename(filename: str) -> dict | None:
    """Extract bit, loc, desc, id from filename. Returns None if invalid."""
    match = FILENAME_PATTERN.match(filename)
    if not match:
        print(f"  ⚠  Skipping '{filename}' — doesn't match BIT_LOC_DESC_ID.mp4 format")
        return None
    return match.groupdict()


# ── Step 3: Identity Match ────────────────────────────────────────────────────

def get_identity(bit: str, config: dict) -> dict | None:
    key = bit.upper()
    if key not in config:
        print(f"  ⚠  No config entry for bit '{key}' — skipping")
        return None
    return config[key]


# ── Step 4: AI Captioning ─────────────────────────────────────────────────────

def generate_caption(desc: str, loc: str, tone: str, hashtags: str) -> str:
    prompt = (
        f"Write a short, punchy social media caption for a video.\n"
        f"Description: {desc}\n"
        f"Location: {loc}\n"
        f"Tone: {tone}\n"
        f"End with these hashtags: {hashtags}\n"
        f"Caption only. No explanation. Max 3 sentences."
    )
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()


# ── Step 5: Unified Posting via Zernio ───────────────────────────────────────

def post_to_zernio(
    drive_file_id: str,
    caption: str,
    account_ids: list[str],
    filename: str,
) -> dict:
    """
    Zernio accepts a Google Drive file ID, caption, and target account list.
    It handles cross-platform delivery internally.
    """
    payload = {
        "source": {
            "type": "google_drive",
            "file_id": drive_file_id,
        },
        "caption": caption,
        "accounts": account_ids,
        "metadata": {"original_filename": filename},
    }
    resp = requests.post(
        ZERNIO_ENDPOINT,
        headers={
            "Authorization": f"Bearer {ZERNIO_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run_pipeline():
    print("🚀 Pipeline starting…\n")
    config  = load_config()
    history = load_history()
    files   = scan_drive_folder(DRIVE_FOLDER_ID)
    print(f"📂 Found {len(files)} .mp4 file(s) in Drive folder\n")

    for file in files:
        filename = file["name"]
        print(f"── {filename}")

        # Parse
        parts = parse_filename(filename)
        if not parts:
            continue

        bit, loc, desc, uid_num = parts["bit"], parts["loc"], parts["desc"], parts["id"]
        uid = f"{bit.upper()}_{uid_num}"

        # Deduplicate
        if uid in history:
            print(f"  ✓  Already posted (uid={uid}) — skipping\n")
            continue

        # Identity
        identity = get_identity(bit, config)
        if not identity:
            continue

        # Caption
        print(f"  ✍  Generating caption (tone: {identity['tone']})…")
        caption = generate_caption(desc, loc, identity["tone"], identity["hashtags"])
        print(f"  💬  {caption[:80]}…")

        # Post
        print(f"  📤  Posting to accounts: {identity['accounts']}…")
        try:
            response = post_to_zernio(file["id"], caption, identity["accounts"], filename)
            print(f"  ✅  Posted! Zernio job: {response.get('job_id', 'n/a')}")
        except requests.HTTPError as e:
            print(f"  ❌  Post failed: {e}")
            continue

        # Log
        record_history(uid, filename, bit.upper(), response)
        print()

    print("✅ Pipeline complete.")


if __name__ == "__main__":
    run_pipeline()
