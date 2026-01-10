import ftplib
import time
import os
import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone

TRIBE_NAME = "Tribe Valkyrie"

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "5"))
STATE_FILE = "cursor.json"

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

# Directory containing logs (your value is correct)
FTP_LOG_DIR = os.getenv("FTP_LOG_DIR")  # e.g. arksa/ShooterGame/Saved/Logs

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

missing = [k for k, v in {
    "FTP_HOST": FTP_HOST,
    "FTP_USER": FTP_USER,
    "FTP_PASS": FTP_PASS,
    "FTP_LOG_DIR": FTP_LOG_DIR,
    "DISCORD_WEBHOOK_URL": DISCORD_WEBHOOK_URL,
}.items() if not v]

if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

COLOR_CLAIM = 0x9B59B6   # purple
COLOR_TAME  = 0x2ECC71   # green
COLOR_DEATH = 0xE74C3C   # red
COLOR_DEST  = 0xF1C40F   # yellow
COLOR_OTHER = 0x95A5A6   # grey


def utc_now():
    return datetime.now(timezone.utc).isoformat()

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"file": None, "offsets": {}}
    try:
        with open(STATE_FILE, "r") as f:
            s = json.load(f)
        if "offsets" not in s or not isinstance(s["offsets"], dict):
            s["offsets"] = {}
        return {"file": s.get("file"), "offsets": s["offsets"]}
    except Exception:
        return {"file": None, "offsets": {}}

def save_state(current_file, offsets):
    with open(STATE_FILE, "w") as f:
        json.dump({"file": current_file, "offsets": offsets}, f)

def send_webhook(text, color):
    payload = {
        "embeds": [{
            "description": text[:4096],
            "color": int(color),
            "timestamp": utc_now(),
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
        urllib.request.urlopen(req, timeout=20).read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print("Discord HTTP error:", e.code, body)
    except Exception as e:
        print("Webhook failed:", e)

def classify_color(line: str) -> int:
    l = line.lower()
    if "claimed" in l:
        return COLOR_CLAIM
    if "tamed" in l or "froze baby" in l:
        return COLOR_TAME
    if "was killed" in l or "starved" in l or "died" in l:
        return COLOR_DEATH
    if "demolished" in l:
        return COLOR_DEST
    return COLOR_OTHER

def clean_line(line: str) -> str:
    line = re.sub(r"<[^>]+>", "", line)
    return line.strip()

def ensure_dir(ftp: ftplib.FTP, directory: str):
    ftp.cwd(directory)

def basename(path: str) -> str:
    # Works for both "/" and "\" just in case
    return path.replace("\\", "/").split("/")[-1]

def list_tribelog_files(ftp: ftplib.FTP) -> list[str]:
    """
    Return entries that point to TribeLog_*.log.
    The server may return either:
      - "TribeLog_123.log"
      - "arksa/ShooterGame/Saved/Logs/TribeLog_123.log"
    We match on basename, but we return the original string so RETR works.
    """
    try:
        names = ftp.nlst()
    except Exception:
        names = []
        lines = []
        ftp.retrlines("LIST", lines.append)
        for ln in lines:
            parts = ln.split()
            if parts:
                names.append(parts[-1])

    tribe_logs = []
    for n in names:
        b = basename(n).lower()
        if b.startswith("tribelog_") and b.endswith(".log"):
            tribe_logs.append(n)

    return tribe_logs

def mdtm(ftp: ftplib.FTP, path_or_name: str) -> str | None:
    try:
        resp = ftp.sendcmd(f"MDTM {path_or_name}")
        return resp.split()[-1].strip()  # "213 yyyymmddhhmmss"
    except Exception:
        return None

def pick_latest_tribelog(ftp: ftplib.FTP) -> str | None:
    files = list_tribelog_files(ftp)
    if not files:
        return None

    best = None
    best_ts = None
    for f in files:
        ts = mdtm(ftp, f)
        if ts is None:
            continue
        if best is None or ts > best_ts:
            best = f
            best_ts = ts

    if best is None:
        # fallback: sort by basename to get the “last” one
        return sorted(files, key=lambda x: basename(x).lower())[-1]

    return best

def read_from_ftp_with_rest(ftp: ftplib.FTP, remote_path: str, start_offset: int) -> bytes:
    data = bytearray()

    def cb(chunk):
        data.extend(chunk)

    try:
        try:
            ftp.voidcmd("TYPE I")  # binary
        except Exception:
            pass

        ftp.retrbinary(f"RETR {remote_path}", cb, rest=start_offset)
    except ftplib.error_perm as e:
        msg = str(e)
        if "550" in msg or "450" in msg or "426" in msg:
            return b""
        raise

    return bytes(data)

def get_new_lines():
    state = load_state()
    offsets = state["offsets"]

    with ftplib.FTP() as ftp:
        ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
        ftp.login(FTP_USER, FTP_PASS)
        ftp.set_pasv(True)

        ensure_dir(ftp, FTP_LOG_DIR)

        latest = pick_latest_tribelog(ftp)
        if latest is None:
            print("No TribeLog_*.log files found in directory:", FTP_LOG_DIR)
            return []

        if state["file"] != latest:
            print(f"Log target changed: {state['file']} -> {latest}")

        current_offset = int(offsets.get(latest, 0))
        raw = read_from_ftp_with_rest(ftp, latest, current_offset)

        if not raw:
            save_state(latest, offsets)
            return []

        offsets[latest] = current_offset + len(raw)
        save_state(latest, offsets)

        text = raw.decode(errors="ignore")
        return text.splitlines()

def main():
    print(f"Polling every {POLL_INTERVAL:.1f} seconds")
    while True:
        try:
            lines = get_new_lines()
            if lines:
                print("Read", len(lines), "new lines")

            for line in lines:
                if TRIBE_NAME not in line:
                    continue

                clean = clean_line(line)
                if not clean:
                    continue

                color = classify_color(clean)
                send_webhook(clean, color)

        except Exception as e:
            print("Error:", e)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()