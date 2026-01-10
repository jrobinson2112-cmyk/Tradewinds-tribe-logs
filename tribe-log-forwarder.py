import os
import time
import json
import re
import ftplib
import hashlib
from typing import List, Optional, Tuple

import requests

# =========================
# ENV CONFIG
# =========================

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Point this at the Logs DIRECTORY (recommended)
FTP_LOG_DIR = os.getenv("FTP_LOG_DIR", "arksa/ShooterGame/Saved/Logs").rstrip("/")

TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "10"))

STATE_FILE = "cursor.json"

# If true, first run starts at end and sends nothing until a new matching line appears
SKIP_BACKLOG_ON_FIRST_RUN = os.getenv("SKIP_BACKLOG_ON_FIRST_RUN", "true").lower() in ("1", "true", "yes")

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
# DISCORD FORMATTING
# =========================

RICH_TAG_RE = re.compile(r"<[^>]+>")

def clean_ark_text(s: str) -> str:
    s = s.strip()
    s = RICH_TAG_RE.sub("", s)
    return s.strip()

def line_color(text: str) -> int:
    lower = text.lower()
    if "claimed" in lower or "unclaimed" in lower or "claiming" in lower:
        return 0x9B59B6  # purple
    if "tamed" in lower or "taming" in lower:
        return 0x2ECC71  # green
    if "was killed" in lower or "killed" in lower or "died" in lower or "starved to death" in lower:
        return 0xE74C3C  # red
    if "demolished" in lower or "destroyed" in lower:
        return 0xF1C40F  # yellow
    return 0x95A5A6      # grey

def format_payload(line: str) -> dict:
    txt = clean_ark_text(line)
    return {"embeds": [{"description": txt[:4096], "color": line_color(txt)}]}

