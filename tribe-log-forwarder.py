import os
import time
import ftplib
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

# Nitrado Logs folder (ASA)
FTP_LOG_DIR = os.getenv("FTP_LOG_DIR", "arksa/ShooterGame/Saved/Logs")

# Tribe filter
TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie")

# Polling
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "10"))

# Heartbeat (minutes) - sends "still alive / no new log data" if nothing new comes in
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "30"))

# If true, skip backlog on first run (recommended)
SKIP_BACKLOG_ON_START = os.getenv("SKIP_BACKLOG_ON_START", "true").lower() in ("1", "true", "yes", "y")

# =========================
# VALIDATION
# =========================
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


# =========================
# DISCORD HELPERS
# =========================
def pick_color(text: str) -> int:
    lower = text.lower()

    # Purple: claiming/unclaiming
    if "claimed" in lower or "unclaimed" in lower or "claiming" in lower:
        return 0x9B59B6

    # Green: taming/tamed
    if "tamed" in lower or "taming" in lower:
        return 0x2ECC71

    # Red: deaths
    if (
        "was killed" in lower
        or "killed" in lower
        or "died" in lower
        or "starved to death" in lower
        or "was slain" in lower
    ):
        return 0xE74C3C

    # Yellow: demolished/destroyed
    if "demolished" in lower or "destroyed" in lower:
        return 0xF1C40F

    # Default grey
    return 0x95A5A6


def discord_payload(message: str, color: int, username: str = "Valkyrie Tribe Logs") -> dict:
    return {
        "username": username,
        "embeds": [
            {
                "description": message,
                "color": color,
            }
        ],
    }


def send_to_discord(payload: dict, max_retries: int = 3) -> None:
    """
    Handles Discord 429 rate limiting using retry_after.
    """
    for attempt in range(max_retries):
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)

        # Success
        if 200 <= resp.status_code < 300:
            return

        # Rate limited
        if resp.status_code == 429:
            try:
                data = resp.json()
                retry_after = float(data.get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            time.sleep(max(0.2, retry_after))
            continue

        # Other errors
        raise RuntimeError(f"Discord webhook error {resp.status_code}: {resp.text}")

    raise RuntimeError("Discord webhook error: exceeded retry attempts (rate limited)")


# =========================
# FTP HELPERS
# =========================
def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=15)
    ftp.login(FTP_USER, FTP_PASS)
    return ftp


def list_allowed_logs(ftp: ftplib.FTP, logs_dir: str) -> List[str]:
    """
    Only allow:
      - ShooterGame.log
      - ServerGame*.log
    Exclude backups, crashstack, FailedWater, etc.
    """
    ftp.cwd(logs_dir)
    names = ftp.nlst()

    allowed = []
    for name in names:
        base = name.split("/")[-1]

        # Exclusions
        lower = base.lower()
        if "backup" in lower:
            continue
        if "failedwater" in lower:
            continue
        if lower.endswith(".crashstack"):
            continue

        # Allowed patterns
        if base == "ShooterGame.log":
            allowed.append(base)
        elif base.startswith("ServerGame") and base.endswith(".log"):
            allowed.append(base)

    return allowed


def ftp_size_binary(ftp: ftplib.FTP, remote_path: str) -> int:
    """
    Some FTP servers require binary mode for SIZE.
    """
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass
    return ftp.size(remote_path)


def read_from_offset(ftp: ftplib.FTP, remote_path: str, offset: int) -> bytes:
    """
    Reads bytes from remote_path starting at offset using REST + RETR.
    """
    chunks: List[bytes] = []

    def _cb(data: bytes) -> None:
        chunks.append(data)

    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass

    ftp.retrbinary(f"RETR {remote_path}", _cb, rest=offset)
    return b"".join(chunks)


def select_active_log(ftp: ftplib.FTP, logs_dir: str, allowed: List[str]) -> Optional[str]:
    """
    Prefer ShooterGame.log if present; otherwise pick the largest ServerGame*.log.
    """
    if not allowed:
        return None

    if "ShooterGame.log" in allowed:
        return f"{logs_dir}/ShooterGame.log"

    # Pick largest servergame log
    best_path = None
    best_size = -1
    for base in allowed:
        p = f"{logs_dir}/{base}"
        try:
            s = ftp_size_binary(ftp, p)
        except Exception:
            continue
        if s > best_size:
            best_size = s
            best_path = p

    return best_path


