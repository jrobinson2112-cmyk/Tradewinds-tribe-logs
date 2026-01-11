import os
import re
import time
import json
import asyncio
import unicodedata
import hashlib
from collections import deque

import aiohttp

# ============================================================
# ENV VARS
# ============================================================
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "27020"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

# JSON mapping: {"Valkyrie":"https://discord.com/api/webhooks/....", "Hellsing":"...", "The Dwellers":"..."}
TRIBE_WEBHOOKS_JSON = os.getenv("TRIBE_WEBHOOKS", "")

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "10"))
HEARTBEAT_MINUTES = float(os.getenv("HEARTBEAT_MINUTES", "10"))
MAX_SEND_PER_POLL = int(os.getenv("MAX_SEND_PER_POLL", "10"))

# Keep a rolling memory of sent lines to avoid duplicates (hashes)
DEDUP_CACHE_SIZE = int(os.getenv("DEDUP_CACHE_SIZE", "5000"))

# Optional: set to "1" if you want to forward backlog on first start (can spam)
FORWARD_BACKLOG = os.getenv("FORWARD_BACKLOG", "0") == "1"

# ============================================================
# VALIDATION
# ============================================================
missing = []
for k in ("RCON_HOST", "RCON_PORT", "RCON_PASSWORD", "TRIBE_WEBHOOKS"):
    if not os.getenv(k) and k != "RCON_PORT":
        missing.append(k)

if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

try:
    TRIBE_WEBHOOKS = json.loads(TRIBE_WEBHOOKS_JSON)
    if not isinstance(TRIBE_WEBHOOKS, dict) or not TRIBE_WEBHOOKS:
        raise ValueError("TRIBE_WEBHOOKS must be a non-empty JSON object.")
except Exception as e:
    raise RuntimeError(f"Invalid TRIBE_WEBHOOKS JSON. Example: {{\"Valkyrie\":\"<webhook>\"}}. Error: {e}")

# Normalize tribe names for matching
TRIBES = {str(k): str(v) for k, v in TRIBE_WEBHOOKS.items()}
TRIBES_LOWER = {k.lower(): k for k in TRIBES.keys()}

print("Starting RCON tribe log forwarder")
print(f"Polling every {POLL_SECONDS:.1f}s | Heartbeat every {HEARTBEAT_MINUTES:.1f}m")
print("Routing tribes:", ", ".join(TRIBES.keys()))

# ============================================================
# DISCORD EMBED COLORING (Ark-like)
# ============================================================
COL_DEFAULT = 0x95A5A6
COL_CLAIM = 0x9B59B6   # purple
COL_TAME  = 0x2ECC71   # green
COL_DEATH = 0xE74C3C   # red
COL_DEMOL = 0xF1C40F   # yellow

def pick_color(text: str) -> int:
    t = text.lower()
    if "claimed" in t or "unclaimed" in t or "claiming" in t:
        return COL_CLAIM
    if "tamed" in t or "taming" in t:
        return COL_TAME
    if "killed" in t or "was killed" in t or "died" in t or "starved to death" in t:
        return COL_DEATH
    if "demolished" in t or "destroyed" in t:
        return COL_DEMOL
    return COL_DEFAULT

# ============================================================
# UNICODE CLEANUP (fixes ????)
# ============================================================
RICHCOLOR_RE = re.compile(r"<\s*RichColor[^>]*>", re.IGNORECASE)
TAG_RE = re.compile(r"</?[^>]+>")  # any XML-ish tags

def clean_unicode(s: str) -> str:
    if not s:
        return ""
    # Keep real Unicode characters (Øðî etc)
    s = unicodedata.normalize("NFC", s)

    # Remove only true control chars
    s = "".join(ch for ch in s if ch.isprintable() or ch in ("\n", "\t"))

    # Strip Ark RichColor + stray tags
    s = RICHCOLOR_RE.sub("", s)
    s = TAG_RE.sub("", s)

    # Fix common trailing junk seen in log forwarding
    s = s.replace("</>)", "")
    s = s.strip()

    # Strip trailing "!)" / "!)'" etc without killing legit punctuation in the middle
    s = re.sub(r"[!']?\)\s*$", "", s).strip()
    s = re.sub(r"!\s*$", "", s).strip()

    return s

# ============================================================
# FORMAT: "Day 221, 22:51:49 - Sir Magnus claimed 'Roan Pinto - Lvl 150'"
# ============================================================
DAY_TIME_RE = re.compile(r"Day\s+(?P<day>\d+),\s*(?P<time>\d{2}:\d{2}:\d{2})\s*:\s*(?P<body>.+)$")
# Matches lines like:
# "... Tribe Valkyrie, ID 123: Day 216, 17:42:24: Sir Magnus claimed 'X - Lvl 150 (Thing)'!"
# or already compact "Day 229, 06:10:28: Øðîn froze Baby Desmodus - Lvl 224 (Desmodus)"
def format_line_compact(line: str) -> str:
    line = clean_unicode(line)

    # Find the "Day X, HH:MM:SS:" portion anywhere in the line
    idx = line.lower().find("day ")
    if idx != -1:
        candidate = line[idx:]
    else:
        candidate = line

    m = DAY_TIME_RE.search(candidate)
    if not m:
        # Fallback: just return cleaned text
        return line

    day = m.group("day")
    t = m.group("time")
    body = m.group("body").strip()

    # Remove leading "Tribe <name>, ID ...:" if still present
    body = re.sub(r"^Tribe\s+[^:]+:\s*", "", body, flags=re.IGNORECASE).strip()
    body = re.sub(r"^Tribe\s+[^,]+,\s*ID\s*\d+\s*:\s*", "", body, flags=re.IGNORECASE).strip()

    # If the log includes "(Valkyrie!)" at end etc, remove trailing "(TribeName...)" chunks
    body = re.sub(r"\(\s*[^)]*\b(?:%s)\b[^)]*\)\s*$" % "|".join(re.escape(k) for k in TRIBES.keys()), "", body, flags=re.IGNORECASE).strip()

    # Remove extra duplicate right-parens
    body = body.rstrip(") ").strip()

    # If you want to remove the final species bracket e.g. "(Desmodus)" keep it? (leave as-is)
    # If you want ONLY "... - Lvl 150" (no species), uncomment:
    # body = re.sub(r"\s*\([^)]*\)\s*$", "", body).strip()

    return f"Day {day}, {t} - {body}"

