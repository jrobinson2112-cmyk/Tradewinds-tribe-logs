import os
import time
import ftplib
import hashlib
import json
import logging
from typing import Dict, List, Optional, Tuple

import requests

# ============================================================
# CONFIG (ENV VARS)
# ============================================================

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Monitor EVERYTHING inside this logs directory
FTP_LOGS_DIR = os.getenv("FTP_LOGS_DIR", "arksa/ShooterGame/Saved/Logs")

# Tribe filter
TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie")

# Polling
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "10"))  # seconds
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "10"))

# State persistence
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# Webhook safety
MAX_SEND_PER_POLL = int(os.getenv("MAX_SEND_PER_POLL", "5"))
SEND_MIN_DELAY = float(os.getenv("SEND_MIN_DELAY", "0.35"))

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ============================================================
# VALIDATION
# ============================================================

def require_env():
    missing = []
    if not FTP_HOST: missing.append("FTP_HOST")
    if not FTP_USER: missing.append("FTP_USER")
    if not FTP_PASS: missing.append("FTP_PASS")
    if not DISCORD_WEBHOOK_URL: missing.append("DISCORD_WEBHOOK_URL")
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

# ============================================================
# DISCORD FORMAT HELPERS
# ============================================================

def classify_color(text: str) -> int:
    lower = text.lower()
    if "claimed" in lower or "claiming" in lower:
        return 0x9B59B6  # purple
    if "tamed" in lower or "taming" in lower:
        return 0x2ECC71  # green
    if "killed" in lower or "died" in lower or "was killed" in lower or "was slain" in lower:
        return 0xE74C3C  # red
    if "demolished" in lower or "destroyed" in lower:
        return 0xF1C40F  # yellow
    return 0x95A5A6  # grey


def format_discord_payload(line: str) -> dict:
    text = line.strip()
    return {"embeds": [{"description": text, "color": classify_color(text)}]}


def send_to_discord(payload: dict) -> Tuple[bool, Optional[float], str]:
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
    except Exception as e:
        return False, None, f"Webhook request failed: {e}"

    if resp.status_code in (200, 204):
        return True, None, "OK"

    if resp.status_code == 429:
        try:
            data = resp.json()
            retry_after = float(data.get("retry_after", 1.0))
        except Exception:
            retry_after = 1.0
        return False, retry_after, f"Discord webhook error 429: {resp.text}"

    return False, None, f"Discord webhook error {resp.status_code}: {resp.text}"

# ============================================================
# STATE (per-file offsets + dedupe)
# ============================================================

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {
            "offsets": {},             # { "filename": int_offset }
            "seen": [],                # recent line hashes
            "first_run_done": False,   # once true, we no longer skip backlog
            "last_heartbeat_ts": 0.0,
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
            if "offsets" not in s: s["offsets"] = {}
            if "seen" not in s: s["seen"] = []
            if "first_run_done" not in s: s["first_run_done"] = False
            if "last_heartbeat_ts" not in s: s["last_heartbeat_ts"] = 0.0
            return s
    except Exception:
        return {
            "offsets": {},
            "seen": [],
            "first_run_done": False,
            "last_heartbeat_ts": 0.0,
        }


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        logging.warning(f"Could not save state: {e}")


def line_fingerprint(line: str) -> str:
    return hashlib.sha256(line.strip().encode("utf-8", errors="ignore")).hexdigest()


def has_seen(state: dict, fp: str) -> bool:
    return fp in state.get("seen", [])


def remember_seen(state: dict, fp: str, limit: int = 3000) -> None:
    seen = state.get("seen", [])
    seen.append(fp)
    if len(seen) > limit:
        seen = seen[-limit:]
    state["seen"] = seen


# ============================================================
# FTP HELPERS
# ============================================================

def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=25)
    ftp.login(FTP_USER, FTP_PASS)
    return ftp


def ftp_cwd_logs(ftp: ftplib.FTP) -> None:
    ftp.cwd(FTP_LOGS_DIR)


def ftp_type_binary(ftp: ftplib.FTP) -> None:
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass


def ftp_size(ftp: ftplib.FTP, name: str) -> Optional[int]:
    """
    Returns size for regular files; returns None if not a regular file
    or server blocks SIZE.
    """
    try:
        ftp_type_binary(ftp)
        return ftp.size(name)
    except Exception:
        return None


def ftp_list_names(ftp: ftplib.FTP) -> List[str]:
    try:
        names = ftp.nlst()
        # Some servers return the directory itself in nlst; filter empty
        return [n for n in names if n and n not in (".", "..")]
    except Exception as e:
        logging.error(f"Could not list directory: {e}")
        return []


def ftp_read_from_offset(ftp: ftplib.FTP, name: str, offset: int) -> Tuple[bytes, int]:
    chunks: List[bytes] = []

    def cb(b: bytes):
        chunks.append(b)

    ftp_type_binary(ftp)

    if offset > 0:
        ftp.retrbinary(f"RETR {name}", cb, rest=offset)
    else:
        ftp.retrbinary(f"RETR {name}", cb)

    data = b"".join(chunks)
    return data, offset + len(data)


