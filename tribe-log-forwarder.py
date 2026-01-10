import os
import time
import json
import hashlib
import logging
from typing import Deque, Set
from collections import deque

import requests
from ftplib import FTP, error_perm

# ----------------------------
# Config / Env
# ----------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

# IMPORTANT: Set this to the Valkyrie tribe log file on your server, e.g.
# arksa/ShooterGame/Saved/Logs/TribeLog_1238525433.log
FTP_REMOTE_PATH = os.getenv("FTP_REMOTE_PATH")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "5"))
READ_CHUNK_BYTES = int(os.getenv("READ_CHUNK_BYTES", "262144"))  # 256KB per poll is plenty

# Dedup controls
SEEN_CACHE_FILE = os.getenv("SEEN_CACHE_FILE", "seen_cache.json")
SEEN_CACHE_MAX = int(os.getenv("SEEN_CACHE_MAX", "4000"))  # how many message fingerprints to remember
IN_MEMORY_SEEN_MAX = int(os.getenv("IN_MEMORY_SEEN_MAX", "1500"))  # for quick checks

# ----------------------------
# Logging
# ----------------------------

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tribe-log-forwarder")


def require_env():
    missing = []
    for k in ["FTP_HOST", "FTP_USER", "FTP_PASS", "FTP_REMOTE_PATH", "DISCORD_WEBHOOK_URL"]:
        if not os.getenv(k):
            missing.append(k)
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


# ----------------------------
# Helpers
# ----------------------------

def stable_hash(s: str) -> str:
    """
    Make a stable fingerprint for a log line.
    Using sha1 is fine for dedup (not security).
    """
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()


def load_seen_cache(path: str, max_items: int) -> Deque[str]:
    """
    Load a bounded deque of hashes from disk.
    """
    if not os.path.exists(path):
        return deque(maxlen=max_items)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            d = deque((str(x) for x in data), maxlen=max_items)
            return d
    except Exception as e:
        log.warning("Could not load seen cache (%s). Starting fresh. Error: %s", path, e)
    return deque(maxlen=max_items)


def save_seen_cache(path: str, seen_deque: Deque[str]) -> None:
    """
    Persist the seen hashes to disk.
    """
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(list(seen_deque), f)
    except Exception as e:
        log.warning("Could not save seen cache (%s). Error: %s", path, e)


def connect_ftp() -> FTP:
    ftp = FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.set_pasv(True)
    return ftp


def ftp_download_tail(ftp: FTP, remote_path: str, start_at: int) -> tuple[bytes, int]:
    """
    Download from remote file starting at byte offset.
    Returns (data_bytes, new_offset).
    """
    data_parts = []

    def _cb(chunk: bytes):
        data_parts.append(chunk)

    try:
        # retrbinary supports REST for resume
        ftp.voidcmd("TYPE I")  # binary mode
        ftp.sendcmd(f"REST {start_at}")
        ftp.retrbinary(f"RETR {remote_path}", _cb, blocksize=65536)
    except error_perm as e:
        # Some FTP servers don't support REST for certain files or permissions
        raise
    data = b"".join(data_parts)
    return data, start_at + len(data)


def discord_send(webhook_url: str, content: str) -> None:
    # Discord webhook content limit is 2000 chars.
    # Split if needed.
    if not content:
        return

    chunks = []
    while len(content) > 2000:
        chunks.append(content[:2000])
        content = content[2000:]
    chunks.append(content)

    for c in chunks:
        r = requests.post(webhook_url, json={"content": c}, timeout=15)
        if r.status_code >= 300:
            raise RuntimeError(f"Discord webhook failed: {r.status_code} {r.text}")


# ----------------------------
# Main tail loop
# ----------------------------

def main():
    require_env()

    # Load persisted dedup hashes
    seen_deque = load_seen_cache(SEEN_CACHE_FILE, SEEN_CACHE_MAX)
    seen_set: Set[str] = set(seen_deque)  # quick membership checks

    # In-memory recent (extra fast)
    recent_seen: Deque[str] = deque(maxlen=IN_MEMORY_SEEN_MAX)
    for h in list(seen_deque)[-IN_MEMORY_SEEN_MAX:]:
        recent_seen.append(h)

    log.info("Starting tribe-log-forwarder")
    log.info("FTP_HOST=%s FTP_PORT=%s", FTP_HOST, FTP_PORT)
    log.info("FTP_REMOTE_PATH=%s", FTP_REMOTE_PATH)
    log.info("Polling every %ss", POLL_SECONDS)

    offset = 0
    partial_line = b""
    last_save_time = 0.0

    while True:
        ftp = None
        try:
            ftp = connect_ftp()

            # If first run, start at EOF so we don't dump the entire history.
            if offset == 0:
                try:
                    size = ftp.size(FTP_REMOTE_PATH)
                    if size is None:
                        size = 0
                    offset = int(size)
                    log.info("Initial offset set to EOF: %d bytes", offset)
                except Exception as e:
                    log.warning("Could not get remote file size (starting at 0). Error: %s", e)
                    offset = 0

            # Download any new bytes
            data, new_offset = ftp_download_tail(ftp, FTP_REMOTE_PATH, offset)

            # Some servers may return the whole file even with REST (rare).
            # If that happens, cap how much we process per cycle.
            if len(data) > READ_CHUNK_BYTES:
                data = data[-READ_CHUNK_BYTES:]
                # new_offset still correct, but we only parse last chunk

            offset = new_offset

            if not data:
                time.sleep(POLL_SECONDS)
                continue

            # Combine with any leftover partial line
            data = partial_line + data

            # Split into lines safely
            lines = data.splitlines(keepends=False)

            # If the data didn't end with a newline, the last line may be partial.
            # Keep it for next poll.
            if data and not data.endswith(b"\n") and not data.endswith(b"\r"):
                partial_line = lines[-1] if lines else b""
                lines = lines[:-1] if lines else []
            else:
                partial_line = b""

            # Decode and send new unique lines
            for raw in lines:
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                h = stable_hash(line)

                # Dedup check (fast + persistent)
                if h in seen_set:
                    continue

                # Mark as seen
                seen_deque.append(h)
                seen_set.add(h)
                recent_seen.append(h)

                # Keep seen_set bounded to prevent growth
                # (when deque drops old, remove from set)
                while len(seen_set) > len(seen_deque):
                    # This shouldn't really happen, but guard anyway
                    seen_set = set(seen_deque)

                try:
                    discord_send(DISCORD_WEBHOOK_URL, line)
                    log.info("Sent: %s", line[:120])
                except Exception as e:
                    log.error("Failed sending to Discord. Error: %s", e)

            # Periodically save cache (every ~30s)
            now = time.time()
            if now - last_save_time > 30:
                save_seen_cache(SEEN_CACHE_FILE, seen_deque)
                last_save_time = now

            time.sleep(POLL_SECONDS)

        except error_perm as e:
            log.error("FTP permission/error: %s", e)
            time.sleep(10)

        except Exception as e:
            log.error("Loop error: %s", e)
            time.sleep(10)

        finally:
            try:
                if ftp:
                    ftp.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()