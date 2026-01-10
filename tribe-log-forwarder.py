import os
import time
import ftplib
import requests
import re

# ================== CONFIG ==================

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# IMPORTANT: this must exist on FTP
FTP_LOG_DIR = os.getenv("FTP_LOG_DIR", "arksa/ShooterGame/Saved/Logs")

TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Valkyrie")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "10"))

missing = [k for k, v in {
    "FTP_HOST": FTP_HOST,
    "FTP_USER": FTP_USER,
    "FTP_PASS": FTP_PASS,
    "DISCORD_WEBHOOK_URL": DISCORD_WEBHOOK_URL,
    "FTP_LOG_DIR": FTP_LOG_DIR,
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
    m = re.search(r'Day\s+(\d+),\s+(\d{2}:\d{2}:\d{2}):\s*(.+)', line)
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
    if "killed" in t or "died" in t or "starved" in t:
        return 0xE74C3C  # red
    if "demolished" in t or "destroyed" in t:
        return 0xF1C40F  # yellow
    return 0x95A5A6      # grey

def send_discord(text: str, color: int):
    payload = {"embeds": [{"description": text, "color": color}]}
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)

    # Handle Discord rate limits politely
    if r.status_code == 429:
        try:
            retry = float(r.json().get("retry_after", 1))
        except Exception:
            retry = 1.0
        time.sleep(retry)
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)

def ftp_connect():
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=20)
    ftp.login(FTP_USER, FTP_PASS)
    return ftp

def safe_cwd(ftp: ftplib.FTP, path: str):
    """
    Change directory robustly by stepping one folder at a time.
    Supports paths like:
      arksa/ShooterGame/Saved/Logs
      /arksa/ShooterGame/Saved/Logs
    """
    p = path.strip()
    if p.startswith("/"):
        ftp.cwd("/")
        p = p.lstrip("/")

    for part in [x for x in p.split("/") if x]:
        ftp.cwd(part)

def list_logs(ftp):
    names = []
    ftp.retrlines("NLST", names.append)
    return [
        n for n in names
        if n.endswith(".log")
        and "backup" not in n.lower()
        and "failed" not in n.lower()
        and "crash" not in n.lower()
    ]

def read_from_offset(ftp, filename, offset):
    lines = []

    # binary mode for REST offsets
    ftp.sendcmd("TYPE I")
    with ftp.transfercmd(f"RETR {filename}", rest=offset) as conn:
        data = b""
        while True:
            chunk = conn.recv(8192)
            if not chunk:
                break
            data += chunk

    text = data.decode("utf-8", errors="ignore")
    lines = text.splitlines()
    return lines, offset + len(data)

# ================== MAIN ==================

def main():
    print("Starting Container")
    print(f"Polling every {POLL_SECONDS}s")
    print(f"Filtering: Tribe {TARGET_TRIBE}")
    print(f"Using FTP_LOG_DIR: {FTP_LOG_DIR}")

    offsets = {}
    last_heartbeat_sent = time.time()
    first_run = True

    while True:
        try:
            ftp = ftp_connect()
            print("FTP PWD:", ftp.pwd())

            # Try the provided directory, then a couple of common fallbacks
            tried = []
            cwd_ok = False
            for candidate in [FTP_LOG_DIR, "/" + FTP_LOG_DIR, "arksa/ShooterGame/Saved/Logs", "/arksa/ShooterGame/Saved/Logs"]:
                if candidate in tried:
                    continue
                tried.append(candidate)
                try:
                    safe_cwd(ftp, candidate)
                    cwd_ok = True
                    break
                except Exception:
                    # reset back to root before next attempt
                    try:
                        ftp.cwd("/")
                    except Exception:
                        pass

            if not cwd_ok:
                raise RuntimeError(f"Could not CWD into logs dir. Tried: {tried}")

            logs = list_logs(ftp)
            if not logs:
                ftp.quit()
                time.sleep(POLL_SECONDS)
                continue

            latest_log = max(logs)  # simple but works well for these filenames

            # On first time seeing a log, skip backlog
            if latest_log not in offsets:
                try:
                    size = ftp.size(latest_log)
                except Exception:
                    size = 0
                offsets[latest_log] = size
                print(f"Active log selected: {latest_log} (skipping backlog)")
                ftp.quit()
                first_run = False
                time.sleep(POLL_SECONDS)
                continue

            offset = offsets[latest_log]
            lines, new_offset = read_from_offset(ftp, latest_log, offset)
            offsets[latest_log] = new_offset

            # Find only the most recent matching tribe line
            latest_match = None
            for line in reversed(lines):
                if TARGET_TRIBE.lower() in line.lower():
                    summary = extract_summary(line)
                    if summary:
                        latest_match = summary
                        break

            if latest_match:
                send_discord(latest_match, embed_color(latest_match))
                last_heartbeat_sent = time.time()
            else:
                # Heartbeat every X minutes
                if time.time() - last_heartbeat_sent >= HEARTBEAT_MINUTES * 60:
                    send_discord("No new logs since last check.", 0x95A5A6)
                    last_heartbeat_sent = time.time()

            ftp.quit()

        except Exception as e:
            print("Error:", e)

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()