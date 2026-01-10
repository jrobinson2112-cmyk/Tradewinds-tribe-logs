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

# Directory containing logs (NOT a file path)
FTP_LOGS_DIR = os.getenv("FTP_LOGS_DIR", "arksa/ShooterGame/Saved/Logs")

TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "10"))

# Only allow these log types (exclude backups etc)
ALLOW_SHOOTERGAME = True
ALLOW_SERVERGAME = True

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
    # Remove ARK RichColor tags etc (optional but makes Discord cleaner)
    # Example: <RichColor Color="1, 0, 1, 1"> ... </>)
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
            # Respect rate limit
            try:
                data = r.json()
                retry_after = float(data.get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            time.sleep(max(0.2, retry_after))
            continue

        # Other errors: print body for debugging
        print(f"Error: Discord webhook error {r.status_code}: {r.text}")
        return

# =========================
# FTP HELPERS
# =========================
def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=20)
    ftp.login(FTP_USER, FTP_PASS)
    # Important for SIZE/MLSD reliability:
    try:
        ftp.voidcmd("TYPE I")  # binary mode
    except Exception:
        pass
    return ftp

def ftp_listdir(ftp: ftplib.FTP, path: str) -> List[str]:
    # Use NLST for max compatibility
    try:
        return ftp.nlst(path)
    except ftplib.error_perm as e:
        # Sometimes NLST needs cwd then nlst() with no args
        ftp.cwd(path)
        return ftp.nlst()

def is_allowed_log(name: str) -> bool:
    base = os.path.basename(name)

    # exclude noisy stuff
    lower = base.lower()
    if "backup" in lower:
        return False
    if "failedwater" in lower:
        return False
    if lower.endswith(".crashstack"):
        return False

    if ALLOW_SHOOTERGAME and base == "ShooterGame.log":
        return True
    if ALLOW_SERVERGAME and base.startswith("ServerGame.") and base.endswith(".log"):
        return True

    return False

def pick_active_log(ftp: ftplib.FTP) -> Optional[str]:
    """
    Pick the most recently modified allowed log from FTP_LOGS_DIR.
    Prefers MLSD timestamps; falls back to filename preference.
    Returns full path.
    """
    # Try MLSD for timestamps (best)
    candidates: List[Tuple[str, str]] = []  # (path, modify)
    try:
        ftp.cwd(FTP_LOGS_DIR)
        for name, facts in ftp.mlsd():
            if not is_allowed_log(name):
                continue
            modify = facts.get("modify", "")
            candidates.append((f"{FTP_LOGS_DIR.rstrip('/')}/{name}", modify))
        if candidates:
            # Sort by modify time (YYYYMMDDHHMMSS)
            candidates.sort(key=lambda x: x[1], reverse=True)
            return candidates[0][0]
    except Exception:
        pass

    # Fallback: NLST + preference order (ShooterGame.log first, else latest ServerGame.* lexicographically)
    try:
        items = ftp_listdir(ftp, FTP_LOGS_DIR)
        allowed = [p for p in items if is_allowed_log(p)]
        if not allowed:
            return None

        # Prefer ShooterGame.log if present
        for p in allowed:
            if os.path.basename(p) == "ShooterGame.log":
                # Ensure full path
                if "/" not in p:
                    return f"{FTP_LOGS_DIR.rstrip('/')}/{p}"
                return p

        # Else pick "largest-looking" / last in sort
        allowed.sort()
        p = allowed[-1]
        if "/" not in p:
            return f"{FTP_LOGS_DIR.rstrip('/')}/{p}"
        return p
    except Exception:
        return None

def get_remote_size(ftp: ftplib.FTP, path: str) -> Optional[int]:
    try:
        ftp.voidcmd("TYPE I")  # binary mode for SIZE
    except Exception:
        pass
    try:
        return ftp.size(path)
    except Exception as e:
        # You were seeing "No such file or directory" here
        print(f"Could not get remote file size: {e}")
        return None

def read_from_offset(ftp: ftplib.FTP, path: str, offset: int) -> bytes:
    buf = bytearray()

    def _cb(data: bytes):
        buf.extend(data)

    ftp.voidcmd("TYPE I")
    ftp.retrbinary(f"RETR {path}", _cb, rest=offset)
    return bytes(buf)

# =========================
# DEDUPE HELPERS
# =========================
def fingerprint(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8", errors="ignore")).hexdigest()

# =========================
# MAIN
# =========================
def main():
    print("Starting Container")
    print(f"Logs dir: {FTP_LOGS_DIR}")
    print("Allowed logs: ShooterGame.log and ServerGame*.log (excluding backups/FailedWater/etc)")
    print(f"Polling every {POLL_INTERVAL:.1f} seconds")
    print(f"Filtering: {TARGET_TRIBE}")

    active_log: Optional[str] = None
    offset = 0
    first_run = True
    last_sent_fp: Optional[str] = None

    while True:
        try:
            ftp = ftp_connect()
            try:
                chosen = pick_active_log(ftp)
                if not chosen:
                    print(f"No ShooterGame.log / ServerGame*.log files found in directory: {FTP_LOGS_DIR}")
                    time.sleep(POLL_INTERVAL)
                    continue

                if chosen != active_log:
                    print(f"Active log selected: {chosen}")
                    active_log = chosen
                    offset = 0
                    first_run = True

                size = get_remote_size(ftp, active_log)
                if size is None:
                    # file missing right now
                    time.sleep(POLL_INTERVAL)
                    continue

                # If first run, skip backlog and start from end
                if first_run:
                    offset = size
                    first_run = False
                    print("First run: skipped backlog and started live from the end.")
                    time.sleep(POLL_INTERVAL)
                    continue

                # If file shrank (rotation), reset offset
                if size < offset:
                    print(f"Log rotated (size {size} < offset {offset}) -> resetting offset")
                    offset = 0

                if size == offset:
                    # nothing new
                    time.sleep(POLL_INTERVAL)
                    continue

                data = read_from_offset(ftp, active_log, offset)
                offset = size

                text = data.decode("utf-8", errors="ignore")
                lines = [ln for ln in text.splitlines() if ln.strip()]

                if not lines:
                    time.sleep(POLL_INTERVAL)
                    continue

                # Find newest matching line only (to avoid webhook spam)
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
                    else:
                        # same newest line as last time
                        pass

            finally:
                try:
                    ftp.quit()
                except Exception:
                    pass

        except ftplib.error_perm as e:
            print(f"Error: FTP permission/error: {e}")
        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()