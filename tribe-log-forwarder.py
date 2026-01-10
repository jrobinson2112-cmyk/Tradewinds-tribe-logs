import os
import time
import ftplib
import hashlib
import json
import logging
from typing import List, Optional, Tuple

import requests

# ============================================================
# CONFIG (ENV VARS)
# ============================================================

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Always read ONLY this directory + file
FTP_LOGS_DIR = os.getenv("FTP_LOGS_DIR", "arksa/ShooterGame/Saved/Logs")
LOG_FILE = os.getenv("LOG_FILE", "ShooterGame.log")  # ONLY this file

# Tribe filter
TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie")

# Polling
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "10"))  # seconds
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "10"))

# State file (persists cursor + dedupe across restarts on Railway if disk persists)
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# Discord rate limit safety
SEND_MIN_DELAY = float(os.getenv("SEND_MIN_DELAY", "0.35"))  # seconds between webhook posts
MAX_SEND_PER_POLL = int(os.getenv("MAX_SEND_PER_POLL", "5"))  # keep it low to avoid 429 spikes

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
# DISCORD FORMAT HELPERS
# ============================================================

def classify_color(text: str) -> int:
    """
    Color-code like ARK tribe logs:
    - claiming in purple
    - taming in green
    - deaths in red
    - demolished in yellow
    - otherwise neutral grey
    """
    lower = text.lower()

    if "claimed" in lower or "claiming" in lower:
        return 0x9B59B6  # purple
    if "tamed" in lower or "taming" in lower:
        return 0x2ECC71  # green
    if "killed" in lower or "died" in lower or "was killed" in lower or "was slain" in lower:
        return 0xE74C3C  # red
    if "demolished" in lower or "destroyed" in lower:
        return 0xF1C40F  # yellow

    return 0x95A5A6  # grey


def format_discord_payload(line: str) -> dict:
    text = line.strip()
    return {
        "embeds": [
            {
                "description": text,
                "color": classify_color(text),
            }
        ]
    }


def send_to_discord(payload: dict) -> Tuple[bool, Optional[float], str]:
    """
    Returns (ok, retry_after_seconds, message)
    """
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    except Exception as e:
        return False, None, f"Webhook request failed: {e}"

    if resp.status_code in (200, 204):
        return True, None, "OK"

    # Rate limit handling
    if resp.status_code == 429:
        try:
            data = resp.json()
            retry_after = float(data.get("retry_after", 1.0))
        except Exception:
            retry_after = 1.0
        return False, retry_after, f"Discord webhook error 429: {resp.text}"

    return False, None, f"Discord webhook error {resp.status_code}: {resp.text}"


# ============================================================
# STATE (cursor + dedupe)
# ============================================================

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {
            "offset": 0,
            "seen": [],  # list of recent hashes
            "last_active_size": 0,
            "last_heartbeat_ts": 0,
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "offset": 0,
            "seen": [],
            "last_active_size": 0,
            "last_heartbeat_ts": 0,
        }


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        logging.warning(f"Could not save state: {e}")


def line_fingerprint(line: str) -> str:
    # Strong-ish stable dedupe key
    return hashlib.sha256(line.strip().encode("utf-8", errors="ignore")).hexdigest()


def remember_seen(state: dict, fp: str, limit: int = 2000) -> None:
    seen = state.get("seen", [])
    seen.append(fp)
    if len(seen) > limit:
        seen = seen[-limit:]
    state["seen"] = seen


def has_seen(state: dict, fp: str) -> bool:
    return fp in state.get("seen", [])


# ============================================================
# FTP HELPERS
# ============================================================

def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=20)
    ftp.login(FTP_USER, FTP_PASS)
    return ftp


def ftp_cwd_logs(ftp: ftplib.FTP) -> None:
    # Always operate relative to the logs dir
    ftp.cwd(FTP_LOGS_DIR)


def ftp_get_size_binary(ftp: ftplib.FTP, filename: str) -> Optional[int]:
    """
    Nitrado sometimes blocks SIZE in ASCII mode.
    Force binary TYPE I, then try SIZE.
    """
    try:
        ftp.voidcmd("TYPE I")
        return ftp.size(filename)
    except Exception as e:
        logging.warning(f"Could not get remote file size: {e}")
        return None


def ftp_read_from_offset(ftp: ftplib.FTP, filename: str, offset: int) -> Tuple[bytes, int]:
    """
    Reads file bytes from offset to end via RETR with rest parameter.
    Returns (data, new_offset).
    """
    chunks: List[bytes] = []

    def cb(b: bytes):
        chunks.append(b)

    # Ensure binary transfers
    ftp.voidcmd("TYPE I")

    # Use REST if offset > 0; otherwise read full (but we will set offset to end on first run)
    if offset > 0:
        ftp.retrbinary(f"RETR {filename}", cb, rest=offset)
    else:
        ftp.retrbinary(f"RETR {filename}", cb)

    data = b"".join(chunks)
    return data, offset + len(data)


