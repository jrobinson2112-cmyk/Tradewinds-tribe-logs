import os
import time
import ftplib
import requests
import re
from datetime import datetime

# ================== CONFIG ==================

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")
FTP_LOG_DIR = os.getenv("FTP_LOG_DIR", "arksa/ShooterGame/Saved/Logs")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Valkyrie")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "10"))

# ================== VALIDATION ==================

required = [
    FTP_HOST, FTP_USER, FTP_PASS,
    DISCORD_WEBHOOK_URL, FTP_LOG_DIR
]

missing = [k for k, v in {
    "FTP_HOST": FTP_HOST,
    "FTP_USER": FTP_USER,
    "FTP_PASS": FTP_PASS,
    "DISCORD_WEBHOOK_URL": DISCORD_WEBHOOK_URL,
    "FTP_LOG_DIR": FTP_LOG_DIR
}.items() if not v]

if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

# ================== HELPERS ==================

def clean_ark_markup(text: str) -> str:
    text = re.sub(r'<RichColor[^>]*>', '', text)
    text = re.sub(r'</[^>]*>', '', text)
    text = text.replace('</>)', '').replace('>)', ')')
    return text.strip()

def extract_summary(line: str) -> str | None:
    m = re.search(
        r'Day\s+(\d+),\s+(\d{2}:\d{2}:\d{2}):\s*(.+)',
        line
    )
    if not m:
        return None

    day, time_str, rest = m.groups()
    rest = clean_ark_markup(rest)
    return f"Day {day}, {time_str} - {rest}"

def embed_color(text: str) -> int:
    t = text.lower()
    if "claimed" in t or "unclaimed" in t:
        return 0x9B59B6  # purple
    if "tamed" in t:
        return 0x2ECC71  # green
    if "killed" in t or "died" in t:
        return 0xE74C3C  # red
    if "demolished" in t or "destroyed" in t:
        return 0xF1C40F  # yellow
    return 0x95A5A6      # grey

def send_discord(text: str, color: int):
    payload = {
        "embeds": [{
            "description": text,
            "color": color
        }]
    }
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    if r.status_code == 429:
        retry = r.json().get("retry_after", 1)
        time.sleep(float(retry))
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)

# ================== FTP ==================

def ftp_connect():
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=15)
    ftp.login(FTP_USER, FTP_PASS)
    return ftp

def list_logs(ftp):
    files = []
    ftp.retrlines("NLST", files.append)
    return [
        f for f in files
        if f.endswith(".log")
        and "backup" not in f.lower()
        and "failed" not in f.lower()
    ]

def read_from_offset(ftp, path, offset):
    lines = []
    def cb(line):
        lines.append(line)

    ftp.sendcmd("TYPE I")
    with ftp.transfercmd(f"RETR {path}", rest=offset) as conn:
        buf = b""
        while True:
            chunk = conn.recv(8192)
            if not chunk:
                break
            buf += chunk
        for l in buf.decode("utf-8", errors="ignore").splitlines():
            cb(l)

    return lines, offset + len(buf)

# ================== MAIN ==================

def main():
    print("Starting Container")
    print(f"Polling every {POLL_SECONDS}s")
    print(f"Filtering: Tribe {TARGET_TRIBE}")

    offsets = {}
    last_heartbeat = time.time()

    while True:
        try:
            ftp = ftp_connect()
            ftp.cwd(FTP_LOG_DIR)

            logs = list_logs(ftp)
            if not logs:
                ftp.quit()
                time.sleep(POLL_SECONDS)
                continue

            latest_log = max(logs)
            if latest_log not in offsets:
                size = ftp.size(latest_log)
                offsets[latest_log] = size
                print(f"Active log selected: {latest_log} (skipping backlog)")
                ftp.quit()
                time.sleep(POLL_SECONDS)
                continue

            offset = offsets[latest_log]
            lines, new_offset = read_from_offset(ftp, latest_log, offset)
            offsets[latest_log] = new_offset

            latest_match = None
            for line in reversed(lines):
                if TARGET_TRIBE.lower() in line.lower():
                    summary = extract_summary(line)
                    if summary:
                        latest_match = summary
                        break

            if latest_match:
                send_discord(latest_match, embed_color(latest_match))
            else:
                if time.time() - last_heartbeat >= HEARTBEAT_MINUTES * 60:
                    send_discord("No new logs since last check.", 0x95A5A6)
                    last_heartbeat = time.time()

            ftp.quit()

        except Exception as e:
            print("Error:", e)

        time.sleep(POLL_SECONDS)

# ================== RUN ==================

if __name__ == "__main__":
    main()