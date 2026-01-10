import os
import ftplib
import time
import logging
import json
import hashlib
import requests

# =====================
# CONFIG
# =====================
POLL_INTERVAL = 5.0
STATE_FILE = "tribe_state.json"
FILTER_TEXT = "tribe valkyrie"

# =====================
# LOGGING
# =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# =====================
# ENV
# =====================
FTP_HOST = os.getenv("FTP_HOST")
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")
FTP_REMOTE_PATH = os.getenv("FTP_REMOTE_PATH")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

def require_env():
    missing = []
    for k in [
        "FTP_HOST",
        "FTP_USER",
        "FTP_PASS",
        "FTP_REMOTE_PATH",
        "DISCORD_WEBHOOK_URL",
    ]:
        if not os.getenv(k):
            missing.append(k)
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )

# =====================
# STATE
# =====================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"offset": 0, "sent": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# =====================
# DISCORD
# =====================
def send_discord(msg: str):
    requests.post(
        DISCORD_WEBHOOK_URL,
        json={"content": msg[:2000]},
        timeout=10,
    )

# =====================
# FTP
# =====================
def fetch_new_lines(state):
    ftp = ftplib.FTP(FTP_HOST)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.sendcmd("TYPE I")  # binary mode

    size = ftp.size(FTP_REMOTE_PATH)
    if size is None or size <= state["offset"]:
        ftp.quit()
        return []

    lines = []

    def handle(block):
        nonlocal lines
        lines.append(block)

    ftp.retrbinary(
        f"RETR {FTP_REMOTE_PATH}",
        handle,
        rest=state["offset"],
    )

    ftp.quit()

    data = b"".join(lines)
    state["offset"] = size

    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        return []

    return text.splitlines()

# =====================
# MAIN LOOP
# =====================
def main():
    require_env()
    state = load_state()

    logging.info("Polling every %.1fs", POLL_INTERVAL)

    while True:
        try:
            lines = fetch_new_lines(state)

            for line in lines:
                low = line.lower()

                # FILTER: ONLY Tribe Valkyrie
                if FILTER_TEXT not in low:
                    continue

                # Deduplicate by hash
                h = hashlib.sha1(line.encode("utf-8", errors="ignore")).hexdigest()
                if h in state["sent"]:
                    continue

                state["sent"].append(h)
                state["sent"] = state["sent"][-5000:]  # cap memory

                send_discord(line)

            save_state(state)

        except Exception as e:
            logging.error("Error: %s", e)

        time.sleep(POLL_INTERVAL)

# =====================
# START
# =====================
if __name__ == "__main__":
    main()