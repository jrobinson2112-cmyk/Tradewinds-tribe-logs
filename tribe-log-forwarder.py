import os
import time
import json
import asyncio
import hashlib
import re
from collections import deque
from typing import Optional, Dict, Tuple, List

import aiohttp

# ============================================================
# ENV / CONFIG
# ============================================================

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "27020"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")  # base webhook url
DISCORD_THREAD_ID = os.getenv("DISCORD_THREAD_ID")      # for forum channels/threads (optional)

TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie")  # exact substring match
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "10"))

# If 1: on first run it will NOT spam backlog; it will seed dedupe using current GetGameLog output
# If 0: it will forward current log on first run (can be a lot)
SEED_DEDUPE_ON_FIRST_RUN = os.getenv("SEED_DEDUPE_ON_FIRST_RUN", "1") == "1"

# Dedupe memory size (how many line hashes to remember)
DEDUPE_MAX = int(os.getenv("DEDUPE_MAX", "5000"))

# Persist dedupe between restarts
STATE_FILE = os.getenv("STATE_FILE", "rcon_tribelog_state.json")

# ============================================================
# VALIDATION
# ============================================================

missing = []
for k in ["RCON_HOST", "RCON_PORT", "RCON_PASSWORD", "DISCORD_WEBHOOK_URL"]:
    if not os.getenv(k):
        missing.append(k)
if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

# ============================================================
# DISCORD WEBHOOK HELPERS
# ============================================================

def _strip_query(url: str) -> str:
    return url.split("?", 1)[0].rstrip("/")

WEBHOOK_BASE = _strip_query(DISCORD_WEBHOOK_URL)

def webhook_post_url() -> str:
    # Forum webhooks need thread_id (or thread_name); we support thread_id.
    if DISCORD_THREAD_ID:
        return f"{WEBHOOK_BASE}?thread_id={DISCORD_THREAD_ID}"
    return WEBHOOK_BASE

def discord_color_for(text: str) -> int:
    lower = text.lower()
    if "claimed" in lower or "claiming" in lower or "unclaimed" in lower:
        return 0x9B59B6  # purple
    if "tamed" in lower or "taming" in lower:
        return 0x2ECC71  # green
    if "killed" in lower or "died" in lower or "death" in lower:
        return 0xE74C3C  # red
    if "demolished" in lower or "destroyed" in lower:
        return 0xF1C40F  # yellow
    return 0x95A5A6  # grey

async def send_to_discord(session: aiohttp.ClientSession, line: str) -> None:
    payload = {
        "embeds": [
            {
                "description": line,
                "color": discord_color_for(line),
            }
        ]
    }

    # Handle rate limits properly
    for _ in range(6):
        async with session.post(webhook_post_url(), json=payload) as r:
            if r.status in (200, 204):
                return

            txt = await r.text()

            if r.status == 429:
                try:
                    data = await r.json()
                    retry_after = float(data.get("retry_after", 0.5))
                except Exception:
                    retry_after = 0.5
                await asyncio.sleep(max(0.1, retry_after))
                continue

            print(f"[ERROR] Discord webhook error {r.status}: {txt}")
            return

# ============================================================
# TEXT CLEANUP (SPECIAL CHARS + ARK TAG JUNK)
# ============================================================

RICHCOLOR_RE = re.compile(r"<RichColor[^>]*>", re.IGNORECASE)
TAG_RE = re.compile(r"</?[^>]+>")  # crude strip of any tags

def repair_mojibake(s: str) -> str:
    # Fix common mojibake if the text got decoded incorrectly somewhere upstream
    if "Ã" in s or "â" in s:
        try:
            return s.encode("latin-1").decode("utf-8")
        except Exception:
            return s
    return s

def clean_action_text(s: str) -> str:
    s = s.strip()

    # Remove RichColor tags and any other markup tags
    s = RICHCOLOR_RE.sub("", s)
    s = TAG_RE.sub("", s)

    # Remove common trailing junk from logs: </>), </>), !</>), extra !)
    s = re.sub(r"</\)\)\s*$", "", s)
    s = re.sub(r"</\)\s*$", "", s)
    s = re.sub(r"\)\s*$", "", s)

    # Remove trailing "'!" or "!)" patterns (seen in Ark logs)
    s = re.sub(r"!\s*$", "", s)
    s = re.sub(r"\s*\)\s*$", "", s)

    # Normalize whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s

# Example line chunk contains:
# "Tribe Valkyrie, ID ...: Day 216, 16:53:34: Sir Magnus claimed 'Roan Pinto - Lvl 150 ...'!"
DAYTIME_RE = re.compile(
    r"Day\s+(?P<day>\d+),\s*(?P<time>\d{1,2}:\d{2}:\d{2}):\s*(?P<rest>.+)$",
    re.IGNORECASE,
)

