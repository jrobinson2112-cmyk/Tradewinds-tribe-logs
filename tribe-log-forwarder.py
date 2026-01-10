import os
import time
import ftplib
import json
import re
import requests

# ============================================================
# CONFIG (ENV VARS)
# ============================================================

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Path to log file on FTP
FTP_LOG_PATH = os.getenv("FTP_LOG_PATH", "arksa/ShooterGame/Saved/Logs/ShooterGame.log")

# Tribe name to filter (you said “Valkyrie” / “Tribe Valkyrie”)
TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Valkyrie")

# Poll interval (seconds)
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "15"))

# Where we store our cursor so we only send each line once
STATE_FILE = "cursor.json"

# ============================================================
# VALIDATION
# ============================================================

missing = [k for k, v in {
    "FTP_HOST": FTP_HOST,
    "FTP_USER": FTP_USER,
    "FTP_PASS": FTP_PASS,
    "DISCORD_WEBHOOK_URL": DISCORD_WEBHOOK_URL,
}.items() if not v]

if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


# ============================================================
# DISCORD FORMAT HELPERS
# ============================================================

def clean_ark_tags(text: str) -> str:
    # Strip <RichColor ...> etc
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def format_log_line(line: str) -> dict:
    """
    Returns Discord embed payload with color based on ARK log type
    """
    text = clean_ark_tags(line)

    color = 0x95A5A6  # default grey
    lower = text.lower()

    if "claimed" in lower or "claiming" in lower:
        color = 0x9B59B6  # purple
    elif "tamed" in lower or "taming" in lower or "froze baby" in lower:
        color = 0x2ECC71  # green
    elif "was killed" in lower or "killed" in lower or "died" in lower or "starved" in lower:
        color = 0xE74C3C  # red
    elif "demolished" in lower or "destroyed" in lower:
        color = 0xF1C40F  # yellow

    return {
        "embeds": [
            {
                "description": text[:4096],
                "color": color
            }
        ]
    }


def send_to_discord(payload: dict):
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    # Useful if webhook is failing (401/404/429 etc)
    if r.status_code >= 300:
        raise RuntimeError(f"Discord webhook error {r.status_code}: {r.text[:300]}")


# ============================================================
# STATE (CURSOR) HANDLING
# ============================================================

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"offset": 0}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        if "offset" not in s:
            s["offset"] = 0
        return s
    except Exception:
        return {"offset": 0}


def save_state(offset: int):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"offset": int(offset)}, f)


# ============================================================
# FTP LOG HANDLING (READ ONLY NEW BYTES)
# ============================================================

def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.set_pasv(True)
    return ftp


def ftp_get_size(ftp: ftplib.FTP, path: str) -> int | None:
    """
    SIZE often fails in ASCII mode on some servers, but in binary it may work.
    If it fails, we return None and still continue using REST read.
    """
    try:
        try:
            ftp.voidcmd("TYPE I")  # binary mode
        except Exception:
            pass
        resp = ftp.sendcmd(f"SIZE {path}")
        return int(resp.split()[-1])
    except Exception:
        return None


def ftp_read_from_offset(ftp: ftplib.FTP, path: str, offset: int) -> bytes:
    data = bytearray()

    def cb(chunk: bytes):
        data.extend(chunk)

    # Ensure binary mode so REST offsets are bytes
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass

    # REST is supported by most FTP servers
    ftp.retrbinary(f"RETR {path}", cb, rest=offset)
    return bytes(data)


def fetch_new_lines() -> list[str]:
    state = load_state()
    offset = int(state.get("offset", 0))

    ftp = ftp_connect()
    try:
        size = ftp_get_size(ftp, FTP_LOG_PATH)

        # If the log rotated/truncated, reset cursor
        if size is not None and size < offset:
            print(f"Log shrank (rotation?) size={size} < offset={offset}. Resetting offset to 0.")
            offset = 0

        raw = ftp_read_from_offset(ftp, FTP_LOG_PATH, offset)
        if not raw:
            return []

        new_offset = offset + len(raw)
        save_state(new_offset)

        text = raw.decode("utf-8", errors="ignore")
        return text.splitlines()

    finally:
        try:
            ftp.quit()
        except Exception:
            pass


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("Tribe log forwarder started")
    print("FTP_LOG_PATH:", FTP_LOG_PATH)
    print("TARGET_TRIBE:", TARGET_TRIBE)
    print(f"Polling every {POLL_INTERVAL:.1f}s")

    while True:
        try:
            lines = fetch_new_lines()

            if lines:
                print(f"Read {len(lines)} new lines")

            sent = 0
            for line in lines:
                if TARGET_TRIBE.lower() in line.lower():
                    payload = format_log_line(line)
                    send_to_discord(payload)
                    sent += 1

            if lines and sent == 0:
                print("No matching tribe lines found in new data.")

            if sent:
                print(f"Sent {sent} messages to Discord.")

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()