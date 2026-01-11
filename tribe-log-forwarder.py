import os
import re
import time
import json
import asyncio
import hashlib
import aiohttp

# ============================================================
# ENV
# ============================================================

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = os.getenv("RCON_PORT")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

TRIBE_ROUTES_RAW = os.getenv("TRIBE_ROUTES")

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "10"))
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "60"))
MAX_SEND_PER_POLL = int(os.getenv("MAX_SEND_PER_POLL", "5"))

# If true, sends old matching lines on first run (can spam). Default false.
SEND_BACKLOG_FIRST_RUN = os.getenv("SEND_BACKLOG_FIRST_RUN", "false").strip().lower() in ("1", "true", "yes", "y")

# ============================================================
# VALIDATION
# ============================================================

missing = []
for k in ["RCON_HOST", "RCON_PORT", "RCON_PASSWORD", "TRIBE_ROUTES"]:
    if not os.getenv(k):
        missing.append(k)

if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

RCON_PORT = int(RCON_PORT)

try:
    TRIBE_ROUTES = json.loads(TRIBE_ROUTES_RAW)
except Exception as e:
    raise RuntimeError(f"TRIBE_ROUTES must be valid JSON. Error: {e}")

if not isinstance(TRIBE_ROUTES, list) or not TRIBE_ROUTES:
    raise RuntimeError("TRIBE_ROUTES must be a JSON array with at least one route.")

for i, r in enumerate(TRIBE_ROUTES):
    if not isinstance(r, dict):
        raise RuntimeError(f"TRIBE_ROUTES[{i}] must be an object.")
    for key in ("tribe", "webhook", "thread_id"):
        if key not in r or not str(r[key]).strip():
            raise RuntimeError(f"TRIBE_ROUTES[{i}] is missing '{key}'.")

print("Starting Container")
print("Routing tribes:", ", ".join(r["tribe"] for r in TRIBE_ROUTES))

# ============================================================
# COLORS (Discord embed sidebar)
# ============================================================
COLOR_RED = 0xE74C3C
COLOR_YELLOW = 0xF1C40F
COLOR_PURPLE = 0x9B59B6
COLOR_GREEN = 0x2ECC71
COLOR_LIGHT_BLUE = 0x5DADE2
COLOR_WHITE = 0xFFFFFF

# ============================================================
# RCON (Source-style minimal)
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

def _decode_rcon_bytes(b: bytes) -> str:
    """
    Best-effort decode without intentionally replacing characters.
    If server sends non-UTF8, fall back to latin-1.
    """
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1", errors="strict")

async def rcon_command(command: str, timeout: float = 6.0) -> str:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout
    )
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
            if size < 10 or i + size > len(data):
                break
            pkt = data[i:i+size]
            i += size
            # pkt: req_id(4) + type(4) + body + \x00\x00
            body = pkt[8:-2]
            txt = _decode_rcon_bytes(body)
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
# PARSING / CLEANING
# ============================================================

# Matches:
# Day 233, 17:45:33: Sir Magnus froze [Ø] - Lvl 215 (Pyromane)
DAY_LINE_RE = re.compile(r"(Day\s+\d+,\s+\d{2}:\d{2}:\d{2}):\s*(.*)")

# Remove RichColor tags and any remaining angle-tag fragments
RICHCOLOR_RE = re.compile(r"<\s*RichColor[^>]*>", re.IGNORECASE)
TAG_RE = re.compile(r"</\s*>", re.IGNORECASE)

# Remove noisy prefixes sometimes included in server logs
# Examples:
# 2026.01.11_14.36.10: Tribe Valkyrie, ID 123...: Day 233, 13:30:26: ...
PREFIX_TO_DAY_RE = re.compile(r".*?(Day\s+\d+,\s+\d{2}:\d{2}:\d{2}:)", re.IGNORECASE)

def strip_trailing_punct(s: str) -> str:
    # remove annoying log endings like "!)", "! )", "'))", "</>)", etc
    s = s.strip()

    # remove closing tag fragments
    s = TAG_RE.sub("", s).strip()

    # common trailing noise
    while s.endswith("!)") or s.endswith("')!") or s.endswith("!)") or s.endswith("!)") or s.endswith("!)"):
        s = s[:-2].rstrip()

    # remove a single trailing "!))" style junk
    s = re.sub(r"[!]+[)\]]+$", "", s).strip()
    # remove trailing unmatched ')' if it’s from junk, but keep real "(Rex)" etc
    s = re.sub(r"</\s*\)\s*>$", "", s).strip()

    # final tidy: remove trailing lone "!" only (keep normal punctuation inside)
    s = re.sub(r"!+$", "", s).strip()
    return s

def clean_line_to_day_time_who_what(raw_line: str) -> str | None:
    line = raw_line.strip()
    if not line:
        return None

    # If line contains a Day... later, cut to that
    m = PREFIX_TO_DAY_RE.match(line)
    if m:
        idx = m.start(1)
        line = line[idx:]

    # Remove RichColor open tags anywhere
    line = RICHCOLOR_RE.sub("", line)

    # Now try to isolate "Day X, HH:MM:SS: rest"
    m2 = re.search(r"(Day\s+\d+,\s+\d{2}:\d{2}:\d{2}):\s*(.*)", line)
    if not m2:
        return None

    day_time = m2.group(1).strip()
    rest = m2.group(2).strip()

    # Remove extra "Tribe Valkyrie, ID ...:" that sometimes appears before the action
    # If rest begins with "Tribe X, ID ...: Day ..." we already handled above,
    # but sometimes it begins with "Tribe Valkyrie, ID ...: Sir ..."
    rest = re.sub(r"^Tribe\s+.*?ID\s+\d+:\s*", "", rest, flags=re.IGNORECASE).strip()

    # Remove remaining closing RichColor tags
    rest = TAG_RE.sub("", rest).strip()

    # Final punctuation cleanup
    rest = strip_trailing_punct(rest)

    # Final output format
    return f"{day_time} - {rest}"

