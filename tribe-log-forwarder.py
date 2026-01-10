import os
import time
import json
import hashlib
import logging
from collections import deque
from ftplib import FTP, error_perm
import requests

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tribe-log-forwarder")

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")
FTP_REMOTE_PATH = os.getenv("FTP_REMOTE_PATH")  # must point to the exact file
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "5"))

SEEN_CACHE_FILE = os.getenv("SEEN_CACHE_FILE", "seen_cache.json")
SEEN_CACHE_MAX = int(os.getenv("SEEN_CACHE_MAX", "4000"))


def require_env():
    missing = []
    for k in ["FTP_HOST", "FTP_USER", "FTP_PASS", "FTP_REMOTE_PATH", "DISCORD_WEBHOOK_URL"]:
        if not os.getenv(k):
            missing.append(k)
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


def stable_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()


def load_seen_cache(path: str, max_items: int) -> deque:
    if not os.path.exists(path):
        return deque(maxlen=max_items)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return deque((str(x) for x in data), maxlen=max_items)
    except Exception as e:
        log.warning("Could not load seen cache (%s). Starting fresh. Error: %s", path, e)
    return deque(maxlen=max_items)


def save_seen_cache(path: str, seen_deque: deque) -> None:
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
    # Put server into binary mode so SIZE works on servers that require it
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass
    return ftp


def discord_send(webhook_url: str, content: str) -> None:
    if not content:
        return
    # Discord: 2000 char limit
    while content:
        chunk = content[:2000]
        content = content[2000:]
        r = requests.post(webhook_url, json={"content": chunk}, timeout=15)
        if r.status_code >= 300:
            raise RuntimeError(f"Discord webhook failed: {r.status_code} {r.text}")


def ftp_retr_from_offset(ftp: FTP, remote_path: str, start_at: int) -> tuple[bytes, int]:
    data_parts = []

    def _cb(chunk: bytes):
        data_parts.append(chunk)

    ftp.voidcmd("TYPE I")
    ftp.sendcmd(f"REST {start_at}")
    ftp.retrbinary(f"RETR {remote_path}", _cb, blocksize=65536)
    data = b"".join(data_parts)
    return data, start_at + len(data)


def ftp_try_debug_path(ftp: FTP, remote_path: str) -> None:
    """
    If RETR fails, log PWD and list likely directories to help you fix FTP_REMOTE_PATH.
    """
    try:
        pwd = ftp.pwd()
        log.error("FTP PWD is: %s", pwd)
    except Exception:
        pass

    # Try listing the logs folder based on the provided path
    parts = remote_path.replace("\\", "/").split("/")
    if len(parts) > 1:
        parent = "/".join(parts[:-1])
        try:
            log.error("Trying to list directory: %s", parent)
            items = ftp.nlst(parent)
            # show only first 50 to avoid huge logs
            log.error("Directory listing (first 50): %s", items[:50])
        except Exception as e:
            log.error("Could not list %s. Error: %s", parent, e)

    # Also try some common Ark locations relative to FTP root
    common = [
        "ShooterGame/Saved/Logs",
        "arksa/ShooterGame/Saved/Logs",
        "ARK/ShooterGame/Saved/Logs",
        "logs",
    ]
    for d in common:
        try:
            items = ftp.nlst(d)
            log.error("Found directory '%s' (first 30): %s", d, items[:30])
        except Exception:
            continue


def main():
    require_env()

    seen_deque = load_seen_cache(SEEN_CACHE_FILE, SEEN_CACHE_MAX)
    seen_set = set(seen_deque)

    log.info("Starting tribe-log-forwarder")
    log.info("Polling every %ss", POLL_SECONDS)

    offset = 0
    partial = b""
    last_save = 0.0

    while True:
        ftp = None
        try:
            ftp = connect_ftp()

            # First run: try to start at EOF (so we don't spam history)
            if offset == 0:
                try:
                    ftp.voidcmd("TYPE I")  # ensure binary before SIZE
                    size = ftp.size(FTP_REMOTE_PATH)
                    if size is not None:
                        offset = int(size)
                        log.info("Initial offset set to EOF: %d bytes", offset)
                    else:
                        log.warning("FTP SIZE returned None; starting at 0")
                except Exception as e:
                    log.warning("Could not get remote file size (starting at 0). Error: %s", e)

            # Read new bytes
            try:
                data, offset = ftp_retr_from_offset(ftp, FTP_REMOTE_PATH, offset)
            except error_perm as e:
                # This is your current error (No such file or dir)
                log.error("FTP permission/error: %s", e)
                ftp_try_debug_path(ftp, FTP_REMOTE_PATH)
                time.sleep(10)
                continue

            if not data:
                time.sleep(POLL_SECONDS)
                continue

            data = partial + data
            lines = data.splitlines(keepends=False)

            if data and not data.endswith(b"\n") and not data.endswith(b"\r"):
                partial = lines[-1] if lines else b""
                lines = lines[:-1] if lines else []
            else:
                partial = b""

            for raw in lines:
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                h = stable_hash(line)
                if h in seen_set:
                    continue  # <-- send once

                seen_deque.append(h)
                seen_set.add(h)

                try:
                    discord_send(DISCORD_WEBHOOK_URL, line)
                    log.info("Sent: %s", line[:120])
                except Exception as e:
                    log.error("Failed sending to Discord. Error: %s", e)

            now = time.time()
            if now - last_save > 30:
                save_seen_cache(SEEN_CACHE_FILE, seen_deque)
                last_save = now

            time.sleep(POLL_SECONDS)

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