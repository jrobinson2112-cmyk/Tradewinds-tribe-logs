import os
import time
import json
import re
import ftplib
import hashlib
from typing import List, Tuple, Optional

import requests

# ============================================================
# ENV CONFIG
# ============================================================

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Can be either the file path OR the Logs directory
FTP_LOG_PATH = os.getenv("FTP_LOG_PATH", "arksa/ShooterGame/Saved/Logs")

# Filter string (use exactly what appears in logs)
TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie")

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "10"))

# If first run sees a huge backlog, skip it and start live from the end
SKIP_BACKLOG_ON_FIRST_RUN = os.getenv("SKIP_BACKLOG_ON_FIRST_RUN", "true").lower() in ("1", "true", "yes")

STATE_FILE = "cursor.json"

# ============================================================
# VALIDATION
# ============================================================

missing = []
if not FTP_HOST:
    missing.append("FTP_HOST")
if not FTP_USER:
    missing.append("FTP_USER")
if not FTP_PASS:
    missing.append("FTP_PASS")
if not DISCORD_WEBHOOK_URL:
    missing.append("DISCORD_WEBHOOK_URL")

if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

# ============================================================
# DISCORD FORMAT HELPERS
# ============================================================

RICH_TAG_RE = re.compile(r"<[^>]+>")

def clean_ark_text(s: str) -> str:
    s = s.strip()
    s = RICH_TAG_RE.sub("", s)
    return s.strip()

def line_color(text: str) -> int:
    lower = text.lower()
    if "claimed" in lower or "unclaimed" in lower or "claiming" in lower:
        return 0x9B59B6  # purple
    if "tamed" in lower or "taming" in lower:
        return 0x2ECC71  # green
    if "was killed" in lower or "killed" in lower or "died" in lower or "starved to death" in lower:
        return 0xE74C3C  # red
    if "demolished" in lower or "destroyed" in lower:
        return 0xF1C40F  # yellow
    return 0x95A5A6  # default grey

def format_payload(line: str) -> dict:
    text = clean_ark_text(line)
    return {"embeds": [{"description": text[:4096], "color": line_color(text)}]}

