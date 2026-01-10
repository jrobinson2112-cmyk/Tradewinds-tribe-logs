import os
import re
import json
import time
import hashlib
import logging
from typing import List, Optional, Tuple

import requests
from rcon.source import Client


# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tribe-log-forwarder")


# =========================
# ENV / CONFIG
# =========================
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "27020"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie").strip()
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "10"))
HEARTBEAT_MINUTES = float(os.getenv("HEARTBEAT_MINUTES", "10"))
MAX_SEND_PER_POLL = int(os.getenv("MAX_SEND_PER_POLL", "5"))
SEND_BACKLOG = os.getenv("SEND_BACKLOG", "0") == "1"

# >>> CORRECT ASA COMMAND <<<
RCON_COMMAND = "GetGameLog"

STATE_PATH = "tribe_forwarder_state.json"


def require_env():
    missing = []
    if not RCON_HOST:
        missing.append("RCON_HOST")
    if not RCON_PASSWORD:
        missing.append("RCON_PASSWORD")
    if not DISCORD_WEBHOOK_URL:
        missing.append("DISCORD_WEBHOOK_URL")
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


# =========================
# Discord helpers
# =========================
COLOR_DEFAULT = 0x95A5A6
COLOR_PURPLE = 0x9B59B6
COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_YELLOW = 0xF1C40F


def pick_color(text: str) -> int:
    t = text.lower()
    if "claimed" in t or "unclaimed" in t:
        return COLOR_PURPLE
    if "tamed" in t:
        return COLOR_GREEN
    if "killed" in t or "was killed" in t or "died" in t:
        return COLOR_RED
    if "demolished" in t or "destroyed" in t:
        return COLOR_YELLOW
    return COLOR_DEFAULT


def send_webhook(message: str, color: int) -> None:
    payload = {"embeds": [{"description": message, "color": color}]}

    while True:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        if r.status_code in (200, 204):
            return
        if r.status_code == 429:
            retry = r.json().get("retry_after", 1)
            time.sleep(float(retry))
            continue
        log.error("Discord error %s: %s", r.status_code, r.text[:200])
        return


# =========================
# Parsing
# =========================
DAY_TIME_RE = re.compile(r"Day\s+(\d+),\s*([0-9]{2}:[0-9]{2}:[0-9]{2})")
TAG_RE = re.compile(r"<[^>]+>")


def clean_text(s: str) -> str:
    s = TAG_RE.sub("", s)
    s = s.rstrip("!").rstrip(")").rstrip()
    return s


def shorten_level(s: str) -> str:
    return re.sub(r"( - Lvl \d+)\s*\([^)]*\)", r"\1", s)


def format_line(raw: str) -> Optional[Tuple[str, int]]:
    if TARGET_TRIBE.lower() not in raw.lower():
        return None

    raw = clean_text(raw)
    m = DAY_TIME_RE.search(raw)
    if not m:
        return None

    day, tm = m.groups()
    after = raw[m.end():].lstrip(": ").strip()
    after = shorten_level(after)

    msg = f"Day {day}, {tm} - {after}"
    msg = clean_text(msg)
    return msg, pick_color(msg)


# =========================
# State
# =========================
def load_state():
    if not os.path.exists(STATE_PATH):
        return {"seen": [], "last_heartbeat": 0}
    return json.load(open(STATE_PATH))


def save_state(state):
    json.dump(state, open(STATE_PATH, "w"))


def h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# =========================
# RCON
# =========================
def get_game_log() -> List[str]:
    with Client(RCON_HOST, RCON_PORT, passwd=RCON_PASSWORD, timeout=10) as c:
        out = c.run(RCON_COMMAND)
    if isinstance(out, bytes):
        out = out.decode(errors="ignore")
    return [l.strip() for l in out.splitlines() if l.strip()]


# =========================
# Main loop
# =========================
def main():
    require_env()

    log.info("Starting Container")
    log.info("Using RCON command: %s", RCON_COMMAND)
    log.info("Filtering: %s", TARGET_TRIBE)

    state = load_state()
    seen = set(state.get("seen", []))
    last_heartbeat = state.get("last_heartbeat", 0)

    first_run = True

    while True:
        sent = 0
        any_sent = False

        try:
            lines = get_game_log()
            parsed = []

            for l in lines:
                f = format_line(l)
                if f:
                    msg, color = f
                    parsed.append((msg, color, h(msg)))

            if first_run and SEND_BACKLOG:
                iterable = parsed
            else:
                iterable = reversed(parsed)

            for msg, color, hh in iterable:
                if hh in seen:
                    continue
                if sent >= MAX_SEND_PER_POLL:
                    break
                send_webhook(msg, color)
                seen.add(hh)
                any_sent = True
                sent += 1

            now = time.time()
            if now - last_heartbeat >= HEARTBEAT_MINUTES * 60:
                if not any_sent:
                    send_webhook("No new logs since last check.", COLOR_DEFAULT)
                last_heartbeat = now

            first_run = False

        except Exception as e:
            log.error("Error: %s", e)

        save_state({"seen": list(seen)[-5000:], "last_heartbeat": last_heartbeat})
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()