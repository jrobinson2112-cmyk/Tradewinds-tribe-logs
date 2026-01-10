import os
import time
import json
import ftplib
import logging
import hashlib
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Tuple
import urllib.request

# ----------------------------
# Config / Env
# ----------------------------

LOG = logging.getLogger("tribe-log-forwarder")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

REQUIRED_ENV = [
    "FTP_HOST",
    "FTP_USER",
    "FTP_PASS",
    "DISCORD_WEBHOOK_URL",
]

FTP_HOST = os.getenv("FTP_HOST", "").strip()
FTP_USER = os.getenv("FTP_USER", "").strip()
FTP_PASS = os.getenv("FTP_PASS", "").strip()
FTP_PORT = int(os.getenv("FTP_PORT", "21").strip() or "21")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# Filter: only forward lines containing this (case-insensitive)
TRIBE_FILTER = os.getenv("TRIBE_FILTER", "Tribe Valkyrie").strip()

# Either provide an exact file (recommended if you know it),
# OR leave blank to auto-pick newest TribeLog*.log from FTP_LOG_DIR
FTP_REMOTE_PATH = os.getenv("FTP_REMOTE_PATH", "").strip()
FTP_LOG_DIR = os.getenv("FTP_LOG_DIR", "arksa/ShooterGame/Saved/Logs").strip().strip("/")

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "5").strip() or "5")

# State files (Railway disk is ephemeral unless you add a volume; still helps during runtime)
STATE_FILE = os.getenv("STATE_FILE", "/tmp/tribe_log_state.json").strip()
DEDUP_MAX = int(os.getenv("DEDUP_MAX", "5000").strip() or "5000")  # number of line-hashes to remember

# Discord embed limits: description max 4096; keep it safe
MAX_LINE_LEN = int(os.getenv("MAX_LINE_LEN", "3500").strip() or "3500")

DISCORD_USERNAME = os.getenv("DISCORD_USERNAME", "Valkyrie Tribe Logs").strip()

# ----------------------------
# Discord colours (embeds)
# ----------------------------
# Discord embed color is an integer (0xRRGGBB)
COLOR_PURPLE = 0x9B59B6
COLOR_GREEN  = 0x2ECC71
COLOR_RED    = 0xE74C3C
COLOR_YELLOW = 0xF1C40F
COLOR_GREY   = 0x95A5A6

# ----------------------------
# Helpers
# ----------------------------

def require_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def safe_truncate(s: str, max_len: int) -> str:
    s = s.strip("\n\r")
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"

def clean_ark_markup(line: str) -> str:
    """
    Removes common ARK <RichColor ...>...</> and other simple tags
    so the message looks cleaner in Discord.
    """
    # Remove RichColor tags (very simple strip; good enough for typical tribe logs)
    # Examples:
    # <RichColor Color="1, 0, 1, 1">Text!</>
    out = line
    while True:
        start = out.find("<RichColor")
        if start == -1:
            break
        end = out.find(">", start)
        if end == -1:
            break
        out = out[:start] + out[end+1:]
    out = out.replace("</>", "")
    out = out.replace("<", "‹").replace(">", "›")  # avoid Discord interpreting anything weird
    return out

def categorize_color(line: str) -> int:
    """
    Decide colour based on content.
    Adjust keywords here if you want.
    """
    l = line.lower()

    # claiming (purple)
    if " claimed " in l or " claimed'" in l or " claimed\"" in l:
        return COLOR_PURPLE

    # taming (green) - common tribe log phrases
    if "tamed" in l or "taming" in l:
        return COLOR_GREEN

    # deaths (red)
    if " was killed" in l or " was killed!" in l or "killed!" in l or "starved to death" in l or "died" in l:
        return COLOR_RED

    # demolished/destroyed (yellow)
    if "demolish" in l or "demolished" in l or "destroyed" in l:
        return COLOR_YELLOW

    return COLOR_GREY

