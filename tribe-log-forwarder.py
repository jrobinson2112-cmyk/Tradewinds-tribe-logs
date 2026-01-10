"""
ARK Tribe Log Forwarder (FTP -> Discord webhook) with de-dupe (send each log once)

What it does:
- Downloads (or re-downloads) an ARK tribe log file from FTP each cycle
- Reads ONLY new lines since last run (persists a checkpoint to disk)
- Formats messages to resemble ARK tribe log coloring:
    * Claiming: purple
    * Taming: green
    * Deaths: red
    * Demolished: yellow
    * Other: normal
- Sends to a Discord Webhook (recommended) OR (optional) Discord bot token + channel ID

Railway notes:
- Set Replicas = 1 to avoid duplicates from multiple instances.
- Add a start command: `python tribe-log-forwarder.py`

ENV VARS (Webhook mode - simplest):
- DISCORD_WEBHOOK_URL   (required)
- FTP_HOST              (required)
- FTP_USER              (required)
- FTP_PASS              (required)
- FTP_REMOTE_PATH       (required) e.g. /ShooterGame/Saved/Logs/TribeLog.txt
- POLL_SECONDS          (optional, default 10)
- LOCAL_LOG_PATH        (optional, default ./tribe.log)
- CHECKPOINT_PATH       (optional, default ./.checkpoint.json)

Optional (Bot mode - only if you really need channel ID posting):
- DISCORD_BOT_TOKEN
- DISCORD_CHANNEL_ID    e.g. 1449304199270502420
If BOT token is set, it will post using the bot to that channel; otherwise webhook is used.
"""

import os
import re
import json
import time
import ftplib
import hashlib
import requests
from typing import List, Tuple, Optional


# -----------------------------
# Configuration (from env)
# -----------------------------
FTP_HOST = os.getenv("FTP_HOST", "").strip()
FTP_USER = os.getenv("FTP_USER", "").strip()
FTP_PASS = os.getenv("FTP_PASS", "").strip()
FTP_REMOTE_PATH = os.getenv("FTP_REMOTE_PATH", "").strip()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "").strip()

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))
LOCAL_LOG_PATH = os.getenv("LOCAL_LOG_PATH", "./tribe.log")
CHECKPOINT_PATH = os.getenv("CHECKPOINT_PATH", "./.checkpoint.json")

USER_AGENT = "ark-tribe-log-forwarder/1.0"


# -----------------------------
# Safety checks
# -----------------------------
def require_env():
    missing = []
    for k, v in [
        ("FTP_HOST", FTP_HOST),
        ("FTP_USER", FTP_USER),
        ("FTP_PASS", FTP_PASS),
        ("FTP_REMOTE_PATH", FTP_REMOTE_PATH),
    ]:
        if not v:
            missing.append(k)

    # Need either webhook or bot mode
    if not DISCORD_WEBHOOK_URL and not (DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID):
        missing.append("DISCORD_WEBHOOK_URL (or DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID)")

    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )


# -----------------------------
# FTP download
# -----------------------------
def ftp_download(remote_path: str, local_path: str) -> None:
    """
    Downloads remote_path from FTP to local_path (overwrite each time).
    """
    # Ensure local directory exists
    local_dir = os.path.dirname(os.path.abspath(local_path))
    if local_dir and not os.path.exists(local_dir):
        os.makedirs(local_dir, exist_ok=True)

    with ftplib.FTP(FTP_HOST, timeout=30) as ftp:
        ftp.login(FTP_USER, FTP_PASS)

        # If remote_path includes directories, we can either:
        # - use ftp.retrbinary with full path (often works)
        # - OR cwd into directory and retr file
        # We'll try full path first; fallback to cwd.
        try:
            with open(local_path, "wb") as f:
                ftp.retrbinary(f"RETR {remote_path}", f.write)
            return
        except Exception:
            # fallback: split path
            parts = remote_path.replace("\\", "/").split("/")
            filename = parts[-1]
            directory = "/".join(parts[:-1]) if len(parts) > 1 else ""
            if directory:
                ftp.cwd(directory)
            with open(local_path, "wb") as f:
                ftp.retrbinary(f"RETR {filename}", f.write)


