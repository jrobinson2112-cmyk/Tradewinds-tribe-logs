import ftplib
import time
import os
import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone

TRIBE_NAME = "Tribe Valkyrie"
POLL_INTERVAL = 5.0
STATE_FILE = "cursor.json"

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

# If AUTO_PICK_LATEST_LOG=1, this is treated as a DIRECTORY to scan.
# Otherwise it is treated as the exact file to read.
FTP_LOG_FILE = os.getenv("FTP_LOG_FILE")  # e.g. arksa/ShooterGame/Saved/Logs/ShooterGame.log
AUTO_PICK_LATEST_LOG = os.getenv("AUTO_PICK_LATEST_LOG", "0") == "1"

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

if not all([FTP_HOST, FTP_USER, FTP_PASS, FTP_LOG_FILE, DISCORD_WEBHOOK_URL]):
    raise RuntimeError("Missing required environment variables")

COLOR_CLAIM = 0x9B59B6   # purple
COLOR_TAME  = 0x2ECC71   # green
COLOR_DEATH = 0xE74C3C   # red
COLOR_DEST  = 0xF1C40F   # yellow
COLOR_OTHER = 0x95A5A6   # grey

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"path": None, "offset": 0}
    try:
        with open(STATE_FILE, "r") as f:
            s = json.load(f)
            return {"path": s.get("path"), "offset": int(s.get("offset", 0))}
    except Exception:
        return {"path": None, "offset": 0}

def save_state(path, offset):
    with open(STATE_FILE, "w") as f:
        json.dump({"path": path, "offset": int(offset)}, f)

def send_webhook(text, color):
    payload = {
        "embeds": [{
            "description": text[:4096],
            "color": color,
            "timestamp": utc_now()
        }]
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print("Discord HTTP error:", e.code, body)
    except Exception as e:
        print("Webhook failed:", e)

def classify_color(line):
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

def clean_line(line):
    line = re.sub(r"<[^>]+>", "", line)
    return line.strip()

def split_dir_and_file(path):
    path = path.strip()
    if "/" not in path:
        return "", path
    d, f = path.rsplit("/", 1)
    return d, f

def ftp_list_files(ftp, directory):
    files = []
    try:
        ftp.cwd(directory)
    except Exception:
        return files

    try:
        names = ftp.nlst()
    except Exception:
        return files

    for n in names:
        if n in (".", ".."):
            continue
        files.append(n)
    return files

def ftp_mdtm(ftp, filename):
    try:
        resp = ftp.sendcmd(f"MDTM {filename}")
        # resp like: "213 20260110071234"
        ts = resp.split()[-1].strip()
        return ts
    except Exception:
        return None

def pick_latest_log_path(ftp, base_path):
    # base_path can be a directory or a file path; if it looks like a file, use its directory
    base_dir, base_name = split_dir_and_file(base_path)

    # If user gave a file and AUTO_PICK is enabled, scan the file's directory
    directory = base_path if base_name == "" else base_dir
    if directory == "":
        directory = "."

    candidates = ftp_list_files(ftp, directory)
    if not candidates:
        return base_path

    # Prefer active-looking logs
    preferred = []
    for n in candidates:
        ln = n.lower()
        if ln == "shootergame.log" or ln.startswith("servergame.") or ln.startswith("shootergame-backup") is False:
            if ln.endswith(".log"):
                preferred.append(n)

    if not preferred:
        preferred = [n for n in candidates if n.lower().endswith(".log")]

    best = None
    best_ts = None
    for n in preferred:
        ts = ftp_mdtm(ftp, n)
        if ts is None:
            continue
        if best is None or ts > best_ts:
            best = n
            best_ts = ts

    if best is None:
        # fallback: if ShooterGame.log exists, use it
        for n in candidates:
            if n.lower() == "shootergame.log":
                best = n
                break

    if best is None:
        return base_path

    # return full path
    if directory in (".", ""):
        return best
    return f"{directory}/{best}"

def read_new_lines():
    state = load_state()
    last_path = state["path"]
    offset = state["offset"]

    with ftplib.FTP() as ftp:
        ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
        ftp.login(FTP_USER, FTP_PASS)
        ftp.set_pasv(True)
        ftp.voidcmd("TYPE I")  # binary for SIZE + REST

        target_path = FTP_LOG_FILE

        if AUTO_PICK_LATEST_LOG:
            # Choose newest log in the folder so we keep following active logs
            target_path = pick_latest_log_path(ftp, FTP_LOG_FILE)

        # If the log file changed (rotation/new active file), reset cursor
        if last_path != target_path:
            print(f"Log target changed: {last_path} -> {target_path} (resetting cursor)")
            offset = 0

        # Get size (handle rotation: if size < offset => reset)
        try:
            size = ftp.size(target_path)
        except Exception as e:
            print("Could not get remote file size:", e)
            size = None

        if size is not None and size < offset:
            print(f"Detected rotation/truncation (size {size} < offset {offset}) -> resetting cursor")
            offset = 0

        if size is not None and size <= offset:
            save_state(target_path, offset)
            return []

        data = bytearray()

        def cb(chunk):
            data.extend(chunk)

        try:
            ftp.retrbinary(f"RETR {target_path}", cb, rest=offset)
        except ftplib.error_perm as e:
            print("FTP permission/error:", e)
            save_state(target_path, offset)
            return []
        except Exception as e:
            print("FTP error:", e)
            save_state(target_path, offset)
            return []

        # If we knew the size, use it. Otherwise update by bytes we read.
        new_offset = size if size is not None else (offset + len(data))
        save_state(target_path, new_offset)

        return data.decode(errors="ignore").splitlines()

def main():
    print("Polling every", POLL_INTERVAL, "seconds")
    while True:
        try:
            lines = read_new_lines()
            if lines:
                print("Read", len(lines), "new lines")
            for line in lines:
                if TRIBE_NAME not in line:
                    continue
                clean = clean_line(line)
                color = classify_color(clean)
                send_webhook(clean, color)
        except Exception as e:
            print("Error:", e)

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()