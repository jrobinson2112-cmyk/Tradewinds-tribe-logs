import ftplib
import time
import os
import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone

# =====================
# CONFIG
# =====================
TRIBE_NAME = "Tribe Valkyrie"
POLL_INTERVAL = 5.0
STATE_FILE = "cursor.json"

# =====================
# ENV
# =====================
FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")
FTP_LOG_FILE = os.getenv("FTP_LOG_FILE")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

REQUIRED = [
    FTP_HOST, FTP_USER, FTP_PASS,
    FTP_LOG_FILE, DISCORD_WEBHOOK_URL
]

if not all(REQUIRED):
    raise RuntimeError("Missing required environment variables")

# =====================
# LOG COLORS (Discord)
# =====================
COLOR_CLAIM = 0x9B59B6   # purple
COLOR_TAME  = 0x2ECC71  # green
COLOR_DEATH = 0xE74C3C  # red
COLOR_DEST  = 0xF1C40F  # yellow
COLOR_OTHER = 0x95A5A6  # grey

# =====================
# HELPERS
# =====================
def utc_now():
    return datetime.now(timezone.utc).isoformat()

def load_cursor():
    if not os.path.exists(STATE_FILE):
        return 0
    with open(STATE_FILE, "r") as f:
        return json.load(f).get("offset", 0)

def save_cursor(offset):
    with open(STATE_FILE, "w") as f:
        json.dump({"offset": offset}, f)

def send_webhook(text, color):
    payload = {
        "embeds": [{
            "description": text[:4096],
            "color": color,
            "timestamp": utc_now()
        }]
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        urllib.request.urlopen(req, timeout=20).read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print("Discord error:", e.code, body)
    except Exception as e:
        print("Webhook failed:", e)

def classify_color(line):
    l = line.lower()

    if "claimed" in l:
        return COLOR_CLAIM
    if "tamed" in l or "froze baby" in l:
        return COLOR_TAME
    if "was killed" in l or "starved" in l:
        return COLOR_DEATH
    if "demolished" in l:
        return COLOR_DEST

    return COLOR_OTHER

def clean_line(line):
    line = re.sub(r"<[^>]+>", "", line)
    return line.strip()

# =====================
# FTP READ
# =====================
def read_new_lines():
    offset = load_cursor()
    lines = []

    with ftplib.FTP() as ftp:
        ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
        ftp.login(FTP_USER, FTP_PASS)
        ftp.voidcmd("TYPE I")

        size = ftp.size(FTP_LOG_FILE)
        if size is None or size <= offset:
            return []

        data = bytearray()

        def cb(chunk):
            data.extend(chunk)

        ftp.retrbinary(
            f"RETR {FTP_LOG_FILE}",
            cb,
            rest=offset
        )

    save_cursor(size)
    return data.decode(errors="ignore").splitlines()

# =====================
# MAIN LOOP
# =====================
def main():
    print("Polling every", POLL_INTERVAL, "seconds")

    while True:
        try:
            lines = read_new_lines()

            for line in lines:
                if TRIBE_NAME not in line:
                    continue

                clean = clean_line(line)
                color = classify_color(clean)
                send_webhook(clean, color)

        except Exception as e:
            print("Error:", e)

        time.sleep(POLL_INTERVAL)

# =====================
# START
# =====================
if __name__ == "__main__":
    main()