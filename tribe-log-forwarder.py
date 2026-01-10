import os
import time
import json
import re
import ftplib
import hashlib
from collections import deque
from typing import Deque, List, Tuple, Optional

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

# How many webhook messages max to send per poll (prevents flooding/rate limits)
MAX_SEND_PER_POLL = int(os.getenv("MAX_SEND_PER_POLL", "10"))

# If the first run sees a huge backlog, skip it and start "live" from the end
SKIP_BACKLOG_ON_FIRST_RUN = os.getenv("SKIP_BACKLOG_ON_FIRST_RUN", "true").lower() in ("1", "true", "yes")

STATE_FILE = "cursor.json"
DEDUP_MAX = int(os.getenv("DEDUP_MAX", "3000"))  # keep a bigger dedupe window

# Optional small delay between sends (helps avoid 429)
SEND_SPACING_SECONDS = float(os.getenv("SEND_SPACING_SECONDS", "0.2"))

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
    # claiming / unclaiming
    if "claimed" in lower or "unclaimed" in lower or "claiming" in lower:
        return 0x9B59B6  # purple
    # taming
    if "tamed" in lower or "taming" in lower:
        return 0x2ECC71  # green
    # deaths
    if "was killed" in lower or "killed" in lower or "died" in lower or "starved to death" in lower:
        return 0xE74C3C  # red
    # demolished
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

        # Success
        if r.status_code in (200, 204):
            return

        # Rate limited: wait and retry
        if r.status_code == 429:
            try:
                data = r.json()
                retry_after = float(data.get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            time.sleep(max(0.05, retry_after))
            continue

        # Other errors
        raise RuntimeError(f"Discord webhook error {r.status_code}: {r.text[:500]}")

# ============================================================
# STATE (offset + dedupe + pending queue)
# ============================================================

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"offset": 0, "sent": [], "path": None, "initialized": False, "queue": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        return {
            "offset": int(s.get("offset", 0)),
            "sent": s.get("sent", []) if isinstance(s.get("sent", []), list) else [],
            "path": s.get("path", None),
            "initialized": bool(s.get("initialized", False)),
            "queue": s.get("queue", []) if isinstance(s.get("queue", []), list) else [],
        }
    except Exception:
        return {"offset": 0, "sent": [], "path": None, "initialized": False, "queue": []}

def save_state(offset: int, sent_hashes: Deque[str], resolved_path: str, initialized: bool, queue: Deque[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "offset": int(offset),
                "sent": list(sent_hashes),
                "path": resolved_path,
                "initialized": bool(initialized),
                "queue": list(queue),
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
# MAIN
# ============================================================

def main():
    print("Starting Container")
    print(f"Polling every {POLL_INTERVAL:.1f} seconds")
    print(f"Filtering: {TARGET_TRIBE}")

    state = load_state()
    offset = int(state["offset"])
    initialized = bool(state.get("initialized", False))

    sent_hashes: Deque[str] = deque(state["sent"], maxlen=DEDUP_MAX)
    sent_set = set(sent_hashes)

    # Queue stores *lines* waiting to be sent (after filtering)
    queue: Deque[str] = deque(state.get("queue", []), maxlen=10000)

    # Resolve log path
    ftp = ftp_connect()
    try:
        resolved_path = resolve_log_path(ftp, FTP_LOG_PATH)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    # If target log changed, reset cursor + queue (avoid replays)
    if state.get("path") and state["path"] != resolved_path:
        print(f"Log target changed: {state['path']} -> {resolved_path} (resetting cursor + queue)")
        offset = 0
        queue.clear()

    print(f"Reading: {resolved_path}")

    while True:
        try:
            # 1) Pull new bytes/lines
            new_offset, lines = fetch_new_lines(resolved_path, offset)

            # First run backlog handling
            if not initialized and SKIP_BACKLOG_ON_FIRST_RUN:
                # Jump to end without sending old stuff
                offset = new_offset
                initialized = True
                save_state(offset, sent_hashes, resolved_path, initialized, queue)
                print("First run: skipped backlog and started live from the end.")
                time.sleep(POLL_INTERVAL)
                continue

            # 2) Filter and enqueue new tribe lines, dedupe by line hash
            for line in lines:
                if TARGET_TRIBE.lower() not in line.lower():
                    continue
                h = hash_line(line)
                if h in sent_set:
                    continue
                queue.append(line)

            # update offset now (so we don't re-read the same bytes)
            offset = new_offset

            # 3) Send from queue in a controlled way
            sent_count = 0
            while queue and sent_count < MAX_SEND_PER_POLL:
                line = queue.popleft()
                h = hash_line(line)
                if h in sent_set:
                    continue

                send_to_discord(format_payload(line))
                sent_hashes.append(h)
                sent_set.add(h)

                sent_count += 1
                if SEND_SPACING_SECONDS > 0:
                    time.sleep(SEND_SPACING_SECONDS)

            if lines:
                print(f"Read {len(lines)} new lines")
            if sent_count:
                print(f"Sent {sent_count} messages to Discord (queue remaining: {len(queue)})")

            initialized = True
            save_state(offset, sent_hashes, resolved_path, initialized, queue)

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()