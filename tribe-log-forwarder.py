import os
import time
import json
import re
import ftplib
import hashlib
from collections import deque
import requests

# ============================================================
# ENV CONFIG
# ============================================================

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# IMPORTANT: Your tribe events are in ShooterGame.log
FTP_LOG_PATH = os.getenv("FTP_LOG_PATH", "arksa/ShooterGame/Saved/Logs/ShooterGame.log")

TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie")

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "10"))

STATE_FILE = "cursor.json"
DEDUP_MAX = int(os.getenv("DEDUP_MAX", "500"))  # remember last 500 sent lines

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
    s = RICH_TAG_RE.sub("", s)  # remove <RichColor ...> etc
    return s.strip()

def line_color(text: str) -> int:
    lower = text.lower()

    # Your requested colours
    if "claimed" in lower or "claiming" in lower:
        return 0x9B59B6  # purple
    if "tamed" in lower or "taming" in lower:
        return 0x2ECC71  # green
    if "was killed" in lower or "killed" in lower or "died" in lower:
        return 0xE74C3C  # red
    if "demolished" in lower or "destroyed" in lower:
        return 0xF1C40F  # yellow

    return 0x95A5A6  # default grey

def format_payload(line: str) -> dict:
    text = clean_ark_text(line)
    return {
        "embeds": [
            {
                "description": text[:4096],
                "color": line_color(text),
            }
        ]
    }

def send_to_discord(payload: dict) -> None:
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    if r.status_code >= 300:
        raise RuntimeError(f"Discord webhook error {r.status_code}: {r.text[:300]}")

# ============================================================
# STATE (offset + dedupe)
# ============================================================

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"offset": 0, "sent": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        offset = int(s.get("offset", 0))
        sent = s.get("sent", [])
        if not isinstance(sent, list):
            sent = []
        return {"offset": offset, "sent": sent}
    except Exception:
        return {"offset": 0, "sent": []}

def save_state(offset: int, sent_hashes: deque) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"offset": int(offset), "sent": list(sent_hashes)}, f)

def hash_line(line: str) -> str:
    # Hash the cleaned line so formatting tags don't cause dupes
    clean = clean_ark_text(line)
    return hashlib.sha256(clean.encode("utf-8", errors="ignore")).hexdigest()

# ============================================================
# FTP TAIL (read only new bytes)
# ============================================================

def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.set_pasv(True)
    return ftp

def ftp_get_size(ftp: ftplib.FTP, path: str) -> int | None:
    # Ensure binary mode for SIZE on some servers
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass
    try:
        resp = ftp.sendcmd(f"SIZE {path}")
        return int(resp.split()[-1])
    except Exception:
        return None

def ftp_read_from_offset(ftp: ftplib.FTP, path: str, offset: int) -> bytes:
    buf = bytearray()
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass

    def cb(chunk: bytes):
        buf.extend(chunk)

    ftp.retrbinary(f"RETR {path}", cb, rest=offset)
    return bytes(buf)

def fetch_new_lines(offset: int) -> tuple[int, list[str]]:
    ftp = ftp_connect()
    try:
        size = ftp_get_size(ftp, FTP_LOG_PATH)

        # Handle rotation/truncation
        if size is not None and size < offset:
            print(f"Log shrank (rotation?) size={size} < offset={offset}. Resetting offset to 0.")
            offset = 0

        raw = ftp_read_from_offset(ftp, FTP_LOG_PATH, offset)
        if not raw:
            return offset, []

        new_offset = offset + len(raw)

        text = raw.decode("utf-8", errors="ignore")
        lines = text.splitlines()
        return new_offset, lines

    finally:
        try:
            ftp.quit()
        except Exception:
            pass

# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("Starting Container")
    print(f"Polling every {POLL_INTERVAL:.1f} seconds")
    print(f"Reading: {FTP_LOG_PATH}")
    print(f"Filtering: {TARGET_TRIBE}")

    state = load_state()
    offset = int(state["offset"])
    sent_hashes = deque(state["sent"], maxlen=DEDUP_MAX)
    sent_set = set(sent_hashes)

    while True:
        try:
            new_offset, lines = fetch_new_lines(offset)

            if lines:
                print(f"Read {len(lines)} new lines")

            sent_count = 0

            for line in lines:
                # Only lines that contain the tribe filter
                if TARGET_TRIBE.lower() not in line.lower():
                    continue

                h = hash_line(line)
                if h in sent_set:
                    continue

                send_to_discord(format_payload(line))
                sent_count += 1

                sent_hashes.append(h)
                sent_set.add(h)

                # Keep set in sync with deque maxlen
                while len(sent_set) > len(sent_hashes):
                    sent_set = set(sent_hashes)

            if sent_count:
                print(f"Sent {sent_count} messages to Discord")

            offset = new_offset
            save_state(offset, sent_hashes)

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()