# -----------------------------
# Checkpoint logic (dedupe)
# -----------------------------
def load_checkpoint() -> dict:
    if not os.path.exists(CHECKPOINT_PATH):
        return {"last_offset": 0, "tail_hash": ""}
    try:
        with open(CHECKPOINT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "last_offset" not in data:
            data["last_offset"] = 0
        if "tail_hash" not in data:
            data["tail_hash"] = ""
        return data
    except Exception:
        return {"last_offset": 0, "tail_hash": ""}


def save_checkpoint(last_offset: int, tail_hash: str) -> None:
    tmp = CHECKPOINT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_offset": last_offset, "tail_hash": tail_hash}, f)
    os.replace(tmp, CHECKPOINT_PATH)


def read_new_lines(path: str, checkpoint: dict) -> Tuple[List[str], int, str]:
    """
    Returns: (new_lines, new_offset, new_tail_hash)
    Handles:
      - normal append
      - file rewritten (common when you re-download to same local file)
      - file truncated/rotated
    """
    if not os.path.exists(path):
        return [], int(checkpoint.get("last_offset", 0)), checkpoint.get("tail_hash", "")

    last_offset = int(checkpoint.get("last_offset", 0))
    old_tail_hash = (checkpoint.get("tail_hash", "") or "").strip()

    size = os.path.getsize(path)

    # If file shrank, treat as rotated/truncated: start from 0
    if size < last_offset:
        last_offset = 0

    # Compute a small tail fingerprint (last ~4KB)
    with open(path, "rb") as f:
        if size > 4096:
            f.seek(size - 4096)
        tail_bytes = f.read()
    tail_hash = hashlib.sha1(tail_bytes).hexdigest()

    # If file was rewritten and our offset was from a different file, reset
    rewritten = bool(old_tail_hash) and (tail_hash != old_tail_hash) and (last_offset > 0)

    with open(path, "rb") as f:
        if not rewritten:
            f.seek(last_offset)
        else:
            f.seek(0)
        chunk = f.read()
        new_offset = f.tell()

    text = chunk.decode("utf-8", errors="ignore")
    candidate_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # If rewritten, prevent re-sending what was likely already sent by comparing against old tail.
    if rewritten and old_tail_hash:
        old_tail_text = tail_bytes.decode("utf-8", errors="ignore")
        filtered = []
        for ln in candidate_lines:
            if ln not in old_tail_text:
                filtered.append(ln)
        candidate_lines = filtered

    return candidate_lines, new_offset, tail_hash


# -----------------------------
# Discord sending (Webhook or Bot)
# -----------------------------
def discord_send_webhook(content: str) -> None:
    resp = requests.post(
        DISCORD_WEBHOOK_URL,
        json={"content": content},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Webhook send failed: {resp.status_code} {resp.text}")


def discord_send_bot(content: str) -> None:
    """
    Send message to a channel using a bot token.
    """
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    resp = requests.post(
        url,
        json={"content": content},
        headers={
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
            "User-Agent": USER_AGENT,
        },
        timeout=20,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Bot send failed: {resp.status_code} {resp.text}")


def discord_send(content: str) -> None:
    if DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID:
        discord_send_bot(content)
    else:
        discord_send_webhook(content)


# -----------------------------
# ARK-style coloring via Discord markdown
# -----------------------------
def format_ark_style(line: str) -> str:
    """
    Discord doesn't support real colored text in normal messages.
    Best approximation: use code blocks with syntax highlighting.
    We'll use:
      - 'diff' with + for green and - for red
      - 'fix' tends to look yellow/orange
      - 'css' can look somewhat purple/blue (client-dependent)

    This won't be perfect, but it's the closest in plain Discord messages.
    """

    lower = line.lower()

    # Claiming -> purple-ish
    if "claimed" in lower or "claiming" in lower:
        return f"```css\n{line}\n```"

    # Taming -> green
    if "tamed" in lower or "taming" in lower:
        return f"```diff\n+ {line}\n```"

    # Deaths -> red
    if "killed" in lower or "was killed" in lower or "died" in lower or "death" in lower:
        return f"```diff\n- {line}\n```"

    # Demolished -> yellow
    if "demolished" in lower or "destroyed" in lower:
        return f"```fix\n{line}\n```"

    # Other -> normal
    return line


# -----------------------------
# Main loop
# -----------------------------
def main():
    require_env()

    # Railway / container best practice: avoid multiple replicas
    print("Starting ARK Tribe Log Forwarder (dedupe enabled)")
    print(f"FTP host: {FTP_HOST}")
    print(f"Remote log: {FTP_REMOTE_PATH}")
    print(f"Local log: {LOCAL_LOG_PATH}")
    print(f"Poll seconds: {POLL_SECONDS}")
    print("Discord mode:", "BOT" if (DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID) else "WEBHOOK")

    checkpoint = load_checkpoint()

    while True:
        try:
            # 1) Download latest log snapshot from FTP
            ftp_download(FTP_REMOTE_PATH, LOCAL_LOG_PATH)

            # 2) Read only new lines since last time
            new_lines, new_offset, new_tail_hash = read_new_lines(LOCAL_LOG_PATH, checkpoint)

            # 3) Send
            sent_any = False
            for ln in new_lines:
                msg = format_ark_style(ln)
                discord_send(msg)
                sent_any = True
                # small delay to avoid rate limits if a lot of lines appear at once
                time.sleep(0.25)

            # 4) Save checkpoint only after successful sends
            checkpoint = {"last_offset": new_offset, "tail_hash": new_tail_hash}
            save_checkpoint(new_offset, new_tail_hash)

            if sent_any:
                print(f"Sent {len(new_lines)} new log line(s).")

        except Exception as e:
            # Don't crash on transient errors; log and retry
            print("Error:", repr(e))

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()