import os
import time
import ftplib
import re
import hashlib
import requests
from typing import List, Optional, Tuple

# =========================
# ENV CONFIG
# =========================
FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Optional: set this if you know it (recommended). Example:
# FTP_LOGS_DIR=arksa/ShooterGame/Saved/Logs
FTP_LOGS_DIR_ENV = os.getenv("FTP_LOGS_DIR")

TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "10"))

# =========================
# VALIDATION
# =========================
missing = []
for k, v in [
    ("FTP_HOST", FTP_HOST),
    ("FTP_USER", FTP_USER),
    ("FTP_PASS", FTP_PASS),
    ("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL),
]:
    if not v:
        missing.append(k)
if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

# =========================
# DISCORD HELPERS
# =========================
def discord_color_for_line(text: str) -> int:
    lower = text.lower()
    if "claimed" in lower or "claiming" in lower:
        return 0x9B59B6  # purple
    if "tamed" in lower or "taming" in lower:
        return 0x2ECC71  # green
    if "killed" in lower or "died" in lower:
        return 0xE74C3C  # red
    if "demolished" in lower or "destroyed" in lower:
        return 0xF1C40F  # yellow
    return 0x95A5A6  # grey

def clean_line(line: str) -> str:
    # strip ARK rich color tags
    line = re.sub(r"<\/?RichColor[^>]*>", "", line)
    return line.strip()

def send_to_discord(line: str) -> None:
    line = clean_line(line)
    if not line:
        return

    payload = {
        "embeds": [
            {
                "description": line,
                "color": discord_color_for_line(line),
            }
        ]
    }

    while True:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        if r.status_code == 204:
            return
        if r.status_code == 429:
            try:
                data = r.json()
                retry_after = float(data.get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            time.sleep(max(0.25, retry_after))
            continue

        print(f"Error: Discord webhook error {r.status_code}: {r.text}")
        return

def fingerprint(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8", errors="ignore")).hexdigest()

# =========================
# FTP HELPERS
# =========================
def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=20)
    ftp.login(FTP_USER, FTP_PASS)
    # Use binary mode so SIZE/REST works better
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass
    return ftp

def safe_pwd(ftp: ftplib.FTP) -> str:
    try:
        return ftp.pwd()
    except Exception:
        return "(unknown)"

def dir_exists(ftp: ftplib.FTP, path: str) -> bool:
    cur = safe_pwd(ftp)
    try:
        ftp.cwd(path)
        return True
    except Exception:
        return False
    finally:
        try:
            ftp.cwd(cur)
        except Exception:
            pass

def discover_logs_dir(ftp: ftplib.FTP) -> str:
    candidates = []
    if FTP_LOGS_DIR_ENV:
        candidates.append(FTP_LOGS_DIR_ENV.strip("/"))

    # Common Nitrado layouts
    candidates += [
        "arksa/ShooterGame/Saved/Logs",
        "ShooterGame/Saved/Logs",
        "Saved/Logs",
    ]

    # De-dupe preserve order
    seen = set()
    ordered = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)

    print(f"FTP PWD: {safe_pwd(ftp)}")
    for c in ordered:
        if dir_exists(ftp, c):
            print(f"Using logs dir: {c}")
            return c

    print("Could not find Logs dir. Root listing:")
    try:
        print(ftp.nlst())
    except Exception as e:
        print(f"Could not NLST root: {e}")

    raise RuntimeError("No valid logs directory found. Set FTP_LOGS_DIR to the correct path.")

def is_allowed_log_name(name: str) -> bool:
    lower = name.lower()
    if "backup" in lower:
        return False
    if "failedwater" in lower:
        return False
    if lower.endswith(".crashstack"):
        return False

    if name == "ShooterGame.log":
        return True
    if name.startswith("ServerGame.") and name.endswith(".log"):
        return True
    return False

def list_logs_in_dir(ftp: ftplib.FTP) -> List[str]:
    # assumes we're already cwd'd into logs dir
    try:
        names = ftp.nlst()
    except Exception:
        names = []
    return [n for n in names if n and "/" not in n]  # filenames only

def pick_active_log_filename(ftp: ftplib.FTP) -> Optional[str]:
    # Prefer ShooterGame.log if present
    names = list_logs_in_dir(ftp)
    allowed = [n for n in names if is_allowed_log_name(n)]
    if not allowed:
        return None
    if "ShooterGame.log" in allowed:
        return "ShooterGame.log"

    # If MLSD available, pick newest by modify time
    try:
        candidates: List[Tuple[str, str]] = []
        for name, facts in ftp.mlsd():
            if is_allowed_log_name(name):
                candidates.append((name, facts.get("modify", "")))
        if candidates:
            candidates.sort(key=lambda x: x[1], reverse=True)
            return candidates[0][0]
    except Exception:
        pass

    allowed.sort()
    return allowed[-1]

def get_remote_size_current_dir(ftp: ftplib.FTP, filename: str) -> Optional[int]:
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass
    try:
        return ftp.size(filename)
    except Exception as e:
        print(f"Could not get remote file size: {e}")
        return None

def read_from_offset_current_dir(ftp: ftplib.FTP, filename: str, offset: int) -> bytes:
    buf = bytearray()

    def _cb(data: bytes):
        buf.extend(data)

    ftp.voidcmd("TYPE I")
    ftp.retrbinary(f"RETR {filename}", _cb, rest=offset)
    return bytes(buf)

# =========================
# MAIN
# =========================
def main():
    print("Starting Container")
    print(f"Polling every {POLL_INTERVAL:.1f} seconds")
    print(f"Filtering: {TARGET_TRIBE} (sending ONLY the most recent matching log)")

    logs_dir: Optional[str] = None
    active_filename: Optional[str] = None
    offset = 0
    first_run = True
    last_sent_fp: Optional[str] = None

    while True:
        try:
            ftp = ftp_connect()
            try:
                if logs_dir is None:
                    logs_dir = discover_logs_dir(ftp)

                # IMPORTANT: CWD into logs dir and use FILENAMES only
                ftp.cwd(logs_dir)

                chosen = pick_active_log_filename(ftp)
                if not chosen:
                    print(f"No allowed log files in: {logs_dir}")
                    time.sleep(POLL_INTERVAL)
                    continue

                if chosen != active_filename:
                    print(f"Active log selected: {logs_dir}/{chosen}")
                    active_filename = chosen
                    offset = 0
                    first_run = True

                size = get_remote_size_current_dir(ftp, active_filename)
                if size is None:
                    time.sleep(POLL_INTERVAL)
                    continue

                if first_run:
                    offset = size
                    first_run = False
                    print("First run: skipped backlog and started live from the end.")
                    time.sleep(POLL_INTERVAL)
                    continue

                if size < offset:
                    print(f"Log rotated (size {size} < offset {offset}) -> resetting offset")
                    offset = 0

                if size == offset:
                    time.sleep(POLL_INTERVAL)
                    continue

                data = read_from_offset_current_dir(ftp, active_filename, offset)
                offset = size

                text = data.decode("utf-8", errors="ignore")
                lines = [ln for ln in text.splitlines() if ln.strip()]

                newest_match = None
                for ln in reversed(lines):
                    if TARGET_TRIBE.lower() in ln.lower():
                        newest_match = ln
                        break

                if newest_match:
                    fp = fingerprint(newest_match)
                    if fp != last_sent_fp:
                        send_to_discord(newest_match)
                        last_sent_fp = fp
                        print("Sent 1 message to Discord (most recent matching log)")

            finally:
                try:
                    ftp.quit()
                except Exception:
                    pass

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()