# ============================================================
# RCON (Minimal RCON implementation)
# ============================================================
def _rcon_make_packet(req_id: int, ptype: int, body: str) -> bytes:
    data = body.encode("utf-8") + b"\x00"
    packet = (
        req_id.to_bytes(4, "little", signed=True)
        + ptype.to_bytes(4, "little", signed=True)
        + data
        + b"\x00"
    )
    size = len(packet)
    return size.to_bytes(4, "little", signed=True) + packet

async def rcon_command(command: str, timeout: float = 6.0) -> str:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout)
    try:
        # auth
        writer.write(_rcon_make_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        if len(raw) < 12:
            raise RuntimeError("RCON auth failed (short response)")

        # command
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

        out = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i:i+4], "little", signed=True)
            i += 4
            if i + size > len(data) or size < 10:
                break
            pkt = data[i:i+size]
            i += size
            body = pkt[8:-2]
            # IMPORTANT: keep unicode; don't ascii-sanitize
            txt = body.decode("utf-8", errors="replace")
            if txt:
                out.append(txt)

        return "".join(out).strip()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

# ============================================================
# ROUTING: detect tribe -> webhook
# ============================================================
def detect_tribe(line: str) -> str | None:
    low = line.lower()

    # Strong signal: "Tribe <name>"
    for t_low, t_actual in TRIBES_LOWER.items():
        if f"tribe {t_low}" in low:
            return t_actual

    # Fallback: any occurrence of the tribe name
    for t_low, t_actual in TRIBES_LOWER.items():
        if t_low in low:
            return t_actual

    return None

# ============================================================
# DISCORD WEBHOOK SENDER (handles 429 rate limit)
# ============================================================
async def webhook_send(session: aiohttp.ClientSession, url: str, text: str, color: int):
    payload = {
        "embeds": [
            {
                "description": text,
                "color": color,
            }
        ]
    }

    for _ in range(5):
        async with session.post(url, json=payload) as r:
            if r.status in (200, 204):
                return True

            if r.status == 429:
                try:
                    data = await r.json()
                    retry = float(data.get("retry_after", 0.5))
                except Exception:
                    retry = 0.5
                await asyncio.sleep(max(0.25, retry))
                continue

            body = await r.text()
            print(f"Discord webhook error {r.status}: {body}")
            return False

    print("Discord webhook: gave up after rate limits.")
    return False

def _stable_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()

# ============================================================
# MAIN LOOP
# ============================================================
sent_hashes = deque(maxlen=DEDUP_CACHE_SIZE)
sent_set = set()

def _remember(h: str):
    sent_hashes.append(h)
    sent_set.add(h)
    # Keep set from growing unbounded
    while len(sent_set) > DEDUP_CACHE_SIZE:
        old = sent_hashes.popleft()
        try:
            sent_set.remove(old)
        except KeyError:
            pass

async def main():
    last_heartbeat = 0.0
    first_run = True
    last_any_sent_ts = time.time()

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                out = await rcon_command("GetGameLog", timeout=8.0)

                lines = []
                if out:
                    # Split and clean
                    for raw in out.splitlines():
                        raw = raw.strip()
                        if not raw:
                            continue
                        raw = clean_unicode(raw)
                        if raw:
                            lines.append(raw)

                # On first run, you can either:
                # - skip backlog (default) and only send new things that appear later
                # - OR forward backlog (FORWARD_BACKLOG=1)
                if first_run and not FORWARD_BACKLOG:
                    # Just seed dedupe with what's currently visible
                    for ln in lines:
                        h = _stable_hash(ln)
                        _remember(h)
                    print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")
                    first_run = False
                    await asyncio.sleep(POLL_SECONDS)
                    continue

                to_send = []
                # We only send lines we haven't seen before
                for ln in lines:
                    h = _stable_hash(ln)
                    if h in sent_set:
                        continue

                    tribe = detect_tribe(ln)
                    if not tribe:
                        _remember(h)
                        continue

                    compact = format_line_compact(ln)
                    color = pick_color(compact)
                    to_send.append((tribe, compact, color))
                    _remember(h)

                # Send newest-first, capped per poll (prevents 429 spam)
                if to_send:
                    to_send = to_send[-MAX_SEND_PER_POLL:]
                    for tribe, text, color in to_send:
                        await webhook_send(session, TRIBES[tribe], text, color)
                        last_any_sent_ts = time.time()
                    print(f"Sent {len(to_send)} new log(s).")
                else:
                    now = time.time()
                    if now - last_heartbeat >= HEARTBEAT_MINUTES * 60:
                        # Send heartbeat once per interval to EACH tribe webhook
                        msg = "No new logs since last check."
                        for tribe, url in TRIBES.items():
                            await webhook_send(session, url, msg, COL_DEFAULT)
                        last_heartbeat = now
                        print("Heartbeat sent (no new logs).")

                first_run = False

            except asyncio.TimeoutError:
                print("Error: RCON timed out")
            except Exception as e:
                print(f"Error: {e}")

            await asyncio.sleep(POLL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())