def send_to_discord(payload: dict) -> None:
    while True:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
        if r.status_code in (200, 204):
            return
        if r.status_code == 429:
            try:
                data = r.json()
                retry_after = float(data.get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            time.sleep(max(0.05, retry_after))
            continue
        raise RuntimeError(f"Discord webhook error {r.status_code}: {r.text[:500]}")

def hash_line(line: str) -> str:
    return hashlib.sha256(clean_ark_text(line).encode("utf-8", errors="ignore")).hexdigest()

# =========================
# STATE
# =========================

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"path": None, "offset": 0, "initialized": False, "last_hash": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        return {
            "path": s.get("path"),
            "offset": int(s.get("offset", 0)),
            "initialized": bool(s.get("initialized", False)),
            "last_hash": s.get("last_hash"),
        }
    except Exception:
        return {"path": None, "offset": 0, "initialized": False, "last_hash": None}

def save_state(path: str, offset: int, initialized: bool, last_hash: Optional[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"path": path, "offset": int(offset), "initialized": bool(initialized), "last_hash": last_hash},
            f,
        )

# =========================
# FTP HELPERS
# =========================

def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.set_pasv(True)
    # Use binary mode so SIZE works on many servers
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass
    return ftp

def ftp_size(ftp: ftplib.FTP, path: str) -> Optional[int]:
    try:
        resp = ftp.sendcmd(f"SIZE {path}")
        return int(resp.split()[-1])
    except Exception:
        return None

def ftp_list_logs(ftp: ftplib.FTP, log_dir: str) -> List[str]:
    names: List[str] = []
    try:
        ftp.cwd(log_dir)
        ftp.retrlines("NLST", names.append)
    finally:
        # Return to root-ish isn't necessary, but harmless
        pass
    # Convert to full paths and keep only .log
    full = []
    for n in names:
        n = n.strip()
        if not n:
            continue
        # NLST might return full paths or names depending on server
        if "/" in n:
            path = n
        else:
            path = f"{log_dir}/{n}"
        if path.lower().endswith(".log"):
            full.append(path)
    return full

def choose_active_log(ftp: ftplib.FTP, log_dir: str, prefer_contains: Tuple[str, ...] = ("ShooterGame", "ServerGame")) -> Optional[str]:
    logs = ftp_list_logs(ftp, log_dir)
    if not logs:
        return None

    # Gather (path, size)
    info = []
    for p in logs:
        sz = ftp_size(ftp, p)
        # If SIZE unsupported, still keep it but with -1
        info.append((p, sz if sz is not None else -1))

    # Prefer known names first, but still pick the largest among them
    preferred = [x for x in info if any(k.lower() in x[0].lower() for k in prefer_contains)]
    if preferred:
        preferred.sort(key=lambda t: t[1], reverse=True)
        return preferred[0][0]

    # Otherwise pick largest .log file
    info.sort(key=lambda t: t[1], reverse=True)
    return info[0][0]

def ftp_read_from_offset(ftp: ftplib.FTP, path: str, offset: int) -> bytes:
    buf = bytearray()
    def cb(chunk: bytes):
        buf.extend(chunk)
    ftp.retrbinary(f"RETR {path}", cb, rest=offset)
    return bytes(buf)

def fetch_new_lines(path: str, offset: int) -> Tuple[int, List[str], Optional[int]]:
    ftp = ftp_connect()
    try:
        size = ftp_size(ftp, path)
        if size is not None and size < offset:
            print(f"Log shrank/rotated: size={size} < offset={offset}. Resetting to 0.")
            offset = 0

        raw = ftp_read_from_offset(ftp, path, offset)
        if not raw:
            return offset, [], size

        new_offset = offset + len(raw)
        text = raw.decode("utf-8", errors="ignore")
        return new_offset, text.splitlines(), size
    finally:
        try:
            ftp.quit()
        except Exception:
            pass

# =========================
# MAIN
# =========================

def main():
    print("Starting Container")
    print(f"Polling every {POLL_INTERVAL:.1f} seconds")
    print(f"Filtering: {TARGET_TRIBE} (sending ONLY the most recent matching log)")

    state = load_state()
    current_path = state.get("path")
    offset = int(state.get("offset", 0))
    initialized = bool(state.get("initialized", False))
    last_hash = state.get("last_hash")

    # Resolve an active log file on startup
    ftp = ftp_connect()
    try:
        chosen = choose_active_log(ftp, FTP_LOG_DIR)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    if not chosen:
        raise RuntimeError(f"No .log files found in directory: {FTP_LOG_DIR}")

    if current_path != chosen:
        print(f"Active log selected: {chosen}")
        current_path = chosen
        offset = 0
        initialized = False
        last_hash = None

    while True:
        try:
            # Every loop, re-evaluate active log in case the server switched files
            ftp = ftp_connect()
            try:
                latest = choose_active_log(ftp, FTP_LOG_DIR)
            finally:
                try:
                    ftp.quit()
                except Exception:
                    pass

            if latest and latest != current_path:
                print(f"Log target changed: {current_path} -> {latest} (resetting cursor)")
                current_path = latest
                offset = 0
                initialized = False
                last_hash = None

            new_offset, lines, size = fetch_new_lines(current_path, offset)

            if size is not None:
                print(f"Heartbeat: file={os.path.basename(current_path)} size={size} offset={offset}->{new_offset} new_lines={len(lines)}")
            else:
                print(f"Heartbeat: file={os.path.basename(current_path)} offset={offset}->{new_offset} new_lines={len(lines)} (SIZE n/a)")

            # First run: optionally skip backlog
            if not initialized and SKIP_BACKLOG_ON_FIRST_RUN:
                offset = new_offset
                initialized = True
                save_state(current_path, offset, initialized, last_hash)
                print("First run: skipped backlog and started live from the end.")
                time.sleep(POLL_INTERVAL)
                continue

            offset = new_offset

            # Send ONLY the most recent matching line from the NEW chunk
            most_recent = None
            for line in reversed(lines):
                if TARGET_TRIBE.lower() in line.lower():
                    most_recent = line
                    break

            if most_recent:
                h = hash_line(most_recent)
                if h != last_hash:
                    send_to_discord(format_payload(most_recent))
                    last_hash = h
                    print("Sent 1 message to Discord (most recent match)")
                else:
                    print("Most recent match already sent (deduped)")
            else:
                if lines:
                    print("No matching Valkyrie line in the new chunk.")

            initialized = True
            save_state(current_path, offset, initialized, last_hash)

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()