import os
import time
import ftplib
import json
import re
import requests
from fnmatch import fnmatch

# ============================================================
# CONFIG (ENV VARS)
# ============================================================

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Can be a directory OR a file. If directory, we auto-pick newest tribe log.
FTP_LOG_PATH = os.getenv("FTP_LOG_PATH", "arksa/ShooterGame/Saved/Logs").rstrip("/")

# Make pattern flexible by default (works with TribeLog.log, TribeLog_123.log, etc)
LOG_PATTERN = os.getenv("LOG_PATTERN", "TribeLog*.log")

TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "10"))

STATE_FILE = "cursor.json"

# If FTP_LOG_PATH is a directory and no matches are found, we also try these:
FALLBACK_DIRS = [
    "arksa/ShooterGame/Saved/Logs",
    "arksa/ShooterGame/Saved/Logs/TribeLogs",
    "arksa/ShooterGame/Saved/TribeLogs",
]

# ============================================================
# VALIDATION
# ============================================================

missing = [k for k, v in {
    "FTP_HOST": FTP_HOST,
    "FTP_USER": FTP_USER,
    "FTP_PASS": FTP_PASS,
    "DISCORD_WEBHOOK_URL": DISCORD_WEBHOOK_URL,
}.items() if not v]

if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


# ============================================================
# DISCORD FORMAT HELPERS
# ============================================================

def clean_ark_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()

def format_log_line(line: str) -> dict:
    text = clean_ark_tags(line)
    lower = text.lower()

    color = 0x95A5A6  # default grey

    if "claimed" in lower or "claiming" in lower:
        color = 0x9B59B6  # purple
    elif "tamed" in lower or "taming" in lower:
        color = 0x2ECC71  # green
    elif "was killed" in lower or "killed" in lower or "died" in lower:
        color = 0xE74C3C  # red
    elif "demolished" in lower or "destroyed" in lower:
        color = 0xF1C40F  # yellow

    return {"embeds": [{"description": text[:4096], "color": color}]}

def send_to_discord(payload: dict):
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    if r.status_code >= 300:
        raise RuntimeError(f"Discord webhook error {r.status_code}: {r.text[:300]}")


# ============================================================
# STATE
# ============================================================

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"file": None, "offset": 0}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        return {"file": s.get("file"), "offset": int(s.get("offset", 0))}
    except Exception:
        return {"file": None, "offset": 0}

def save_state(file_path: str, offset: int):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"file": file_path, "offset": int(offset)}, f)


# ============================================================
# FTP HELPERS
# ============================================================

def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.set_pasv(True)
    return ftp

def ftp_is_directory(ftp: ftplib.FTP, path: str) -> bool:
    cur = ftp.pwd()
    try:
        ftp.cwd(path)
        ftp.cwd(cur)
        return True
    except Exception:
        try:
            ftp.cwd(cur)
        except Exception:
            pass
        return False

def ftp_list_files(ftp: ftplib.FTP, directory: str) -> list[str]:
    directory = directory.rstrip("/")
    try:
        items = ftp.nlst(directory)
        # Some servers return full paths, some return names; normalize:
        normalized = []
        for item in items:
            if "/" in item:
                normalized.append(item)
            else:
                normalized.append(f"{directory}/{item}")
        return normalized
    except Exception:
        # fallback: cwd + nlst()
        ftp.cwd(directory)
        names = ftp.nlst()
        return [f"{directory}/{n}" for n in names]

def ftp_mdtm(ftp: ftplib.FTP, path: str) -> str | None:
    try:
        resp = ftp.sendcmd(f"MDTM {path}")
        parts = resp.split()
        return parts[1].strip() if len(parts) >= 2 else None
    except Exception:
        return None

def pick_newest_matching(ftp: ftplib.FTP, directory: str, pattern: str) -> str | None:
    files = ftp_list_files(ftp, directory)
    matches = [p for p in files if fnmatch(os.path.basename(p), pattern)]
    if not matches:
        return None

    scored = []
    for p in matches:
        ts = ftp_mdtm(ftp, p) or ""
        scored.append((ts, p))

    # Prefer MDTM
    if any(ts for ts, _ in scored):
        scored.sort(key=lambda x: x[0])
        return scored[-1][1]

    # fallback: filename sort
    matches.sort()
    return matches[-1]

def ftp_get_size(ftp: ftplib.FTP, path: str) -> int | None:
    try:
        try:
            ftp.voidcmd("TYPE I")
        except Exception:
            pass
        resp = ftp.sendcmd(f"SIZE {path}")
        return int(resp.split()[-1])
    except Exception:
        return None

def ftp_read_from_offset(ftp: ftplib.FTP, path: str, offset: int) -> bytes:
    buf = bytearray()

    def cb(chunk: bytes):
        buf.extend(chunk)

    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass

    ftp.retrbinary(f"RETR {path}", cb, rest=offset)
    return bytes(buf)

def resolve_log_target(ftp: ftplib.FTP) -> str:
    """
    If FTP_LOG_PATH is a file, use it.
    If it's a directory, pick newest matching LOG_PATTERN.
    If none found, try fallback directories.
    """
    path = FTP_LOG_PATH.rstrip("/")

    if not ftp_is_directory(ftp, path):
        return path  # treat as file

    # First try user-provided directory
    newest = pick_newest_matching(ftp, path, LOG_PATTERN)
    if newest:
        return newest

    # Then try fallback dirs
    for d in FALLBACK_DIRS:
        if ftp_is_directory(ftp, d):
            newest = pick_newest_matching(ftp, d, LOG_PATTERN)
            if newest:
                print(f"Found tribe logs in fallback dir: {d}")
                return newest

    raise RuntimeError(f"No {LOG_PATTERN} files found in directory: {path} (and fallbacks)")


# ============================================================
# FETCH ONLY NEW LINES
# ============================================================

def fetch_new_lines() -> tuple[str, list[str]]:
    state = load_state()
    last_file = state.get("file")
    offset = int(state.get("offset", 0))

    ftp = ftp_connect()
    try:
        target_file = resolve_log_target(ftp)

        if target_file != last_file:
            print(f"Log target changed: {last_file} -> {target_file} (resetting cursor)")
            offset = 0

        size = ftp_get_size(ftp, target_file)
        if size is not None and size < offset:
            print(f"Log shrank (rotation?) size={size} < offset={offset}. Resetting offset to 0.")
            offset = 0

        raw = ftp_read_from_offset(ftp, target_file, offset)
        if not raw:
            save_state(target_file, offset)
            return target_file, []

        new_offset = offset + len(raw)
        save_state(target_file, new_offset)

        text = raw.decode("utf-8", errors="ignore")
        return target_file, text.splitlines()

    finally:
        try:
            ftp.quit()
        except Exception:
            pass


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("Tribe log forwarder started")
    print("FTP_LOG_PATH:", FTP_LOG_PATH)
    print("LOG_PATTERN:", LOG_PATTERN)
    print("TARGET_TRIBE:", TARGET_TRIBE)
    print(f"Polling every {POLL_INTERVAL:.1f} seconds")

    while True:
        try:
            file_used, lines = fetch_new_lines()
            if lines:
                print(f"Read {len(lines)} new lines from {file_used}")

            sent = 0
            for line in lines:
                if TARGET_TRIBE.lower() in line.lower():
                    send_to_discord(format_log_line(line))
                    sent += 1

            if lines and sent == 0:
                print("No matching tribe lines found in new data.")
            if sent:
                print(f"Sent {sent} messages to Discord.")

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()