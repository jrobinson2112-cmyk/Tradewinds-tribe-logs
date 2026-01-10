# tradewinds_bot.py
# ------------------------------------------------------------
# Tradewinds Bot + Tribe Log Forwarder (FTP -> Discord Webhook)
#
# Features added:
#  - Background polling of Nitrado logs (ShooterGame.log + ServerGame*.log)
#  - Filters ONLY "Tribe Valkyrie" lines
#  - Sends ONLY the most recent matching log per check (prevents spam / rate limits)
#  - Dedupes so the same log line is never sent twice
#  - Slash command: /gettribelogs  (forces an immediate check)
#  - Heartbeat every 10 minutes: "No new logs since last"
#
# ENV VARS REQUIRED:
#   DISCORD_BOT_TOKEN
#   DISCORD_WEBHOOK_URL
#   FTP_HOST
#   FTP_USER
#   FTP_PASS
#
# ENV VARS OPTIONAL:
#   FTP_PORT (default 21)
#   FTP_LOGS_DIR (default arksa/ShooterGame/Saved/Logs)
#   TARGET_TRIBE (default "Tribe Valkyrie")
#   POLL_INTERVAL_SECONDS (default 10)
#   HEARTBEAT_MINUTES (default 10)
# ------------------------------------------------------------

import os
import re
import time
import json
import ftplib
import hashlib
import asyncio
from dataclasses import dataclass
from typing import List, Optional, Tuple

import requests
import discord
from discord import app_commands


# ============================================================
# ENV / CONFIG
# ============================================================

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

FTP_LOGS_DIR = os.getenv("FTP_LOGS_DIR", "arksa/ShooterGame/Saved/Logs")
TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie")

POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "10"))
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "10"))

# Post limits (Discord webhooks have rate limits; keep it low)
WEBHOOK_TIMEOUT_SECONDS = 10


def require_env():
    missing = []
    for k, v in [
        ("DISCORD_BOT_TOKEN", DISCORD_BOT_TOKEN),
        ("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL),
        ("FTP_HOST", FTP_HOST),
        ("FTP_USER", FTP_USER),
        ("FTP_PASS", FTP_PASS),
    ]:
        if not v:
            missing.append(k)
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


# ============================================================
# DISCORD WEBHOOK HELPERS (color-coded like ARK tribe logs)
# ============================================================

RICHCOLOR_TAG_RE = re.compile(r"<\/?RichColor[^>]*>", re.IGNORECASE)
ARK_TIMESTAMP_PREFIX_RE = re.compile(r"^\[[0-9\.\-:]+\]\[[0-9]+\]")  # strips [2026...][410] style prefix


def clean_line(text: str) -> str:
    t = text.strip()
    t = RICHCOLOR_TAG_RE.sub("", t)
    t = ARK_TIMESTAMP_PREFIX_RE.sub("", t).strip()
    # Remove stray formatting artifacts
    t = t.replace("!>)", "!").replace("!>)", "!")
    return t.strip()


def embed_payload_for_log_line(line: str) -> dict:
    """
    Returns a Discord webhook payload with embed color based on log type:
      - claiming/claimed: purple
      - taming/tamed: green
      - deaths: red
      - demolished/destroyed: yellow
      - else: grey
    """
    text = clean_line(line)
    lower = text.lower()

    color = 0x95A5A6  # default grey

    if "claimed" in lower or "claiming" in lower:
        color = 0x9B59B6  # purple
    elif "tamed" in lower or "taming" in lower:
        color = 0x2ECC71  # green
    elif "killed" in lower or "died" in lower or "death" in lower:
        color = 0xE74C3C  # red
    elif "demolished" in lower or "destroyed" in lower:
        color = 0xF1C40F  # yellow

    return {
        "embeds": [
            {
                "description": text[:4096],  # Discord embed description limit
                "color": color,
            }
        ]
    }


def send_webhook(payload: dict) -> Tuple[bool, Optional[float], str]:
    """
    Sends payload to Discord webhook.
    Returns: (ok, retry_after_seconds_if_rate_limited, error_text)
    """
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=WEBHOOK_TIMEOUT_SECONDS)
        if r.status_code in (200, 204):
            return True, None, ""
        if r.status_code == 429:
            try:
                data = r.json()
                retry_after = float(data.get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            return False, retry_after, f"Discord webhook rate limited (429): {r.text}"
        return False, None, f"Discord webhook error {r.status_code}: {r.text}"
    except Exception as e:
        return False, None, f"Discord webhook exception: {e}"


async def send_webhook_with_retry(payload: dict, max_retries: int = 5) -> bool:
    """
    Handles Discord webhook rate limiting automatically.
    """
    for _ in range(max_retries):
        ok, retry_after, err = send_webhook(payload)
        if ok:
            return True
        if retry_after is not None:
            await asyncio.sleep(max(0.1, retry_after))
            continue
        # non-rate-limit error
        print(err)
        return False
    return False


# ============================================================
# FTP LOG READER (incremental tail)
# ============================================================

ALLOWED_ACTIVE_LOG_RE = re.compile(r"^(ShooterGame\.log|ServerGame\..*\.log)$", re.IGNORECASE)
EXCLUDE_RE = re.compile(r"(backup|FailedWater|crashstack)", re.IGNORECASE)


def _ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=15)
    ftp.login(FTP_USER, FTP_PASS)
    # Passive mode typically works best on hosting panels
    ftp.set_pasv(True)
    return ftp


