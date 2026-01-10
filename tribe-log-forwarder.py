import os
import time
import ftplib
import requests
import re
from datetime import datetime

# ================= CONFIG =================

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

FTP_LOG_DIR = os.getenv("FTP_LOG_DIR", "arksa/ShooterGame/Saved/Logs")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "10"))
MAX_SEND_PER_POLL = int(os.getenv("MAX_SEND_PER_POLL", "1"))

# ================= VALIDATION =================

required = [
    FTP_HOST, FTP_USER, FTP_PASS,
    DISCORD_WEBHOOK_URL, FTP_LOG_DIR
]

if not all(required):
    raise RuntimeError("Missing required environment variables")

# ================= DISCORD =================

def send_embed(text: str, color: int):
    payload = {
        "embeds": [{
            "description": text,
            "color": color
        }]
    }
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    if r.status_code >= 300:
        raise RuntimeError(f"Discord error {r.status_code}: {r.text}")

# ================= LOG CLEANING =================

def clean_line(line: str) -> str:
    # Remove RichColor tags
    line = re.sub(r"<RichColor.*?>", "", line)
    line = re.sub(r"</>", "", line)

    # Remove duplicated timestamps
    line = re.sub(r"^\[.*?\]\[\d+\]", "", line)
    line = re.sub(r"\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}:\s*", "", line)

    # Trim junk endings
    line = line.rstrip("!)").rstrip(")").strip()

    return line

def format_log(line: str) -> str:
    # Extract Day + Time
    m = re.search(r"Day\s+(\d+),\s+(\d{2}:\d{2}:\d{2})", line)
    day, t = m.groups() if m else ("?", "?")

    # Remove leading tribe text
    line = re.sub(r".*?:\s*", "", line)

    return f"Day {day}, {t} - {line}"

def get_color(text: str) -> int:
    l = text.lower()
    if "claimed" in l or "unclaimed" in l:
        return 0x9B59B6  # purple
    if "tamed" in l:
        return 0x2ECC71  # green
    if "killed" in l or "died" in l or "starved":
        return 0xE74C3C  # red
    if "demolished" in l or "destroyed" in l:
        return 0xF1C40F  # yellow
    return 0x95A5A6  # grey

# ================= FTP =================

def ftp_connect():
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=15)
    ftp.login(FTP_USER, FTP_PASS)
    return ftp

def list_logs(ftp):
    files = ftp.nlst(FTP_LOG_DIR)
    return [
        f for f in files
        if f.lower().endswith(".log")
        and "backup" not in f.lower()
        and "failed" not in f.lower()
    ]

def read_new_lines(ftp, path, offset):
    lines = []

    def handle(line):
        lines.append(line)

    ftp.retrlines(f"RETR {path}", handle)
    size = sum(len(l) + 1 for l in lines)
    return lines[offset:], size

# ================= MAIN =================

def main():
    print("Starting Tribe Log Forwarder")
    print(f"Filtering: {TARGET_TRIBE}")

    last_offsets = {}
    last_heartbeat = time.time()
    first_run = True

    while True:
        try:
            ftp = ftp_connect()
            logs = list_logs(ftp)

            sent = 0

            for log in sorted(logs):
                offset = last_offsets.get(log, 0)
                lines, size = read_new_lines(ftp, log, offset)
                last_offsets[log] = size

                if first_run:
                    continue

                matches = [
                    l for l in lines
                    if TARGET_TRIBE.lower() in l.lower()
                ]

                if matches:
                    raw = matches[-1]
                    cleaned = clean_line(raw)
                    formatted = format_log(cleaned)
                    color = get_color(formatted)

                    send_embed(formatted, color)
                    sent += 1

                    if sent >= MAX_SEND_PER_POLL:
                        break

            ftp.quit()

            if not sent and time.time() - last_heartbeat >= HEARTBEAT_MINUTES * 60:
                send_embed("No new logs since last check.", 0x95A5A6)
                last_heartbeat = time.time()

            first_run = False

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()