# =========================
# LOG PARSING / DEDUPE
# =========================
def normalize_line(line: str) -> str:
    # Strip and collapse weird whitespace
    return " ".join(line.strip().split())


def line_hash(line: str) -> str:
    return hashlib.sha256(normalize_line(line).encode("utf-8", errors="ignore")).hexdigest()


def find_last_matching_line(lines: List[str], needle: str) -> Optional[str]:
    """
    Return the last line that contains the target tribe string.
    """
    n = needle.lower()
    for line in reversed(lines):
        if n in line.lower():
            return line
    return None


# =========================
# MAIN
# =========================
def main() -> None:
    print("Starting Container")
    print(f"Polling every {POLL_INTERVAL:.1f} seconds")
    print(f"Filtering: {TARGET_TRIBE} (sending ONLY the most recent matching log)")
    print(f"Logs dir: {FTP_LOG_DIR}")
    print("Allowed logs: ShooterGame.log and ServerGame*.log (excluding backups/FailedWater/etc)")

    # Cursor state
    active_log: Optional[str] = None
    offset: int = 0
    partial_buf: bytes = b""
    last_sent: Optional[str] = None  # hash of last sent line

    # Heartbeat state
    last_any_new_data_ts = time.time()
    last_heartbeat_sent_ts = 0.0

    first_run = True

    while True:
        try:
            ftp = ftp_connect()

            # Find allowed logs and choose active
            allowed = list_allowed_logs(ftp, FTP_LOG_DIR)
            chosen = select_active_log(ftp, FTP_LOG_DIR, allowed)

            if not chosen:
                print(f"No allowed logs found in directory: {FTP_LOG_DIR}")
                ftp.quit()
                time.sleep(POLL_INTERVAL)
                continue

            if chosen != active_log:
                print(f"Active log selected: {chosen}")
                active_log = chosen
                offset = 0
                partial_buf = b""
                first_run = True

            # Get remote size
            try:
                remote_size = ftp_size_binary(ftp, active_log)
            except Exception as e:
                ftp.quit()
                print(f"Could not get remote file size: {e}")
                time.sleep(POLL_INTERVAL)
                continue

            # On first run, optionally skip backlog and start from end
            if first_run and SKIP_BACKLOG_ON_START:
                offset = remote_size
                first_run = False
                ftp.quit()
                print("First run: skipped backlog and started live from the end.")
                time.sleep(POLL_INTERVAL)
                continue

            # If no new bytes, heartbeat logic
            if remote_size <= offset:
                ftp.quit()

                now = time.time()
                minutes_since_new = (now - last_any_new_data_ts) / 60.0

                if HEARTBEAT_MINUTES > 0 and (now - last_heartbeat_sent_ts) >= (HEARTBEAT_MINUTES * 60):
                    hb_msg = f"No new log data from Nitrado yet (last change ~{int(minutes_since_new)} min ago). Still running."
                    try:
                        send_to_discord(discord_payload(hb_msg, 0x95A5A6))
                        print("Heartbeat sent to Discord")
                        last_heartbeat_sent_ts = now
                    except Exception as e:
                        print(f"Heartbeat send failed: {e}")

                time.sleep(POLL_INTERVAL)
                continue

            # Read new bytes
            data = read_from_offset(ftp, active_log, offset)
            ftp.quit()

            offset = remote_size

            if not data:
                time.sleep(POLL_INTERVAL)
                continue

            last_any_new_data_ts = time.time()

            # Combine with any partial line from last poll
            combined = partial_buf + data

            # Split into lines safely
            lines_bytes = combined.split(b"\n")

            # Last element may be an incomplete line
            partial_buf = lines_bytes[-1]
            complete_lines = lines_bytes[:-1]

            decoded_lines = []
            for bline in complete_lines:
                try:
                    decoded_lines.append(bline.decode("utf-8", errors="ignore"))
                except Exception:
                    continue

            # Find ONLY the most recent matching line
            last_match = find_last_matching_line(decoded_lines, TARGET_TRIBE)
            if not last_match:
                time.sleep(POLL_INTERVAL)
                continue

            cleaned = normalize_line(last_match)
            h = line_hash(cleaned)

            # Dedupe: don't resend same line
            if last_sent == h:
                time.sleep(POLL_INTERVAL)
                continue

            # Send it
            color = pick_color(cleaned)
            payload = discord_payload(cleaned, color)

            try:
                send_to_discord(payload)
                print("Sent 1 message to Discord")
                last_sent = h
            except Exception as e:
                print(f"Error sending to Discord: {e}")

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()