def _ftp_listdir(ftp: ftplib.FTP, path: str) -> List[str]:
    """
    Returns list of filenames within 'path'.
    Uses MLSD if available, otherwise NLST.
    """
    names: List[str] = []

    # Prefer MLSD (gives structured entries)
    try:
        for entry in ftp.mlsd(path):
            name, facts = entry
            names.append(name)
        return names
    except Exception:
        pass

    # Fallback to NLST
    try:
        for item in ftp.nlst(path):
            # NLST may return full paths; normalize to filename
            name = item.split("/")[-1]
            if name:
                names.append(name)
    except Exception:
        pass

    return names


def pick_active_log_file(ftp: ftplib.FTP, logs_dir: str) -> Optional[str]:
    """
    Chooses the "active" log file to tail:
      - ShooterGame.log preferred
      - else newest-ish ServerGame.*.log (by name; Nitrado usually rotates with timestamps)
    Excludes backups and unrelated logs.
    Returns full remote path.
    """
    names = _ftp_listdir(ftp, logs_dir)
    if not names:
        return None

    # Filter allowed + exclude noise
    allowed = []
    for n in names:
        if EXCLUDE_RE.search(n):
            continue
        if ALLOWED_ACTIVE_LOG_RE.match(n):
            allowed.append(n)

    if not allowed:
        return None

    # Prefer ShooterGame.log
    for n in allowed:
        if n.lower() == "shootergame.log":
            return f"{logs_dir.rstrip('/')}/{n}"

    # Else pick the "largest-looking/newest name" ServerGame file by sorting
    allowed.sort(reverse=True)
    return f"{logs_dir.rstrip('/')}/{allowed[0]}"


def ftp_get_size(ftp: ftplib.FTP, remote_path: str) -> Optional[int]:
    """
    Returns remote file size in bytes if supported.
    Some servers require TYPE I before SIZE.
    """
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass

    try:
        return ftp.size(remote_path)
    except Exception as e:
        # Common: "550 SIZE not allowed in ASCII mode" or missing file
        print(f"Could not get remote file size: {e}")
        return None


def ftp_read_from_offset(ftp: ftplib.FTP, remote_path: str, offset: int) -> bytes:
    """
    Reads bytes from remote file starting at 'offset' using REST (binary).
    If REST is not supported, falls back to reading the whole file.
    """
    # Ensure binary
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass

    chunks: List[bytes] = []

    # Try REST streaming
    try:
        conn = ftp.transfercmd(f"RETR {remote_path}", rest=offset)
        while True:
            block = conn.recv(8192)
            if not block:
                break
            chunks.append(block)
        conn.close()
        try:
            ftp.voidresp()
        except Exception:
            pass
        return b"".join(chunks)
    except Exception:
        # Fallback: read entire file then slice
        chunks.clear()
        ftp.retrbinary(f"RETR {remote_path}", chunks.append)
        data = b"".join(chunks)
        if offset <= 0:
            return data
        return data[offset:]


def split_new_lines(buffer: bytes) -> List[str]:
    """
    Converts raw bytes to lines safely.
    """
    text = buffer.decode("utf-8", errors="ignore")
    # Normalise line breaks
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    return lines


# ============================================================
# STATE + CHECKER
# ============================================================

@dataclass
class TribeLogState:
    active_log_path: Optional[str] = None
    offset: int = 0
    last_sent_hash: Optional[str] = None
    last_sent_at: float = 0.0
    first_run_skipped_backlog: bool = False


STATE = TribeLogState()


def line_hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8", errors="ignore")).hexdigest()


def is_target_line(line: str) -> bool:
    return TARGET_TRIBE.lower() in line.lower()


