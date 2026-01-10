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

# Throttling (important to avoid 429 during first-run backlog)
MAX_SEND_PER_POLL = int(os.getenv("MAX_SEND_PER_POLL", "10"))  # per loop
SEND_DELAY_SECONDS = float(os.getenv("SEND_DELAY_SECONDS", "0.4"))  # spacing

# State persistence (Railway disk is ephemeral sometimes, but still helps within a running container)
STATE_FILE = os.getenv("STATE_FILE", "tribe_forwarder_state.json").strip()

# Dedupe size (avoid re-sending same line)
DEDUPE_CACHE_SIZE = int(os.getenv("DEDUPE_CACHE_SIZE", "4000"))

# If you truly want *everything* on first run, keep this True.
SEND_BACKLOG_ON_FIRST_RUN = os.getenv("SEND_BACKLOG_ON_FIRST_RUN", "true").lower() in ("1", "true", "yes")

# If you want to ignore some noisy logs
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

# Example lines:
# [2026.01.10-08.18.26:427][243]2026.01.10_08.18.26: Tribe Valkyrie, ID 123...: Day 216, 16:53:34: <RichColor ...>Sir Magnus claimed 'Roan Pinto - Lvl 150 (Roan Pinto)'!</>)
DAY_TIME_RE = re.compile(r"Day\s+(\d+),\s+(\d{1,2}:\d{2}:\d{2})\s*:\s*(.+)$")
RICHCOLOR_RE = re.compile(r"<\s*RichColor[^>]*>", re.IGNORECASE)
TAG_CLOSE_RE = re.compile(r"</\s*>", re.IGNORECASE)

def normalize_line(raw: str) -> str:
    """
    Removes Ark RichColor tags and trailing junk like </> ), !), !</> etc.
    Also trims extra closing parentheses at the end.
    """
    s = raw.strip()

    # Remove RichColor open + close markers
    s = RICHCOLOR_RE.sub("", s)
    s = TAG_CLOSE_RE.sub("", s)

    # Remove the common trailing garbage you showed: </>), </>), !), !</>), etc.
    # Do this gently from the end.
    s = re.sub(r"\s*<\s*/\s*>\s*\)*\s*$", "", s)     # "</>)"
    s = re.sub(r"\s*!\s*\)*\s*$", "", s)            # "!)" or "!))"
    s = re.sub(r"\s*\)+\s*$", "", s)                # trailing "))" etc

    # Also sometimes lines end with "'!)" inside quotes—leave inner punctuation alone.
    return s.strip()

def extract_display_text(line: str) -> Optional[str]:
    """
    Return: "Day XXX, HH:MM:SS - message"
    Only if TARGET_TRIBE matches and a Day/time stamp is found.
    """
    if TARGET_TRIBE.lower() not in line.lower():
        return None

    cleaned = normalize_line(line)

    # Find "Day N, HH:MM:SS: rest..."
    m = DAY_TIME_RE.search(cleaned)
    if not m:
        return None

    day = m.group(1)
    t = m.group(2)
    rest = m.group(3).strip()

    # Remove leading "Tribe Valkyrie, ID..., " if it leaks into rest
    # (Sometimes it's before Day, sometimes after; this keeps output clean)
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
    return 0x95A5A6      # grey

def make_webhook_payload(text: str) -> dict:
    return {
        "embeds": [
            {
                "description": text,
                "color": color_for_text(text),
            }
        ]
    }

# ============================================================
# STATE (offsets + dedupe)
# ============================================================

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"offsets": {}, "dedupe": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "offsets" not in data:
            data["offsets"] = {}
        if "dedupe" not in data:
            data["dedupe"] = []
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

    # Rate limit handling
    if r.status_code == 429:
        try:
            data = r.json()
            retry_after = float(data.get("retry_after", 1.0))
        except Exception:
            retry_after = 1.0
        time.sleep(max(retry_after, 0.25))
        return False, f"Discord webhook error 429 (rate limited). Slept {retry_after}s."

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
    """
    Some servers reject SIZE in ASCII mode. We force TYPE I then SIZE.
    """
    try:
        ftp.sendcmd("TYPE I")
        s = ftp.size(path)
        if s is None:
            return None
        return int(s)
    except Exception:
        return None

def ftp_read_from_offset(ftp: ftplib.FTP, path: str, offset: int) -> bytes:
    """
    Read remote file starting at 'offset' using REST + RETR in binary mode.
    """
    ftp.sendcmd("TYPE I")
    chunks: List[bytes] = []

    def cb(data: bytes):
        chunks.append(data)

    # REST + RETR (ftplib supports rest=)
    ftp.retrbinary(f"RETR {path}", cb, rest=offset)
    return b"".join(chunks)

# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print(f"Polling every {POLL_INTERVAL}s")
    print(f"Filtering: {TARGET_TRIBE}")
    print(f"SEND_BACKLOG_ON_FIRST_RUN={SEND_BACKLOG_ON_FIRST_RUN}")

    state = load_state()
    offsets: Dict[str, int] = state.get("offsets", {})
    dedupe_list = state.get("dedupe", [])
    dedupe = deque(dedupe_list, maxlen=DEDUPE_CACHE_SIZE)
    dedupe_set = set(dedupe_list)

    last_any_sent_ts = time.time()
    last_heartbeat_ts = 0.0

    # backlog mode = if state has no offsets yet
    first_run = (len(offsets) == 0)

    while True:
        sent_this_loop = 0
        new_found_this_loop = 0

        try:
            ftp = ftp_connect()
            logs_dir = find_logs_dir(ftp)
            log_files = list_log_files(ftp, logs_dir)

            if not log_files:
                print(f"No .log files found in directory: {logs_dir}")
                ftp.quit()
                time.sleep(POLL_INTERVAL)
                continue

            # On first run with backlog enabled, start offsets at 0 for all log files
            if first_run and SEND_BACKLOG_ON_FIRST_RUN:
                for p in log_files:
                    offsets.setdefault(p, 0)

            # Otherwise, for any newly discovered file, start at its end (don’t spam old logs)
            for p in log_files:
                if p not in offsets:
                    sz = ftp_size_binary(ftp, p) or 0
                    offsets[p] = sz

            # Process each file (new bytes only)
            for p in log_files:
                if sent_this_loop >= MAX_SEND_PER_POLL:
                    break

                current_size = ftp_size_binary(ftp, p)
                if current_size is None:
                    # If size fails, try reading from offset anyway (may still work)
                    current_size = offsets.get(p, 0)

                off = offsets.get(p, 0)

                # Handle truncation / rotation
                if current_size < off:
                    off = 0

                if current_size == off:
                    continue

                data = ftp_read_from_offset(ftp, p, off)
                offsets[p] = current_size

                # Decode
                text = data.decode("utf-8", errors="ignore")
                lines = [ln for ln in text.splitlines() if ln.strip()]
                if not lines:
                    continue

                # Find matching tribe lines (convert to short display)
                matching: List[str] = []
                for ln in lines:
                    disp = extract_display_text(ln)
                    if disp:
                        matching.append(disp)

                if not matching:
                    continue

                new_found_this_loop += len(matching)

                # For backlog: send ALL (but still obey MAX_SEND_PER_POLL per loop, so it drains over time)
                # For normal operation: send ALL new matching lines too (not just most recent)
                for disp in matching:
                    sig = line_sig(disp)
                    if sig in dedupe_set:
                        continue

                    ok, msg = webhook_post(make_webhook_payload(disp))
                    if not ok:
                        print(f"Error: {msg}")
                        # If rate limited, webhook_post already slept; continue carefully
                    else:
                        sent_this_loop += 1
                        last_any_sent_ts = time.time()

                    # Dedupe record regardless (prevents repeats if Nitrado repeats lines)
                    dedupe.append(sig)
                    dedupe_set.add(sig)
                    if len(dedupe_set) > DEDUPE_CACHE_SIZE:
                        # rebuild set from deque occasionally
                        dedupe_set = set(dedupe)

                    save_state({"offsets": offsets, "dedupe": list(dedupe)})

                    if sent_this_loop >= MAX_SEND_PER_POLL:
                        break

                    time.sleep(SEND_DELAY_SECONDS)

            ftp.quit()

            # First run backlog completion:
            if first_run:
                if SEND_BACKLOG_ON_FIRST_RUN:
                    # We'll consider "first run done" once we have offsets for all files and we processed at least one loop.
                    # After this, newly discovered files won't backfill old content.
                    first_run = False
                    print("First run backlog mode complete (now watching only new appended logs).")
                else:
                    first_run = False

        except Exception as e:
            print(f"Error: {e}")

        # Heartbeat every HEARTBEAT_MINUTES if nothing new has been sent recently
        now = time.time()
        hb_interval = HEARTBEAT_MINUTES * 60
        if HEARTBEAT_MINUTES > 0 and (now - last_heartbeat_ts) >= hb_interval:
            # Only post heartbeat if we didn't send anything since last heartbeat window
            if now - last_any_sent_ts >= hb_interval:
                ok, msg = webhook_post(make_webhook_payload("No new logs since last check."))
                if not ok:
                    print(f"Error: {msg}")
            last_heartbeat_ts = now

        # Console status
        if new_found_this_loop > 0:
            print(f"Found {new_found_this_loop} matching lines; sent {sent_this_loop} this loop (cap {MAX_SEND_PER_POLL}).")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()