def send_to_discord(payload: dict) -> None:
    """
    Sends to Discord webhook and respects rate-limit responses.
    """
    while True:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)

        if r.status_code in (200, 204):
            return

        if r.status_code == 429:
            try:
                data = r.json()
                retry_after = float(data.get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            time.sleep(max(0.05, retry_after))
            continue

        raise RuntimeError(f"Discord webhook error {r.status_code}: {r.text[:500]}")

# ============================================================
# STATE
# ============================================================

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"offset": 0, "path": None, "initialized": False, "last_hash": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        return {
            "offset": int(s.get("offset", 0)),
            "path": s.get("path", None),
            "initialized": bool(s.get("initialized", False)),
            "last_hash": s.get("last_hash", None),
        }
    except Exception:
        return {"offset": 0, "path": None, "initialized": False, "last_hash": None}

def save_state(offset: int, resolved_path: str, initialized: bool, last_hash: Optional[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "offset": int(offset),
                "path": resolved_path,
                "initialized": bool(initialized),
                "last_hash": last_hash,
            },
            f,
        )

def hash_line(line: str) -> str:
    clean = clean_ark_text(line)
    return hashlib.sha256(clean.encode("utf-8", errors="ignore")).hexdigest()

# ============================================================
# FTP HELPERS
# ============================================================

def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.set_pasv(True)
    return ftp

def ftp_type_binary(ftp: ftplib.FTP) -> None:
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass

def ftp_is_directory(ftp: ftplib.FTP, path: str) -> bool:
    current = ftp.pwd()
    try:
        ftp.cwd(path)
        ftp.cwd(current)
        return True
    except Exception:
        try:
            ftp.cwd(current)
        except Exception:
            pass
        return False

def ftp_file_exists(ftp: ftplib.FTP, file_path: str) -> bool:
    ftp_type_binary(ftp)
    try:
        ftp.sendcmd(f"SIZE {file_path}")
        return True
    except Exception:
        return False

def resolve_log_path(ftp: ftplib.FTP, configured: str) -> str:
    path = configured.rstrip("/")
    if ftp_is_directory(ftp, path):
        candidate = f"{path}/ShooterGame.log"
        if ftp_file_exists(ftp, candidate):
            print(f"FTP_LOG_PATH is a directory; using: {candidate}")
            return candidate
        raise RuntimeError(f"FTP_LOG_PATH points to a directory ({path}) but ShooterGame.log was not found inside it.")
    if not ftp_file_exists(ftp, path):
        raise RuntimeError(f"Log file not found at: {path}")
    return path

def ftp_get_size(ftp: ftplib.FTP, file_path: str) -> Optional[int]:
    ftp_type_binary(ftp)
    try:
        resp = ftp.sendcmd(f"SIZE {file_path}")
        return int(resp.split()[-1])
    except Exception:
        return None

def ftp_read_from_offset(ftp: ftplib.FTP, file_path: str, offset: int) -> bytes:
    ftp_type_binary(ftp)
    buf = bytearray()

    def cb(chunk: bytes):
        buf.extend(chunk)

    ftp.retrbinary(f"RETR {file_path}", cb, rest=offset)
    return bytes(buf)

def fetch_new_lines(file_path: str, offset: int) -> Tuple[int, List[str]]:
    ftp = ftp_connect()
    try:
        size = ftp_get_size(ftp, file_path)
        if size is not None and size < offset:
            print(f"Log shrank (rotation?) size={size} < offset={offset}. Resetting offset to 0.")
            offset = 0

        raw = ftp_read_from_offset(ftp, file_path, offset)
        if not raw:
            return offset, []

        new_offset = offset + len(raw)
        text = raw.decode("utf-8", errors="ignore")
        return new_offset, text.splitlines()
    finally:
        try:
            ftp.quit()
        except Exception:
            pass

# ============================================================
# MAIN (ONLY SEND MOST RECENT MATCHING LOG)
# ============================================================

def main():
    print("Starting Container")
    print(f"Polling every {POLL_INTERVAL:.1f} seconds")
    print(f"Filtering: {TARGET_TRIBE} (sending ONLY the most recent matching log)")

    state = load_state()
    offset = int(state["offset"])
    initialized = bool(state.get("initialized", False))
    last_hash = state.get("last_hash", None)

    ftp = ftp_connect()
    try:
        resolved_path = resolve_log_path(ftp, FTP_LOG_PATH)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    if state.get("path") and state["path"] != resolved_path:
        print(f"Log target changed: {state['path']} -> {resolved_path} (resetting cursor)")
        offset = 0
        last_hash = None
        initialized = False

    print(f"Reading: {resolved_path}")

    while True:
        try:
            new_offset, lines = fetch_new_lines(resolved_path, offset)

            # First run: skip backlog (start live)
            if not initialized and SKIP_BACKLOG_ON_FIRST_RUN:
                offset = new_offset
                initialized = True
                save_state(offset, resolved_path, initialized, last_hash)
                print("First run: skipped backlog and started live from the end.")
                time.sleep(POLL_INTERVAL)
                continue

            # Always advance offset so we don't re-read bytes
            offset = new_offset

            # Find the most recent matching line (from the NEW chunk only)
            most_recent = None
            for line in reversed(lines):
                if TARGET_TRIBE.lower() in line.lower():
                    most_recent = line
                    break

            if most_recent:
                h = hash_line(most_recent)
                if h != last_hash:
                    send_to_discord(format_payload(most_recent))
                    last_hash = h
                    print("Sent 1 message to Discord (most recent match)")
                else:
                    print("Most recent match already sent (deduped)")
            else:
                if lines:
                    print(f"Read {len(lines)} new lines (no matches)")

            initialized = True
            save_state(offset, resolved_path, initialized, last_hash)

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()