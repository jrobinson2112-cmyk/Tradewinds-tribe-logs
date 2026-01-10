import os
import time
import ftplib
import re
import hashlib
import requests
from typing import List, Tuple, Optional

# =========================
# ENV CONFIG
# =========================
FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Optional: if you set this, the code will try it first
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
    return 0x95A5A6  # grey default

def clean_line(line: str) -> str:
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
            time.sleep(max(0.2, retry_after))
            continue

        print(f"Error: Discord webhook error {r.status_code}: {r.text}")
        return

# =========================
# FTP HELPERS
# =========================
def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=20)
    ftp.login(FTP_USER, FTP_PASS)
    # Make SIZE/REST more reliable
    try:
        ftp.voidcmd("TYPE I")  # binary
    except Exception:
        pass
    return ftp

def dir_exists(ftp: ftplib.FTP, path: str) -> bool:
    cur = None
    try:
        cur = ftp.pwd()
    except Exception:
        cur = None

    try:
        ftp.cwd(path)
        return True
    except Exception:
        return False
    finally:
        if cur:
            try:
                ftp.cwd(cur)
            except Exception:
                pass

def discover_logs_dir(ftp: ftplib.FTP) -> str:
    # Try user's configured dir first, then common Nitrado layouts
    candidates = []
    if FTP_LOGS_DIR_ENV:
        candidates.append(FTP_LOGS_DIR_ENV.strip("/"))

    candidates += [
        "arksa/ShooterGame/Saved/Logs",
        "ShooterGame/Saved/Logs",
        "Saved/Logs",
    ]

    # De-dupe while preserving order
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

    # If none work, give a helpful debug dump
    print("Could not find a working Logs directory. Top-level listing:")
    try:
        print(ftp.nlst())
    except Exception as e:
        print(f"Could not NLST root: {e}")

    raise RuntimeError("No valid logs directory found. Set FTP_LOGS_DIR to the correct path for your FTP root.")

def safe_pwd(ftp: ftplib.FTP) -> str:
    try:
        return ftp.pwd()
    except Exception:
        return "(unknown)"

def ftp_listdir(ftp: ftplib.FTP, path: str) -> List[str]:
    try:
        return ftp.nlst(path)
    except ftplib.error_perm:
        ftp.cwd(path)
        return ftp.nlst()

def is_allowed_log(full_path: str) -> bool:
    base = os.path.basename(full_path)
    lower = base.lower()

    if "backup" in lower:
        return False
    if "failedwater" in lower:
        return False
    if lower.endswith(".crashstack"):
        return False

    if base == "ShooterGame.log":
        return True
    if base.startswith("ServerGame.") and base.endswith(".log"):
        return True

    return False

def pick_active_log(ftp: ftplib.FTP, logs_dir: str) -> Optional[str]:
    # Best: MLSD for timestamps
    try:
        ftp.cwd(logs_dir)
        candidates: List[Tuple[str, str]] = []
        for name, facts in ftp.mlsd():
            p = f"{logs_dir.rstrip('/')}/{name}"
            if not is_allowed_log(p):
                continue
            candidates.append((p, facts.get("modify", "")))
        if candidates:
            candidates.sort(key=lambda x: x[1], reverse=True)
            return candidates[0][0]
    except Exception:
        pass

    # Fallback: NLST + preference
    items = ftp_listdir(ftp, logs_dir)
    allowed = []
    for p in items:
        # NLST may return absolute-ish or just names depending on server
        if "/" not in p:
            p = f"{logs_dir.rstrip('/')}/{p}"
        if is_allowed_log(p):
            allowed.append(p)

    if not allowed:
        return None

    # Prefer ShooterGame.log if present
    for p in allowed:
        if os.path.basename(p) == "ShooterGame.log":
            return p

    allowed.sort()
    return allowed[-1]

def get_remote_size(ftp: ftplib.FTP, path: str) -> Optional[int]:
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass
    try:
        return ftp.size(path)
    except Exception as e:
        print(f"Could not get remote file size: {e}")
        return None

def read_from_offset(ftp: ftplib.FTP, path: str, offset: int) -> bytes:
    buf = bytearray()

    def _cb(data: bytes):
        buf.extend(data)

    ftp.voidcmd("TYPE I")
    ftp.retrbinary(f"RETR {path}", _cb, rest=offset)
    return bytes(buf)

def fingerprint(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8", errors="ignore")).hexdigest()

# =========================
# MAIN
# =========================
def main():
    print("Starting Container")
    print(f"Polling every {POLL_INTERVAL:.1f} seconds")
    print(f"Filtering: {TARGET_TRIBE} (sending ONLY the most recent matching log)")

    logs_dir: Optional[str] = None
    active_log: Optional[str] = None
    offset = 0
    first_run = True
    last_sent_fp: Optional[str] = None

    while True:
        try:
            ftp = ftp_connect()
            try:
                if logs_dir is None:
                    logs_dir = discover_logs_dir(ftp)

                chosen = pick_active_log(ftp, logs_dir)
                if not chosen:
                    print(f"No ShooterGame.log / ServerGame*.log found in: {logs_dir}")
                    time.sleep(POLL_INTERVAL)
                    continue

                if chosen != active_log:
                    print(f"Active log selected: {chosen}")
                    active_log = chosen
                    offset = 0
                    first_run = True

                size = get_remote_size(ftp, active_log)
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

                data = read_from_offset(ftp, active_log, offset)
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