# ============================================================
# LOG PARSING
# ============================================================

def extract_matching_lines(text: str, target_tribe: str) -> List[str]:
    """
    Returns only lines that contain the target tribe string.
    """
    out = []
    needle = target_tribe.lower()
    for line in text.splitlines():
        if needle in line.lower():
            out.append(line)
    return out


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    require_env()

    logging.info("Starting Container")
    logging.info(f"Polling every {POLL_INTERVAL:.1f} seconds")
    logging.info(f"Filtering: {TARGET_TRIBE}")
    logging.info(f"Logs dir: {FTP_LOGS_DIR}")
    logging.info(f"Reading ONLY: {LOG_FILE}")

    state = load_state()
    first_run = True

    sent_since_last = 0
    last_send_ts = 0.0

    while True:
        sent_this_poll = 0
        new_matching_lines: List[str] = []

        try:
            ftp = ftp_connect()
            try:
                ftp_cwd_logs(ftp)

                # Determine size (best-effort)
                size = ftp_get_size_binary(ftp, LOG_FILE)

                # If first run: start at end to avoid backlog spam
                if first_run:
                    if size is not None:
                        state["offset"] = size
                        state["last_active_size"] = size
                        save_state(state)
                        logging.info("First run: skipped backlog and started live from the end.")
                    else:
                        # No size available; safest is to read full once, then set offset to end
                        data, new_offset = ftp_read_from_offset(ftp, LOG_FILE, 0)
                        state["offset"] = new_offset
                        state["last_active_size"] = new_offset
                        save_state(state)
                        logging.info("First run: SIZE unavailable; consumed file and started live from end.")
                    first_run = False
                    ftp.quit()
                    time.sleep(POLL_INTERVAL)
                    continue

                # If file shrank (rotation), reset offset to 0
                if size is not None and size < int(state.get("offset", 0)):
                    logging.info(f"Log rotated (size {size} < offset {state['offset']}). Resetting offset to 0.")
                    state["offset"] = 0

                # Read new bytes
                offset_before = int(state.get("offset", 0))
                data, offset_after = ftp_read_from_offset(ftp, LOG_FILE, offset_before)
                state["offset"] = offset_after

                # Heartbeat line
                effective_size = size if size is not None else offset_after
                logging.info(
                    f"Heartbeat: file={LOG_FILE} size={effective_size} offset={offset_before}->{offset_after} new_bytes={len(data)}"
                )

                ftp.quit()

                if data:
                    # decode bytes to text; tolerate odd bytes
                    text = data.decode("utf-8", errors="ignore")
                    new_matching_lines = extract_matching_lines(text, TARGET_TRIBE)

            finally:
                try:
                    ftp.close()
                except Exception:
                    pass

        except ftplib.error_perm as e:
            logging.error(f"FTP permission/error: {e}")
        except Exception as e:
            logging.error(f"Error: {e}")

        # If we found matching lines, send ONLY the most recent one (as requested)
        if new_matching_lines:
            most_recent = new_matching_lines[-1].strip()
            fp = line_fingerprint(most_recent)

            if not has_seen(state, fp):
                payload = format_discord_payload(most_recent)

                # Send with rate-limit aware retry
                ok, retry_after, msg = send_to_discord(payload)
                if not ok and retry_after:
                    # wait and retry once
                    time.sleep(retry_after)
                    ok, retry_after2, msg2 = send_to_discord(payload)
                    msg = msg2 if not ok else "OK after retry"

                if ok:
                    remember_seen(state, fp)
                    save_state(state)
                    sent_this_poll += 1
                    sent_since_last += 1
                    logging.info("Sent 1 message to Discord (most recent matching log).")
                else:
                    logging.error(msg)
            else:
                logging.info("Most recent matching line was already sent (deduped).")

        # Heartbeat message to Discord every HEARTBEAT_MINUTES: "No new logs since last"
        now = time.time()
        last_hb = float(state.get("last_heartbeat_ts", 0))
        if now - last_hb >= HEARTBEAT_MINUTES * 60:
            # Only send heartbeat if we didn't send a log recently in this window
            # (keeps noise down)
            if sent_since_last == 0:
                hb_payload = {
                    "embeds": [
                        {
                            "description": "No new logs since last check.",
                            "color": 0x95A5A6,
                        }
                    ]
                }
                ok, retry_after, msg = send_to_discord(hb_payload)
                if not ok and retry_after:
                    time.sleep(retry_after)
                    ok, _, _ = send_to_discord(hb_payload)
                if ok:
                    logging.info("Sent heartbeat to Discord.")
                else:
                    logging.warning(f"Heartbeat send failed: {msg}")

            # reset heartbeat window
            state["last_heartbeat_ts"] = now
            sent_since_last = 0
            save_state(state)

        # Respect delay between polls
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()