import os
import re
import time
import json
import ftplib
import hashlib
import requests
from collections import deque
from typing import Dict, List, Tuple, Optional

# ============================================================
# ENV CONFIG
# ============================================================

FTP_HOST = os.getenv("FTP_HOST", "").strip()
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER", "").strip()
FTP_PASS = os.getenv("FTP_PASS", "").strip()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# Logs directory on Nitrado (AUTO-detect if not set)
FTP_LOG_DIR = os.getenv("FTP_LOG_DIR", "").strip()

# Filter
TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie").strip()

# Polling
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "10"))

# Heartbeat ("No new logs since last check.")
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "10"))

# Throttling (avoid 429 during backlog)
MAX_SEND_PER_POLL = int(os.getenv("MAX_SEND_PER_POLL", "10"))
SEND_DELAY_SECONDS = float(os.getenv("SEND_DELAY_SECONDS", "0.4"))

# State persistence
STATE_FILE = os.getenv("STATE_FILE", "tribe_forwarder_state.json").strip()

# Dedupe size
DEDUPE_CACHE_SIZE = int(os.getenv("DEDUPE_CACHE_SIZE", "4000"))

# Backlog controls
SEND_BACKLOG_ON_FIRST_RUN = os.getenv("SEND_BACKLOG_ON_FIRST_RUN", "true").lower() in ("1", "true", "yes")
FORCE_BACKLOG = os.getenv("FORCE_BACKLOG", "0").lower() in ("1", "true", "yes")  # <--- NEW

EXCLUDE_KEYWORDS = ("backup", "failedwater", "failed", ".crash", "crashstack")

# ============================================================
# VALIDATION
# ============================================================

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

# ============================================================
# REGEX / CLEANUP
# ============================================================

DAY_TIME_RE = re.compile(r"Day\s+(\d+),\s+(\d{1,2}:\d{2}:\d{2})\s*:\s*(.+)$")
RICHCOLOR_RE = re.compile(r"<\s*RichColor[^>]*>", re.IGNORECASE)
TAG_CLOSE_RE = re.compile(r"</\s*>", re.IGNORECASE)

def normalize_line(raw: str) -> str:
    s = raw.strip()
    s = RICHCOLOR_RE.sub("", s)
    s = TAG_CLOSE_RE.sub("", s)

    # strip common end junk: </>), !), )), etc
    s = re.sub(r"\s*<\s*/\s*>\s*\)*\s*$", "", s)   # </>)
    s = re.sub(r"\s*!\s*\)*\s*$", "", s)          # !)
    s = re.sub(r"\s*\)+\s*$", "", s)              # trailing ))
    return s.strip()

def extract_display_text(line: str) -> Optional[str]:
    if TARGET_TRIBE.lower() not in line.lower():
        return None

    cleaned = normalize_line(line)
    m = DAY_TIME_RE.search(cleaned)
    if not m:
        return None

    day = m.group(1)
    t = m.group(2)
    rest = m.group(3).strip()

    # keep it clean
    rest = re.sub(r"^Tribe\s+[^:]+:\s*", "", rest, flags=re.IGNORECASE).strip()
    return f"Day {day}, {t} - {rest}"

def color_for_text(text: str) -> int:
    lower = text.lower()
    if "claimed" in lower or "unclaimed" in lower or "claiming" in lower:
        return 0x9B59B6  # purple
    if "tamed" in lower or "taming" in lower:
        return 0x2ECC71  # green
    if "was killed" in lower or "killed" in lower or "died" in lower or "starved" in lower:
        return 0xE74C3C  # red
    if "demolished" in lower or "destroyed" in lower:
        return 0xF1C40F  # yellow
    return 0x95A5A6

def make_webhook_payload(text: str) -> dict:
    return {"embeds": [{"description": text, "color": color_for_text(text)}]}

# ============================================================
# STATE
# ============================================================

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"offsets": {}, "dedupe": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("offsets", {})
        data.setdefault("dedupe", [])
        return data
    except Exception:
        return {"offsets": {}, "dedupe": []}

def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)

def line_sig(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

# ============================================================
# DISCORD WEBHOOK (rate limit aware)
# ============================================================

def webhook_post(payload: dict) -> Tuple[bool, str]:
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
    except Exception as e:
        return False, f"Webhook request failed: {e}"

    if r.status_code == 204 or (200 <= r.status_code < 300):
        return True, "ok"

    if r.status_code == 429:
        try:
            data = r.json()
            retry_after = float(data.get("retry_after", 1.0))
        except Exception:
            retry_after = 1.0
        time.sleep(max(retry_after, 0.25))
        return False, f"Discord webhook 429 (rate limited). Slept {retry_after}s."

    return False, f"Discord webhook error {r.status_code}: {r.text}"

# ============================================================
# FTP HELPERS
# ============================================================

CANDIDATE_LOG_DIRS = [
    FTP_LOG_DIR,
    "arksa/ShooterGame/Saved/Logs",
    "ShooterGame/Saved/Logs",
]

def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=20)
    ftp.login(FTP_USER, FTP_PASS)
    return ftp

def find_logs_dir(ftp: ftplib.FTP) -> str:
    for d in CANDIDATE_LOG_DIRS:
        if not d:
            continue
        try:
            ftp.cwd("/")
            ftp.cwd(d)
            return d
        except Exception:
            continue
    raise RuntimeError("Could not find Logs directory. Tried: " + ", ".join([d for d in CANDIDATE_LOG_DIRS if d]))