def discord_post_embed(description: str, color: int) -> None:
    payload = {
        "username": DISCORD_USERNAME,
        "embeds": [{
            "description": description,
            "color": color,
            "timestamp": utc_now_iso(),
        }]
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            _ = resp.read()
    except urllib.error.HTTPError as e:
        # Handle rate limits
        if e.code == 429:
            try:
                body = e.read().decode("utf-8", errors="ignore")
                j = json.loads(body) if body else {}
                retry_after = float(j.get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            LOG.warning("Discord rate limited. Sleeping %.2fs", retry_after)
            time.sleep(retry_after)
            # retry once
            with urllib.request.urlopen(req, timeout=20) as resp:
                _ = resp.read()
        else:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            LOG.error("Discord webhook error %s: %s", e.code, body[:500])
            raise

# ----------------------------
# State (offset + dedup)
# ----------------------------

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {
            "remote_path": "",
            "offset": 0,
            "partial": "",
            "dedup": [],
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        if "dedup" not in s or not isinstance(s["dedup"], list):
            s["dedup"] = []
        return s
    except Exception:
        return {
            "remote_path": "",
            "offset": 0,
            "partial": "",
            "dedup": [],
        }

def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)

# ----------------------------
# FTP logic
# ----------------------------

def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=20)
    ftp.login(FTP_USER, FTP_PASS)
    # IMPORTANT: use binary mode so SIZE works on servers that reject ASCII SIZE
    ftp.voidcmd("TYPE I")
    return ftp

def ftp_list_dir(ftp: ftplib.FTP, path: str) -> list:
    """
    Returns list of filenames (not full paths) in a directory.
    """
    files = []
    ftp.cwd("/")
    for part in path.split("/"):
        if part:
            ftp.cwd(part)

    try:
        # NLST gives just names
        files = ftp.nlst()
    except ftplib.error_perm:
        # some servers require LIST parsing
        lines = []
        ftp.retrlines("LIST", lines.append)
        for ln in lines:
            # naive: last column is filename
            parts = ln.split()
            if parts:
                files.append(parts[-1])
    return files

def pick_newest_tribelog(ftp: ftplib.FTP, log_dir: str) -> str:
    """
    Choose a TribeLog*.log file from log_dir. Prefer newest by mdtm if available.
    """
    names = ftp_list_dir(ftp, log_dir)
    candidates = [n for n in names if n.lower().startswith("tribelog") and n.lower().endswith(".log")]

    if not candidates:
        # fallback: anything containing tribelog
        candidates = [n for n in names if "tribelog" in n.lower() and n.lower().endswith(".log")]

    if not candidates:
        raise RuntimeError(f"No TribeLog*.log files found in /{log_dir}")

    def get_mdtm(name: str) -> str:
        try:
            resp = ftp.sendcmd(f"MDTM {name}")  # e.g. "213 20260110074012"
            return resp.split()[-1]
        except Exception:
            return ""

    # Try to sort by MDTM, otherwise by name
    with_mdtm = [(get_mdtm(n), n) for n in candidates]
    with_mdtm.sort(key=lambda t: (t[0] or "", t[1]))
    newest = with_mdtm[-1][1]
    return f"{log_dir}/{newest}"

def ftp_size(ftp: ftplib.FTP, remote_path: str) -> int:
    # Ensure TYPE I before SIZE (some servers require)
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass
    return ftp.size(remote_path)  # type: ignore

def ftp_read_from_offset(ftp: ftplib.FTP, remote_path: str, offset: int) -> bytes:
    """
    Read bytes from remote file starting at offset.
    """
    chunks = []
    def cb(data: bytes):
        chunks.append(data)
    ftp.voidcmd("TYPE I")
    ftp.retrbinary(f"RETR {remote_path}", cb, rest=offset)
    return b"".join(chunks)

# ----------------------------
# Main loop
# ----------------------------

def main() -> None:
    require_env()
    state = load_state()

    # Dedup store: remember hashes of lines we've already sent
    dedup = deque(state.get("dedup", []), maxlen=DEDUP_MAX)
    dedup_set = set(dedup)

    remote_path = FTP_REMOTE_PATH or state.get("remote_path", "").strip()
    offset = int(state.get("offset", 0) or 0)
    partial = state.get("partial", "") or ""

    LOG.info("Polling every %.1fs", POLL_SECONDS)
    LOG.info("Filter: %s", TRIBE_FILTER)

    while True:
        try:
            ftp = ftp_connect()
            try:
                # Determine which file to follow
                if not remote_path:
                    remote_path = pick_newest_tribelog(ftp, FTP_LOG_DIR)
                    LOG.info("Auto-selected tribe log: %s", remote_path)
                    # Reset when switching to a new file
                    offset = 0
                    partial = ""

                # If the server rotates logs, the newest file may change:
                # If FTP_REMOTE_PATH isn't set, re-check newest periodically and switch if changed.
                if not FTP_REMOTE_PATH:
                    newest = pick_newest_tribelog(ftp, FTP_LOG_DIR)
                    if newest != remote_path:
                        LOG.info("Detected new tribe log file. Switching: %s -> %s", remote_path, newest)
                        remote_path = newest
                        offset = 0
                        partial = ""

                # Check size; if file shrank (rotated), reset offset
                try:
                    size = ftp_size(ftp, remote_path)
                except Exception as e:
                    LOG.warning("Could not get remote file size (starting at 0). Error: %s", e)
                    size = 0

                if size < offset:
                    LOG.info("Remote file shrank/rotated. Resetting offset to 0.")
                    offset = 0
                    partial = ""

                if size > offset:
                    data = ftp_read_from_offset(ftp, remote_path, offset)
                    offset = size

                    text = data.decode("utf-8", errors="ignore")
                    if partial:
                        text = partial + text
                        partial = ""

                    # If last line is incomplete, keep it for next poll
                    if text and not text.endswith("\n"):
                        last_nl = text.rfind("\n")
                        if last_nl == -1:
                            partial = text
                            text = ""
                        else:
                            partial = text[last_nl+1:]
                            text = text[:last_nl+1]

                    # Process complete lines
                    for raw_line in text.splitlines():
                        line = raw_line.strip()
                        if not line:
                            continue

                        # Filter only the tribe you want
                        if TRIBE_FILTER.lower() not in line.lower():
                            continue

                        # DEDUP: hash on cleaned line
                        cleaned = clean_ark_markup(line)
                        line_hash = sha1_text(cleaned)

                        if line_hash in dedup_set:
                            continue  # already sent

                        # Mark as sent first (prevents duplicates if Discord call is slow and loop overlaps)
                        dedup.append(line_hash)
                        dedup_set.add(line_hash)
                        if len(dedup) == dedup.maxlen:
                            # rebuild set occasionally to drop old hashes
                            dedup_set = set(dedup)

                        # Send to Discord
                        color = categorize_color(cleaned)
                        msg = safe_truncate(cleaned, MAX_LINE_LEN)
                        discord_post_embed(msg, color)

                # Persist state each loop
                state = {
                    "remote_path": remote_path,
                    "offset": offset,
                    "partial": partial,
                    "dedup": list(dedup),
                }
                save_state(state)

            finally:
                try:
                    ftp.quit()
                except Exception:
                    try:
                        ftp.close()
                    except Exception:
                        pass

        except ftplib.error_perm as e:
            LOG.error("FTP permission/error: %s", e)
        except Exception as e:
            LOG.exception("Error: %s", e)

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()