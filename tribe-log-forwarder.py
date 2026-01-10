import os
import time
import json
import re
import ftplib
import hashlib
from typing import List, Optional, Tuple, Dict

import requests

# =========================
# ENV CONFIG
# =========================

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

FTP_LOG_DIR = os.getenv("FTP_LOG_DIR", "arksa/ShooterGame/Saved/Logs").rstrip("/")

TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "10"))

STATE_FILE = "cursor.json"

# Behavior toggles
SKIP_BACKLOG_ON_FIRST_RUN = os.getenv("SKIP_BACKLOG_ON_FIRST_RUN", "true").lower() in ("1", "true", "yes")
SEND_LATEST_ON_START = os.getenv("SEND_LATEST_ON_START", "true").lower() in ("1", "true", "yes")

# How many bytes from the end to inspect when sending "latest on start"
TAIL_BYTES = int(os.getenv("TAIL_BYTES", "200000"))  # ~200 KB

# Only these log types are allowed
ALLOW_EXACT = {"shootergame.log"}
ALLOW_PREFIX = ("servergame",)

# Always ignore these patterns
DENY_SUBSTRINGS = (
    "-backup-",
    "failedwaterdinospawns",
    ".crashstack",
)

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
    return 0x95A5A6      # default

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
        return {"path": None, "offset": 0, "initialized": False, "last_hash": None, "sizes": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        return {
            "path": s.get("path"),
            "offset": int(s.get("offset", 0)),
            "initialized": bool(s.get("initialized", False)),
            "last_hash": s.get("last_hash"),
            "sizes": s.get("sizes", {}) or {},
        }
    except Exception:
        return {"path": None, "offset": 0, "initialized": False, "last_hash": None, "sizes": {}}

def save_state(path: str, offset: int, initialized: bool, last_hash: Optional[str], sizes: Dict[str, int]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "path": path,
                "offset": int(offset),
                "initialized": bool(initialized),
                "last_hash": last_hash,
                "sizes": sizes,
            },
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
    try:
        ftp.voidcmd("TYPE I")  # binary mode (helps SIZE)
    except Exception:
        pass
    return ftp

def ftp_size(ftp: ftplib.FTP, path: str) -> Optional[int]:
    try:
        resp = ftp.sendcmd(f"SIZE {path}")
        return int(resp.split()[-1])
    except Exception:
        return None

def is_allowed_log(path: str) -> bool:
    base = path.split("/")[-1].lower()
    for bad in DENY_SUBSTRINGS:
        if bad in base:
            return False
    if base in ALLOW_EXACT:
        return True
    if any(base.startswith(pfx) for pfx in ALLOW_PREFIX) and base.endswith(".log"):
        return True
    return False

def ftp_list_allowed_logs(ftp: ftplib.FTP, log_dir: str) -> List[str]:
    names: List[str] = []
    ftp.cwd(log_dir)
    ftp.retrlines("NLST", names.append)

    full_paths: List[str] = []
    for n in names:
        n = n.strip()
        if not n:
            continue
        path = n if "/" in n else f"{log_dir}/{n}"
        if is_allowed_log(path):
            full_paths.append(path)
    return full_paths

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

def fetch_tail_lines(path: str, tail_bytes: int) -> Tuple[int, List[str], Optional[int]]:
    ftp = ftp_connect()
    try:
        size = ftp_size(ftp, path)
        if size is None:
            # fallback: read from 0 (not ideal)
            start = 0
        else:
            start = max(0, size - tail_bytes)

        raw = ftp_read_from_offset(ftp, path, start)
        text = raw.decode("utf-8", errors="ignore")
        return (size if size is not None else start + len(raw)), text.splitlines(), size
    finally:
        try:
            ftp.quit()
        except Exception:
            pass

# =========================
# LOG SELECTION (FOLLOW THE GROWING LOG)
# =========================

def pick_growing_log(logs: List[str], sizes_now: Dict[str, int], sizes_prev: Dict[str, int], current: Optional[str]) -> Optional[str]:
    # Choose log with biggest positive growth since last check
    best = None
    best_delta = 0

    for p in logs:
        now = sizes_now.get(p, -1)
        prev = sizes_prev.get(p, -1)
        if now >= 0 and prev >= 0:
            delta = now - prev
            if delta > best_delta:
                best_delta = delta
                best = p

    # If something grew, follow it
    if best and best_delta > 0:
        return best

    # Otherwise keep current if it still exists
    if current in logs:
        return current

    # Fallback: prefer ShooterGame.log if present
    for p in logs:
        if p.split("/")[-1].lower() == "shootergame.log":
            return p

    # Otherwise pick largest
    biggest = None
    biggest_sz = -1
    for p in logs:
        sz = sizes_now.get(p, -1)
        if sz > biggest_sz:
            biggest_sz = sz
            biggest = p
    return biggest

# =========================
# MAIN
# =========================

def main():
    print("Starting Container")
    print(f"Polling every {POLL_INTERVAL:.1f} seconds")
    print(f"Filtering: {TARGET_TRIBE} (sending ONLY the most recent matching log)")
    print(f"Logs dir: {FTP_LOG_DIR}")
    print("Allowed logs: ShooterGame.log and ServerGame*.log (excluding backups/FailedWater/etc)")

    state = load_state()
    current_path = state.get("path")
    offset = int(state.get("offset", 0))
    initialized = bool(state.get("initialized", False))
    last_hash = state.get("last_hash")
    sizes_prev: Dict[str, int] = {k: int(v) for k, v in (state.get("sizes") or {}).items()}

    while True:
        try:
            ftp = ftp_connect()
            try:
                allowed = ftp_list_allowed_logs(ftp, FTP_LOG_DIR)
                if not allowed:
                    print(f"No allowed logs found in directory: {FTP_LOG_DIR}")
                    time.sleep(POLL_INTERVAL)
                    continue

                sizes_now: Dict[str, int] = {}
                for p in allowed:
                    sz = ftp_size(ftp, p)
                    if sz is not None:
                        sizes_now[p] = sz

                chosen = pick_growing_log(allowed, sizes_now, sizes_prev, current_path)
            finally:
                try:
                    ftp.quit()
                except Exception:
                    pass

            if not chosen:
                print("No chosen log this poll.")
                time.sleep(POLL_INTERVAL)
                continue

            if chosen != current_path:
                print(f"Log target changed: {current_path} -> {chosen} (resetting cursor)")
                current_path = chosen
                offset = 0
                initialized = False
                # keep last_hash so we still dedupe the “latest” line if it repeats across logs

            base = current_path.split("/")[-1]

            # Startup behavior: skip backlog BUT send the latest matching line once
            if not initialized and SKIP_BACKLOG_ON_FIRST_RUN and SEND_LATEST_ON_START:
                end_offset, tail_lines, size = fetch_tail_lines(current_path, TAIL_BYTES)
                most_recent = None
                for line in reversed(tail_lines):
                    if TARGET_TRIBE.lower() in line.lower():
                        most_recent = line
                        break
                if most_recent:
                    h = hash_line(most_recent)
                    if h != last_hash:
                        send_to_discord(format_payload(most_recent))
                        last_hash = h
                        print("Sent latest matching line on startup.")
                    else:
                        print("Startup latest already sent (deduped).")
                else:
                    print("No matching Valkyrie line found in tail on startup.")

                offset = end_offset
                initialized = True
                sizes_prev = sizes_now
                save_state(current_path, offset, initialized, last_hash, sizes_prev)
                print("First run: started live from the end.")
                time.sleep(POLL_INTERVAL)
                continue

            # Normal polling
            new_offset, lines, size = fetch_new_lines(current_path, offset)

            if size is not None:
                print(f"Heartbeat: file={base} size={size} offset={offset}->{new_offset} new_lines={len(lines)}")
            else:
                print(f"Heartbeat: file={base} offset={offset}->{new_offset} new_lines={len(lines)} (SIZE n/a)")

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
            sizes_prev = sizes_now
            save_state(current_path, offset, initialized, last_hash, sizes_prev)

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()