# ============================================================
# LOG PARSING
# ============================================================

def extract_matching_lines(text: str, target: str) -> List[str]:
    needle = target.lower()
    out = []
    for line in text.splitlines():
        if needle in line.lower():
            out.append(line)
    return out


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    require_env()

    logging.info("Starting Container")
    logging.info(f"Logs dir: {FTP_LOGS_DIR} (monitoring ALL files inside)")
    logging.info(f"Polling every {POLL_INTERVAL:.1f} seconds")
    logging.info(f"Filtering: {TARGET_TRIBE} (sending ONLY the most recent matching log across all files)")

    state = load_state()
    sent_since_last_heartbeat = 0

    while True:
        most_recent_match: Optional[str] = None
        sent_this_poll = 0

        try:
            ftp = ftp_connect()
            try:
                ftp_cwd_logs(ftp)

                names = ftp_list_names(ftp)

                # Keep only regular files (ones that return a size)
                file_infos: List[Tuple[str, int]] = []
                for n in names:
                    sz = ftp_size(ftp, n)
                    if sz is not None:
                        file_infos.append((n, sz))

                if not file_infos:
                    logging.warning(f"No regular files found in: {FTP_LOGS_DIR}")
                else:
                    # On first run (ever), skip backlog by setting offsets to end for all files
                    if not state.get("first_run_done", False):
                        for fname, sz in file_infos:
                            state["offsets"][fname] = sz
                        state["first_run_done"] = True
                        save_state(state)
                        logging.info("First run: skipped backlog and started live from the end for all files.")
                        # Still log heartbeat-ish info
                        logging.info(f"Heartbeat: files={len(file_infos)} (no reads on first run)")
                        time.sleep(POLL_INTERVAL)
                        continue

                    # Read increments from each file
                    offsets: Dict[str, int] = state.get("offsets", {})

                    total_new_bytes = 0
                    total_files_with_new = 0

                    for fname, sz in file_infos:
                        old_off = int(offsets.get(fname, 0))

                        # handle rotation/shrink
                        if sz < old_off:
                            logging.info(f"Rotation detected: {fname} size {sz} < offset {old_off}; resetting offset to 0")
                            old_off = 0

                        if sz == old_off:
                            continue  # nothing new

                        data, new_off = ftp_read_from_offset(ftp, fname, old_off)
                        offsets[fname] = new_off
                        total_new_bytes += len(data)
                        total_files_with_new += 1

                        if data:
                            text = data.decode("utf-8", errors="ignore")
                            matches = extract_matching_lines(text, TARGET_TRIBE)
                            if matches:
                                # choose last match from this file
                                candidate = matches[-1].strip()
                                # Across files, just keep the most recent we encountered this poll.
                                # (Good enough when we're tailing by byte offsets.)
                                most_recent_match = candidate

                    state["offsets"] = offsets
                    save_state(state)

                    logging.info(
                        f"Heartbeat: files={len(file_infos)} files_with_new={total_files_with_new} new_bytes={total_new_bytes}"
                    )

            finally:
                try:
                    ftp.quit()
                except Exception:
                    try:
                        ftp.close()
                    except Exception:
                        pass

        except ftplib.error_perm as e:
            logging.error(f"FTP permission/error: {e}")
        except Exception as e:
            logging.error(f"Error: {e}")

        # Send ONLY the most recent matching log found this poll (deduped)
        if most_recent_match:
            fp = line_fingerprint(most_recent_match)
            if not has_seen(state, fp):
                payload = format_discord_payload(most_recent_match)

                ok, retry_after, msg = send_to_discord(payload)
                if not ok and retry_after:
                    time.sleep(retry_after)
                    ok, _, msg2 = send_to_discord(payload)
                    msg = msg2 if not ok else "OK after retry"

                if ok:
                    remember_seen(state, fp)
                    save_state(state)
                    sent_this_poll = 1
                    sent_since_last_heartbeat += 1
                    logging.info("Sent 1 message to Discord (most recent matching log across all files).")
                    time.sleep(SEND_MIN_DELAY)
                else:
                    logging.error(msg)
            else:
                logging.info("Most recent matching line was already sent (deduped).")

        # Heartbeat to Discord every X minutes: "No new logs since last check"
        now = time.time()
        last_hb = float(state.get("last_heartbeat_ts", 0.0))
        if now - last_hb >= HEARTBEAT_MINUTES * 60:
            if sent_since_last_heartbeat == 0:
                hb_payload = {"embeds": [{"description": "No new logs since last check.", "color": 0x95A5A6}]}
                ok, retry_after, msg = send_to_discord(hb_payload)
                if not ok and retry_after:
                    time.sleep(retry_after)
                    ok, _, _ = send_to_discord(hb_payload)
                if ok:
                    logging.info("Sent heartbeat to Discord.")
                else:
                    logging.warning(f"Heartbeat send failed: {msg}")

            state["last_heartbeat_ts"] = now
            sent_since_last_heartbeat = 0
            save_state(state)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()