import os
import re
import json
import time
import hashlib
import logging
from typing import List, Optional, Tuple

import requests
from rcon.source import Client  # pip install rcon


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
SEND_BACKLOG = os.getenv("SEND_BACKLOG", "0").strip() == "1"

RCON_COMMAND = os.getenv("RCON_COMMAND", "gettribelog").strip()

STATE_PATH = os.getenv("STATE_PATH", "tribe_forwarder_state.json")


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
COLOR_DEFAULT = 0x95A5A6  # grey
COLOR_PURPLE = 0x9B59B6
COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_YELLOW = 0xF1C40F


def pick_color(text: str) -> int:
    t = text.lower()
    if "claimed" in t or "unclaimed" in t or "claiming" in t:
        return COLOR_PURPLE
    if "tamed" in t or "taming" in t:
        return COLOR_GREEN
    if "killed" in t or "was killed" in t or "died" in t or "death" in t:
        return COLOR_RED
    if "demolished" in t or "destroyed" in t:
        return COLOR_YELLOW
    return COLOR_DEFAULT


def post_webhook_embed(message: str, color: int) -> bool:
    """
    Sends an embed with Discord webhook.
    Handles rate limits (429) by sleeping retry_after.
    Returns True if sent successfully.
    """
    payload = {
        "embeds": [{"description": message, "color": color}]
    }

    for attempt in range(5):
        try:
            resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
            if resp.status_code in (200, 204):
                return True

            if resp.status_code == 429:
                try:
                    data = resp.json()
                    retry_after = float(data.get("retry_after", 1.0))
                except Exception:
                    retry_after = 1.0
                log.warning("Discord rate limited (429). Sleeping %.2fs then retrying...", retry_after)
                time.sleep(retry_after)
                continue

            log.error("Discord webhook error %s: %s", resp.status_code, resp.text[:300])
            return False
        except Exception as e:
            log.error("Discord send error: %s", e)
            time.sleep(1.0)

    return False


# =========================
# Parsing + formatting
# =========================

DAY_TIME_RE = re.compile(r"Day\s+(\d+),\s*([0-9]{1,2}:[0-9]{2}:[0-9]{2})")
RICH_TAG_RE = re.compile(r"<\/?RichColor[^>]*>", re.IGNORECASE)
GENERIC_TAG_RE = re.compile(r"<[^>]+>")  # strips any remaining XML-ish tags
DOUBLE_TS_PREFIX_RE = re.compile(r"^\[[^\]]+\]\[\d+\]\s*\d{4}\.\d{2}\.\d{2}_[0-9]{2}\.[0-9]{2}\.[0-9]{2}:\s*")


def clean_tail_junk(s: str) -> str:
    """
    Removes the annoying trailing characters we’ve seen: </>), !), !</>), etc.
    Also removes extra closing parens if they’re just log artifacts.
    """
    s = s.strip()

    # common trailing artifacts
    for _ in range(6):
        changed = False
        for suffix in ["</>)", "</>)", "</>", "<//>", "/>)", "/>", ">)", "!)", "!))", "!) )"]:
            if s.endswith(suffix):
                s = s[: -len(suffix)].rstrip()
                changed = True
        if not changed:
            break

    # remove trailing stray punctuation that Ark logs add
    s = s.rstrip("!").rstrip()
    # If we end with a lonely ")" artifact, strip it
    if s.endswith(")") and s.count("(") < s.count(")"):
        s = s[:-1].rstrip()

    return s


def shorten_level_segment(s: str) -> str:
    """
    If the message has:  'Name - Lvl 150 (SomeType)'
    we convert to:       'Name - Lvl 150'
    """
    # inside quoted section, remove the final " (Something)" if it exists.
    # This keeps output cleaner like your example.
    s = re.sub(r"(\'[^']* - Lvl \d+)\s*\([^)]*\)(\')", r"\1\2", s)
    return s