def classify_color(text: str) -> int:
    t = text.lower()

    # Red: killed / died / death / destroyed (also "starved to death")
    if any(k in t for k in ["killed", "died", "death", "destroyed", "starved to death"]):
        return COLOR_RED

    # Yellow: demolished OR unclaimed
    if "demolished" in t or "unclaimed" in t:
        return COLOR_YELLOW

    # Purple: claimed (but NOT unclaimed)
    if "claimed" in t and "unclaimed" not in t:
        return COLOR_PURPLE

    # Green: tamed
    if "tamed" in t:
        return COLOR_GREEN

    # Light blue: alliance
    if "alliance" in t:
        return COLOR_LIGHT_BLUE

    # White: everything else (froze, etc)
    return COLOR_WHITE

# ============================================================
# DISCORD WEBHOOK SENDER (Forum thread support)
# ============================================================

def build_thread_webhook_url(base_webhook: str, thread_id: str) -> str:
    """
    Discord expects thread_id query param (lowercase).
    If the user pasted a webhook with ?Thread=... we ignore that and enforce thread_id properly.
    """
    base = base_webhook.split("?", 1)[0].strip()
    return f"{base}?thread_id={thread_id}&wait=true"

async def post_embed(session: aiohttp.ClientSession, webhook_url: str, thread_id: str, content: str, color: int):
    url = build_thread_webhook_url(webhook_url, thread_id)
    payload = {
        "embeds": [{
            "description": content,
            "color": color
        }]
    }

    # basic rate limit handling
    for _ in range(5):
        async with session.post(url, json=payload) as r:
            if r.status in (200, 204):
                return True
            if r.status == 429:
                data = await r.json()
                retry_after = float(data.get("retry_after", 1.0))
                await asyncio.sleep(max(0.2, retry_after))
                continue
            txt = await r.text()
            print(f"Discord webhook error {r.status}: {txt}")
            return False

    return False

# ============================================================
# MAIN LOOP (GetGameLog poll + dedupe)
# ============================================================

class DedupeLRU:
    def __init__(self, max_items: int = 4000):
        self.max_items = max_items
        self.order = []
        self.set = set()

    def seen(self, key: str) -> bool:
        return key in self.set

    def add(self, key: str):
        if key in self.set:
            return
        self.set.add(key)
        self.order.append(key)
        if len(self.order) > self.max_items:
            old = self.order.pop(0)
            self.set.discard(old)

def hash_line(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

async def run():
    dedupe = DedupeLRU(max_items=6000)
    first_run = True

    # Track last activity per tribe for heartbeat
    last_activity_ts = {r["tribe"]: time.time() for r in TRIBE_ROUTES}
    heartbeat_seconds = HEARTBEAT_MINUTES * 60

    async with aiohttp.ClientSession() as session:
        while True:
            sent_anything = False

            try:
                raw = await rcon_command("GetGameLog", timeout=8.0)
            except Exception as e:
                print(f"[ERROR] RCON GetGameLog failed: {e}")
                await asyncio.sleep(POLL_SECONDS)
                continue

            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]

            # Build per-tribe candidate list (newest-first, so we prefer most recent)
            # We still allow multiple sends per poll, limited by MAX_SEND_PER_POLL.
            to_send = []  # (tribe, route, cleaned_text, color)

            for ln in reversed(lines):
                cleaned = clean_line_to_day_time_who_what(ln)
                if not cleaned:
                    continue

                h = hash_line(cleaned)
                if dedupe.seen(h):
                    continue

                # Match to tribe routes by tribe name appearing in original line OR cleaned line
                for route in TRIBE_ROUTES:
                    tribe = str(route["tribe"])
                    if tribe.lower() in ln.lower() or tribe.lower() in cleaned.lower():
                        color = classify_color(cleaned)
                        to_send.append((tribe, route, cleaned, color))
                        dedupe.add(h)
                        break  # one tribe per line

                if len(to_send) >= MAX_SEND_PER_POLL:
                    break

            # First run behavior
            if first_run and not SEND_BACKLOG_FIRST_RUN:
                # Seed dedupe from current output to avoid backlog spam
                for ln in lines:
                    cleaned = clean_line_to_day_time_who_what(ln)
                    if cleaned:
                        dedupe.add(hash_line(cleaned))
                print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")
                first_run = False
            else:
                first_run = False

                # Send
                for tribe, route, cleaned, color in to_send:
                    ok = await post_embed(
                        session=session,
                        webhook_url=str(route["webhook"]),
                        thread_id=str(route["thread_id"]),
                        content=cleaned,
                        color=color
                    )
                    if ok:
                        sent_anything = True
                        last_activity_ts[tribe] = time.time()

            # Heartbeat: ONLY if no activity for HEARTBEAT_MINUTES
            now = time.time()
            for route in TRIBE_ROUTES:
                tribe = str(route["tribe"])
                idle = now - last_activity_ts.get(tribe, now)
                if idle >= heartbeat_seconds:
                    hb_text = f"⏱️ No new logs since last check. (Tribe: {tribe})"
                    await post_embed(
                        session=session,
                        webhook_url=str(route["webhook"]),
                        thread_id=str(route["thread_id"]),
                        content=hb_text,
                        color=COLOR_WHITE
                    )
                    last_activity_ts[tribe] = time.time()
                    print(f"Heartbeat sent for {tribe} (idle {int(idle)}s).")

            await asyncio.sleep(POLL_SECONDS)

# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":
    asyncio.run(run())