def format_line_for_discord(raw_line: str) -> Optional[str]:
    raw_line = repair_mojibake(raw_line)

    # Must contain tribe
    if TARGET_TRIBE.lower() not in raw_line.lower():
        return None

    # Find "Day X, HH:MM:SS: ..."
    m = DAYTIME_RE.search(raw_line)
    if not m:
        return None

    day = m.group("day")
    t = m.group("time")
    rest = m.group("rest")

    rest = clean_action_text(rest)

    # Final format: "Day 221, 22:51:49 - Sir Magnus claimed 'Roan Pinto - Lvl 150'"
    return f"Day {day}, {t} - {rest}"

# ============================================================
# DEDUPE STATE
# ============================================================

def _hash_line(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"seen": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"seen": []}
        if "seen" not in data or not isinstance(data["seen"], list):
            data["seen"] = []
        return data
    except Exception:
        return {"seen": []}

def save_state(data: Dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass

# ============================================================
# RCON (Minimal / Source RCON) + GetGameLog
# ============================================================

def _rcon_make_packet(req_id: int, ptype: int, body: str) -> bytes:
    b = body.encode("utf-8") + b"\x00"
    pkt = (
        req_id.to_bytes(4, "little", signed=True)
        + ptype.to_bytes(4, "little", signed=True)
        + b
        + b"\x00"
    )
    size = len(pkt)
    return size.to_bytes(4, "little", signed=True) + pkt

async def rcon_command(command: str, timeout: float = 6.0) -> str:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout
    )
    try:
        # Auth
        writer.write(_rcon_make_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        if len(raw) < 12:
            raise RuntimeError("RCON auth failed (short response)")

        # Command
        writer.write(_rcon_make_packet(2, 2, command))
        await writer.drain()

        chunks = []
        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                part = await asyncio.wait_for(reader.read(4096), timeout=0.35)
            except asyncio.TimeoutError:
                break
            if not part:
                break
            chunks.append(part)

        if not chunks:
            return ""

        data = b"".join(chunks)

        # Parse packets
        out = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i:i + 4], "little", signed=True)
            i += 4
            if size < 10 or i + size > len(data):
                break
            pkt = data[i:i + size]
            i += size
            body = pkt[8:-2]
            txt = body.decode("utf-8", errors="ignore")
            if txt:
                out.append(txt)

        return "".join(out).strip()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

async def get_game_log(timeout: float = 10.0) -> str:
    # ASA supports GetGameLog in many setups. If yours is different, swap command here.
    return await rcon_command("GetGameLog", timeout=timeout)

# ============================================================
# MAIN LOOP
# ============================================================

def extract_candidate_lines(game_log_text: str) -> List[str]:
    """
    GetGameLog often returns a big blob.
    We split into lines and only keep those that contain the tribe substring,
    and also look like they have "Day X, HH:MM:SS:".
    """
    lines = []
    for ln in game_log_text.splitlines():
        if TARGET_TRIBE.lower() not in ln.lower():
            continue
        if "day " not in ln.lower():
            continue
        if ":" not in ln:
            continue
        lines.append(ln.strip())
    return lines

async def main():
    print("Starting Container")
    print(f"Polling every {POLL_INTERVAL:.1f} seconds")
    print(f"Filtering: {TARGET_TRIBE} (GetGameLog via RCON)")

    state = load_state()
    seen_deque = deque(state.get("seen", []), maxlen=DEDUPE_MAX)
    seen_set = set(seen_deque)

    first_run = True

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                blob = await get_game_log(timeout=10.0)

                if not blob:
                    print("[INFO] Heartbeat: GetGameLog returned empty.")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                candidates = extract_candidate_lines(blob)

                # First run: seed dedupe to avoid backlog spam (optional)
                if first_run and SEED_DEDUPE_ON_FIRST_RUN:
                    seeded = 0
                    for ln in candidates:
                        formatted = format_line_for_discord(ln)
                        if not formatted:
                            continue
                        h = _hash_line(formatted)
                        if h not in seen_set:
                            seen_set.add(h)
                            seen_deque.append(h)
                            seeded += 1
                    print(f"[INFO] First run: seeded dedupe from current GetGameLog ({seeded} matching lines).")
                    first_run = False
                    state["seen"] = list(seen_deque)
                    save_state(state)
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                first_run = False

                # We only want to send NEW lines
                sent = 0
                for ln in candidates:
                    formatted = format_line_for_discord(ln)
                    if not formatted:
                        continue

                    h = _hash_line(formatted)
                    if h in seen_set:
                        continue

                    # Mark seen BEFORE sending (prevents duplicates on retry loops)
                    seen_set.add(h)
                    seen_deque.append(h)

                    await send_to_discord(session, formatted)
                    sent += 1

                    # Small pause prevents hitting webhook limits when a burst happens
                    await asyncio.sleep(0.1)

                if sent == 0:
                    print("[INFO] Heartbeat: no new logs.")
                else:
                    print(f"[INFO] Sent {sent} new log(s).")

                state["seen"] = list(seen_deque)
                save_state(state)

            except asyncio.TimeoutError:
                print("[ERROR] Error: timed out")
            except Exception as e:
                print(f"[ERROR] Error: {e}")

            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())