def format_line(raw: str) -> Optional[Tuple[str, int]]:
    """
    Returns (formatted_message, color) or None if it doesn't match.
    Output format:
      Day 221, 22:51:49 - Sir Magnus claimed 'Roan Pinto - Lvl 150'
    """
    line = raw.strip()
    if not line:
        return None

    # filter tribe
    if TARGET_TRIBE.lower() not in line.lower():
        return None

    # remove known prefixes like [timestamp][id]2026.01.10_...
    line = DOUBLE_TS_PREFIX_RE.sub("", line)

    # strip RichColor and any other tags
    line = RICH_TAG_RE.sub("", line)
    line = GENERIC_TAG_RE.sub("", line)

    # Find day/time
    m = DAY_TIME_RE.search(line)
    if not m:
        # If no Day/time, just return cleaned line
        cleaned = clean_tail_junk(line)
        cleaned = shorten_level_segment(cleaned)
        color = pick_color(cleaned)
        return cleaned, color

    day = m.group(1)
    t = m.group(2)

    # Take everything after the day/time portion
    after = line[m.end():].lstrip()
    # logs often have ": " right after time
    if after.startswith(":"):
        after = after[1:].lstrip()

    # Clean tail + shorten "(Type)"
    after = clean_tail_junk(after)
    after = shorten_level_segment(after)

    # Final message
    msg = f"Day {day}, {t} - {after}".strip()
    msg = clean_tail_junk(msg)

    color = pick_color(msg)
    return msg, color


# =========================
# State (dedupe)
# =========================
def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"seen": [], "last_heartbeat": 0}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen": [], "last_heartbeat": 0}


def save_state(state: dict) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        log.warning("Could not save state: %s", e)


def stable_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


# =========================
# RCON fetch
# =========================
def rcon_get_lines() -> List[str]:
    """
    Connects to RCON, runs gettribelog, returns response lines.
    """
    with Client(RCON_HOST, RCON_PORT, passwd=RCON_PASSWORD, timeout=10) as client:
        resp = client.run(RCON_COMMAND)

    if resp is None:
        return []

    # resp can be bytes or str depending on server/library
    if isinstance(resp, bytes):
        text = resp.decode("utf-8", errors="ignore")
    else:
        text = str(resp)

    # normalize newlines
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    return lines


# =========================
# Main loop
# =========================
def main():
    require_env()

    log.info("Starting Container")
    log.info("RCON: %s:%s | cmd=%s", RCON_HOST, RCON_PORT, RCON_COMMAND)
    log.info("Polling every %.1fs", POLL_SECONDS)
    log.info("Filtering: %s", TARGET_TRIBE)

    state = load_state()
    seen = state.get("seen", [])
    seen_set = set(seen)

    last_heartbeat = float(state.get("last_heartbeat", 0))
    heartbeat_every = HEARTBEAT_MINUTES * 60.0

    first_run = True
    backlog_sent_any = False

    while True:
        sent_any_this_poll = False
        sent_count = 0

        try:
            lines = rcon_get_lines()

            # RCON tribe log outputs usually contain newest last; we’ll preserve order,
            # but on SEND_BACKLOG first run we’ll send older -> newer.
            parsed: List[Tuple[str, int, str]] = []
            for raw in lines:
                out = format_line(raw)
                if not out:
                    continue
                msg, color = out
                h = stable_hash(msg)
                parsed.append((msg, color, h))

            if first_run and SEND_BACKLOG:
                # send everything we haven't seen yet (older -> newer)
                for msg, color, h in parsed:
                    if h in seen_set:
                        continue
                    if sent_count >= MAX_SEND_PER_POLL:
                        break
                    if post_webhook_embed(msg, color):
                        seen_set.add(h)
                        seen.append(h)
                        sent_any_this_poll = True
                        backlog_sent_any = True
                        sent_count += 1

                # Keep seen history bounded
                if len(seen) > 5000:
                    seen = seen[-5000:]
                    seen_set = set(seen)

            else:
                # Normal mode: send only NEW entries, but prioritize MOST RECENT first
                # (prevents spam if Nitrado/Ark dumps a bunch at once)
                for msg, color, h in reversed(parsed):
                    if h in seen_set:
                        continue
                    if sent_count >= MAX_SEND_PER_POLL:
                        break
                    if post_webhook_embed(msg, color):
                        seen_set.add(h)
                        seen.append(h)
                        sent_any_this_poll = True
                        sent_count += 1

                if len(seen) > 5000:
                    seen = seen[-5000:]
                    seen_set = set(seen)

            # heartbeat
            now = time.time()
            if now - last_heartbeat >= heartbeat_every:
                if not sent_any_this_poll:
                    post_webhook_embed("No new logs since last check.", COLOR_DEFAULT)
                last_heartbeat = now

            first_run = False

        except Exception as e:
            log.error("Error: %s", e)

        state = {"seen": seen, "last_heartbeat": last_heartbeat}
        save_state(state)

        # If SEND_BACKLOG is enabled and there are more than MAX_SEND_PER_POLL unseen,
        # keep polling quickly until backlog is drained a bit.
        if first_run and SEND_BACKLOG and backlog_sent_any and sent_count >= MAX_SEND_PER_POLL:
            time.sleep(2.0)
        else:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()