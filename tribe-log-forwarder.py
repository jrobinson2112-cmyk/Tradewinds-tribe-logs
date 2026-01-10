import os
import time
import ftplib
import hashlib
import json
import logging
import re
from typing import Dict, List, Optional, Tuple

import requests

# ============================================================
# CONFIG (ENV VARS)
# ============================================================

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Monitor ALL files in this directory
FTP_LOGS_DIR = os.getenv("FTP_LOGS_DIR", "arksa/ShooterGame/Saved/Logs")

# Tribe filter (must match log text)
TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie")

# Polling
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "10"))  # seconds
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "10"))

# State persistence
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# Discord safety
SEND_MIN_DELAY = float(os.getenv("SEND_MIN_DELAY", "0.35"))

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ============================================================
# VALIDATION
# ============================================================

def require_env():
    missing = []
    if not FTP_HOST: missing.append("FTP_HOST")
    if not FTP_USER: missing.append("FTP_USER")
    if not FTP_PASS: missing.append("FTP_PASS")
    if not DISCORD_WEBHOOK_URL: missing.append("DISCORD_WEBHOOK_URL")
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

# ============================================================
# LOG FORMAT PARSING
# ============================================================

LOG_PATTERN = re.compile(
    r"Day\s+(?P<day>\d+),\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2}):\s+"
    r"(?P<rest>.+)"
)

RICH_COLOR_PATTERN = re.compile(r"<RichColor.*?>|</RichColor>")
SPECIES_SUFFIX_PATTERN = re.compile(r"\s*\([^)]*\)")

def format_ark_log(line: str) -> Optional[str]:
    """
    Converts raw ARK log line into:
    Day XXX, HH:MM:SS - Player action
    """
    clean = RICH_COLOR_PATTERN.sub("", line)

    match = LOG_PATTERN.search(clean)
    if not match:
        return None

    day = match.group("day")
    time_str = match.group("time")
    rest = match.group("rest").strip()

    # Remove species info e.g. (Fire Wyvern)
    rest = SPECIES_SUFFIX_PATTERN.sub("", rest)

    return f"Day {day}, {time_str} - {rest}"

# ============================================================
# DISCORD FORMAT HELPERS
# ============================================================

def classify_color(text: str) -> int:
    lower = text.lower()
    if "claimed" in lower:
        return 0x9B59B6  # purple
    if "tamed" in lower:
        return 0x2ECC71  # green
    if "killed" in lower or "died" in lower:
        return 0xE74C3C  # red
    if "demolished" in lower or "destroyed" in lower:
        return 0xF1C40F  # yellow
    return 0x95A5A6  # grey

def format_discord_payload(line: str) -> Optional[dict]:
    formatted = format_ark_log(line)
    if not formatted:
        return None

    return {
        "embeds": [
            {
                "description": formatted,
                "color": classify_color(formatted),
            }
        ]
    }

def send_to_discord(payload: dict) -> Tuple[bool, Optional[float], str]:
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
    except Exception as e:
        return False, None, f"Webhook request failed: {e}"

    if resp.status_code in (200, 204):
        return True, None, "OK"

    if resp.status_code == 429:
        try:
            data = resp.json()
            retry_after = float(data.get("retry_after", 1.0))
        except Exception:
            retry_after = 1.0
        return False, retry_after, "Rate limited"

    return False, None, f"Webhook error {resp.status_code}: {resp.text}"

# ============================================================
# STATE (per-file offsets + dedupe)
# ============================================================

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {
            "offsets": {},
            "seen": [],
            "first_run_done": False,
            "last_heartbeat_ts": 0.0,
        }
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)

def line_fingerprint(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8", errors="ignore")).hexdigest()

# ============================================================
# FTP HELPERS
# ============================================================

def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
    ftp.login(FTP_USER, FTP_PASS)
    return ftp

def ftp_type_binary(ftp: ftplib.FTP):
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass

def ftp_size(ftp: ftplib.FTP, name: str) -> Optional[int]:
    try:
        ftp_type_binary(ftp)
        return ftp.size(name)
    except Exception:
        return None

def ftp_read_from_offset(ftp: ftplib.FTP, name: str, offset: int) -> Tuple[bytes, int]:
    chunks: List[bytes] = []

    def cb(b):
        chunks.append(b)

    ftp_type_binary(ftp)
    if offset > 0:
        ftp.retrbinary(f"RETR {name}", cb, rest=offset)
    else:
        ftp.retrbinary(f"RETR {name}", cb)

    data = b"".join(chunks)
    return data, offset + len(data)

# ============================================================
# MAIN LOOP
# ============================================================

def main():
    require_env()

    logging.info("Starting Container")
    logging.info(f"Polling every {POLL_INTERVAL}s")
    logging.info(f"Filtering: {TARGET_TRIBE}")
    logging.info(f"Logs dir: {FTP_LOGS_DIR}")

    state = load_state()
    sent_since_heartbeat = 0

    while True:
        most_recent_match = None

        try:
            ftp = ftp_connect()
            ftp.cwd(FTP_LOGS_DIR)

            files = ftp.nlst()

            # First run: skip backlog
            if not state["first_run_done"]:
                for f in files:
                    size = ftp_size(ftp, f)
                    if size is not None:
                        state["offsets"][f] = size
                state["first_run_done"] = True
                save_state(state)
                logging.info("First run: skipped backlog")
                ftp.quit()
                time.sleep(POLL_INTERVAL)
                continue

            for f in files:
                size = ftp_size(ftp, f)
                if size is None:
                    continue

                offset = state["offsets"].get(f, 0)
                if size < offset:
                    offset = 0

                if size == offset:
                    continue

                data, new_offset = ftp_read_from_offset(ftp, f, offset)
                state["offsets"][f] = new_offset

                if data:
                    text = data.decode("utf-8", errors="ignore")
                    for line in text.splitlines():
                        if TARGET_TRIBE.lower() in line.lower():
                            most_recent_match = line

            save_state(state)
            ftp.quit()

        except Exception as e:
            logging.error(e)

        # Send most recent match only
        if most_recent_match:
            fp = line_fingerprint(most_recent_match)
            if fp not in state["seen"]:
                payload = format_discord_payload(most_recent_match)
                if payload:
                    ok, retry_after, _ = send_to_discord(payload)
                    if not ok and retry_after:
                        time.sleep(retry_after)
                        send_to_discord(payload)

                    state["seen"].append(fp)
                    state["seen"] = state["seen"][-3000:]
                    save_state(state)
                    sent_since_heartbeat += 1
                    logging.info("Sent log to Discord")

        # Heartbeat
        now = time.time()
        if now - state["last_heartbeat_ts"] >= HEARTBEAT_MINUTES * 60:
            if sent_since_heartbeat == 0:
                hb = {"embeds": [{"description": "No new logs since last check.", "color": 0x95A5A6}]}
                send_to_discord(hb)
            state["last_heartbeat_ts"] = now
            sent_since_heartbeat = 0
            save_state(state)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()