def list_log_files(ftp: ftplib.FTP, logs_dir: str) -> List[str]:
    ftp.cwd("/")
    ftp.cwd(logs_dir)
    names = ftp.nlst()

    out = []
    for n in names:
        nl = n.lower()
        if not nl.endswith(".log"):
            continue
        if any(k in nl for k in EXCLUDE_KEYWORDS):
            continue
        out.append(f"{logs_dir}/{n}")
    return sorted(out)

def ftp_size_binary(ftp: ftplib.FTP, path: str) -> Optional[int]:
    try:
        ftp.sendcmd("TYPE I")
        s = ftp.size(path)
        return int(s) if s is not None else None
    except Exception:
        return None

def ftp_read_from_offset(ftp: ftplib.FTP, path: str, offset: int) -> bytes:
    ftp.sendcmd("TYPE I")
    chunks: List[bytes] = []

    def cb(data: bytes):
        chunks.append(data)

    ftp.retrbinary(f"RETR {path}", cb, rest=offset)
    return b"".join(chunks)

# ============================================================
# MAIN
# ============================================================

def main():
    print(f"Polling every {POLL_INTERVAL}s")
    print(f"Filtering: {TARGET_TRIBE}")
    print(f"SEND_BACKLOG_ON_FIRST_RUN={SEND_BACKLOG_ON_FIRST_RUN}")
    print(f"FORCE_BACKLOG={FORCE_BACKLOG}")

    state = load_state()

    # If forcing backlog, wipe state so it truly behaves like first run.
    if FORCE_BACKLOG:
        state = {"offsets": {}, "dedupe": []}
        try:
            if os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
        except Exception:
            pass
        print("FORCE_BACKLOG enabled: state cleared; backlog will be resent.")

    offsets: Dict[str, int] = state.get("offsets", {})
    dedupe_list = state.get("dedupe", [])
    dedupe = deque(dedupe_list, maxlen=DEDUPE_CACHE_SIZE)
    dedupe_set = set(dedupe_list)

    last_any_sent_ts = time.time()
    last_heartbeat_ts = 0.0

    first_run = (len(offsets) == 0)

    while True:
        sent_this_loop = 0
        found_this_loop = 0

        try:
            ftp = ftp_connect()
            logs_dir = find_logs_dir(ftp)
            log_files = list_log_files(ftp, logs_dir)

            print(f"Logs dir: {logs_dir} | Files: {len(log_files)} | first_run={first_run}")

            if not log_files:
                ftp.quit()
                time.sleep(POLL_INTERVAL)
                continue

            # Backlog behavior
            if first_run and SEND_BACKLOG_ON_FIRST_RUN:
                for p in log_files:
                    offsets.setdefault(p, 0)
                print("Backlog mode: reading from start of log files (offset=0).")

            # For newly discovered files after first run, start at end
            if not first_run:
                for p in log_files:
                    if p not in offsets:
                        sz = ftp_size_binary(ftp, p) or 0
                        offsets[p] = sz

            for p in log_files:
                if sent_this_loop >= MAX_SEND_PER_POLL:
                    break

                current_size = ftp_size_binary(ftp, p)
                if current_size is None:
                    current_size = offsets.get(p, 0)

                off = offsets.get(p, 0)

                # rotation/truncation
                if current_size < off:
                    off = 0

                if current_size == off:
                    continue

                data = ftp_read_from_offset(ftp, p, off)
                offsets[p] = current_size

                text = data.decode("utf-8", errors="ignore")
                lines = [ln for ln in text.splitlines() if ln.strip()]

                matching: List[str] = []
                for ln in lines:
                    disp = extract_display_text(ln)
                    if disp:
                        matching.append(disp)

                if not matching:
                    continue

                found_this_loop += len(matching)

                for disp in matching:
                    if sent_this_loop >= MAX_SEND_PER_POLL:
                        break

                    sig = line_sig(disp)
                    if sig in dedupe_set:
                        continue

                    ok, msg = webhook_post(make_webhook_payload(disp))
                    if not ok:
                        print(f"Error: {msg}")
                    else:
                        sent_this_loop += 1
                        last_any_sent_ts = time.time()

                    dedupe.append(sig)
                    dedupe_set.add(sig)
                    if len(dedupe_set) > DEDUPE_CACHE_SIZE:
                        dedupe_set = set(dedupe)

                    save_state({"offsets": offsets, "dedupe": list(dedupe)})

                    time.sleep(SEND_DELAY_SECONDS)

            ftp.quit()

            if first_run and SEND_BACKLOG_ON_FIRST_RUN:
                # After first successful scan, switch to live mode
                first_run = False
                save_state({"offsets": offsets, "dedupe": list(dedupe)})
                print("Backlog complete. Now watching only newly appended logs.")

        except Exception as e:
            print(f"Error: {e}")

        # Heartbeat
        now = time.time()
        hb_interval = HEARTBEAT_MINUTES * 60
        if HEARTBEAT_MINUTES > 0 and (now - last_heartbeat_ts) >= hb_interval:
            if now - last_any_sent_ts >= hb_interval:
                ok, msg = webhook_post(make_webhook_payload("No new logs since last check."))
                if not ok:
                    print(f"Error: {msg}")
            last_heartbeat_ts = now

        if found_this_loop or sent_this_loop:
            print(f"Found {found_this_loop} matching lines; sent {sent_this_loop} this loop (cap {MAX_SEND_PER_POLL}).")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()