async def check_for_new_logs(force: bool = False) -> bool:
    """
    Polls FTP log file for new content.
    Sends ONLY the most recent matching line (Tribe Valkyrie) if new and not duplicate.
    Returns True if it sent something.
    """
    ftp = None
    try:
        ftp = _ftp_connect()

        # Pick/refresh active log file each run (handles rotation)
        active = pick_active_log_file(ftp, FTP_LOGS_DIR)
        if not active:
            print(f"No allowed active logs found in directory: {FTP_LOGS_DIR}")
            return False

        if STATE.active_log_path != active:
            print(f"Active log selected: {active} (resetting cursor)")
            STATE.active_log_path = active
            STATE.offset = 0
            STATE.first_run_skipped_backlog = False

        size = ftp_get_size(ftp, STATE.active_log_path)
        if size is not None and size < STATE.offset:
            # rotation/truncation
            print("Remote log shrank (rotation/truncate). Resetting cursor.")
            STATE.offset = 0
            STATE.first_run_skipped_backlog = False

        # On first run, skip backlog and start live from end (unless forced)
        if not STATE.first_run_skipped_backlog and not force:
            if size is None:
                # If we can't size, read the whole thing once and set offset to len(data)
                data = ftp_read_from_offset(ftp, STATE.active_log_path, 0)
                STATE.offset = len(data)
            else:
                STATE.offset = size
            STATE.first_run_skipped_backlog = True
            print("First run: skipped backlog and started live from the end.")
            return False

        # Read new bytes
        data = ftp_read_from_offset(ftp, STATE.active_log_path, STATE.offset)
        if not data:
            return False

        new_offset = STATE.offset + len(data)
        lines = split_new_lines(data)

        print(f"Read {len(lines)} new lines (offset {STATE.offset}->{new_offset})")
        STATE.offset = new_offset

        # Filter lines and pick ONLY most recent matching entry
        matches = [ln for ln in lines if is_target_line(ln)]
        if not matches:
            return False

        most_recent = matches[-1]
        h = line_hash(most_recent)

        # Dedup (never send the same line twice)
        if STATE.last_sent_hash == h:
            return False

        payload = embed_payload_for_log_line(most_recent)
        ok = await send_webhook_with_retry(payload)
        if ok:
            STATE.last_sent_hash = h
            STATE.last_sent_at = time.time()
            return True
        return False

    except ftplib.error_perm as e:
        print(f"FTP permission/error: {e}")
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False
    finally:
        try:
            if ftp is not None:
                ftp.quit()
        except Exception:
            pass


# ============================================================
# BACKGROUND TASKS
# ============================================================

async def tribe_polling_loop():
    print(f"Polling every {POLL_INTERVAL_SECONDS:.1f} seconds")
    print(f"Filtering: {TARGET_TRIBE} (sending ONLY the most recent matching log)")
    print(f"Logs dir: {FTP_LOGS_DIR}")
    while True:
        try:
            await check_for_new_logs(force=False)
        except Exception as e:
            print(f"Polling loop error: {e}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def heartbeat_loop():
    """
    Every HEARTBEAT_MINUTES minutes, if nothing new has been sent, post a heartbeat message.
    """
    interval = max(1, HEARTBEAT_MINUTES) * 60
    while True:
        try:
            now = time.time()
            # Only start heartbeats after we’ve started “live”
            if STATE.first_run_skipped_backlog:
                if STATE.last_sent_at == 0:
                    # If nothing has ever been sent, still heartbeat
                    payload = {
                        "embeds": [{
                            "description": "No new logs since last.",
                            "color": 0x95A5A6
                        }]
                    }
                    await send_webhook_with_retry(payload)
                else:
                    if (now - STATE.last_sent_at) >= interval:
                        payload = {
                            "embeds": [{
                                "description": "No new logs since last.",
                                "color": 0x95A5A6
                            }]
                        }
                        await send_webhook_with_retry(payload)
        except Exception as e:
            print(f"Heartbeat error: {e}")
        await asyncio.sleep(interval)


# ============================================================
# DISCORD BOT (Tradewinds base + tribe command)
# ============================================================

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@tree.command(name="gettribelogs", description="Check Nitrado logs now and post the latest Valkyrie tribe log (if any).")
async def gettribelogs(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    sent = await check_for_new_logs(force=True)
    if sent:
        await interaction.followup.send("✅ Posted the latest Valkyrie tribe log.", ephemeral=True)
    else:
        await interaction.followup.send("ℹ️ No new Valkyrie logs found.", ephemeral=True)


# ---- OPTIONAL: placeholder so you can paste your existing Tradewinds commands here ----
# Add your existing time bot commands/events/tasks below this line if you want.
# -------------------------------------------------------------------------------------


@client.event
async def on_ready():
    print(f"Logged in as {client.user} (id={client.user.id})")
    try:
        # Sync slash commands globally
        await tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print(f"Slash command sync failed: {e}")

    # Start background tasks once
    if not hasattr(client, "_tribe_tasks_started"):
        client._tribe_tasks_started = True
        client.loop.create_task(tribe_polling_loop())
        client.loop.create_task(heartbeat_loop())
        print("Tribe polling + heartbeat started.")


def main():
    require_env()
    print("Starting Container")
    client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()