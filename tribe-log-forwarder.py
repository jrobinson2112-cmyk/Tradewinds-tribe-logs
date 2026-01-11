import os
import time
import json
import re
import asyncio
import hashlib
from typing import Dict, Any, List, Optional, Tuple
import aiohttp

# =========================
# ENV
# =========================
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = os.getenv("RCON_PORT")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

TRIBE_ROUTES_RAW = os.getenv("TRIBE_ROUTES")

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "10"))
MAX_SEND_PER_POLL = int(os.getenv("MAX_SEND_PER_POLL", "12"))

HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "60"))

# Optional: send backlog on first run (default off to avoid spam)
SEND_BACKLOG_ON_START = os.getenv("SEND_BACKLOG_ON_START", "0").strip().lower() in ("1", "true", "yes")

# =========================
# VALIDATION
# =========================
missing = []
for k in ("RCON_HOST", "RCON_PORT", "RCON_PASSWORD", "TRIBE_ROUTES"):
    if not os.getenv(k):
        missing.append(k)
if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

RCON_PORT = int(RCON_PORT)

# =========================
# ROUTES PARSING
# TRIBE_ROUTES supports:
# 1) JSON list:
#    [{"tribe":"Valkyrie","webhook":"https://discord.com/api/webhooks/...","thread_id":"123"}]
# 2) Pipe lines fallback:
#    Valkyrie|https://discord.com/api/webhooks/...|1459805053379547199
# =========================
def parse_routes(raw: str) -> List[Dict[str, str]]:
    raw = raw.strip()

    # JSON form
    if raw.startswith("[") or raw.startswith("{"):
        obj = json.loads(raw)
        routes = []
        if isinstance(obj, dict):
            # allow {"Valkyrie": {"webhook": "...", "thread_id":"..."}}
            for tribe, cfg in obj.items():
                routes.append({
                    "tribe": str(tribe),
                    "webhook": str(cfg.get("webhook", "")),
                    "thread_id": str(cfg.get("thread_id", "")).strip() or "",
                })
        elif isinstance(obj, list):
            for item in obj:
                if not isinstance(item, dict):
                    continue
                routes.append({
                    "tribe": str(item.get("tribe", "")).strip(),
                    "webhook": str(item.get("webhook", "")).strip(),
                    "thread_id": str(item.get("thread_id", "")).strip() or "",
                })
        # validate
        routes = [r for r in routes if r["tribe"] and r["webhook"]]
        return routes

    # Pipe fallback (one per line)
    routes = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        tribe = parts[0]
        webhook = parts[1]
        thread_id = parts[2] if len(parts) >= 3 else ""
        routes.append({"tribe": tribe, "webhook": webhook, "thread_id": thread_id})
    return routes


TRIBE_ROUTES = parse_routes(TRIBE_ROUTES_RAW)

if not TRIBE_ROUTES:
    raise RuntimeError("TRIBE_ROUTES parsed to an empty list. Check your env var format.")

print("Routing tribes:", ", ".join(r["tribe"] for r in TRIBE_ROUTES))

# =========================
# DISCORD COLORS
# =========================
COLOR_RED = 0xE74C3C
COLOR_YELLOW = 0xF1C40F
COLOR_PURPLE = 0x9B59B6
COLOR_GREEN = 0x2ECC71
COLOR_LIGHT_BLUE = 0x5DADE2
COLOR_WHITE = 0xFFFFFF

# =========================
# CLEANING + PARSING
# =========================
RICHCOLOR_RE = re.compile(r"<\s*RichColor[^>]*>", re.IGNORECASE)
RICHCOLOR_CLOSE_RE = re.compile(r"<\s*/\s*>", re.IGNORECASE)  # matches </> and similar
ANGLE_TAG_RE = re.compile(r"<[^>]+>")  # any other tags
LEADING_TIMESTAMP_RE = re.compile(r"^\s*\[\d{4}\.\d{2}\.\d{2}-.*?\]\[\d+\]\s*")  # [2026.01.10-...][123]
DOUBLE_TIMESTAMP_RE = re.compile(r"^\s*\d{4}\.\d{2}\.\d{2}[_-]\d{2}\.\d{2}\.\d{2}:\s*")  # 2026.01.10_08.22.17:
TRIBE_PREFIX_RE = re.compile(r"^Tribe\s+.*?:\s*", re.IGNORECASE)  # "Tribe X, ID ...: " (we'll handle via Day extraction)

DAY_LINE_RE = re.compile(r"Day\s+(\d+)\s*,\s*(\d{2}:\d{2}:\d{2})\s*:\s*(.+)", re.IGNORECASE)

def strip_noise(s: str) -> str:
    s = s.strip()

    # remove leading bracket timestamps + internal prefix
    s = LEADING_TIMESTAMP_RE.sub("", s)
    s = DOUBLE_TIMESTAMP_RE.sub("", s)

    # remove richcolor and other tags
    s = RICHCOLOR_RE.sub("", s)
    s = RICHCOLOR_CLOSE_RE.sub("", s)
    s = ANGLE_TAG_RE.sub("", s)

    # collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()

    return s

def remove_trailing_junk(msg: str, tribe: str) -> str:
    # remove trailing (TribeName) or (Valkyrie)! etc
    # Example: "... (Valkyrie)!)" or "... (Valkyrie)!"
    msg = re.sub(rf"\s*\(\s*{re.escape(tribe)}\s*\)\s*[!)]*\s*$", "", msg, flags=re.IGNORECASE)

    # remove any trailing ! ) combinations
    msg = re.sub(r"[!)]\s*$", "", msg).strip()

    return msg

def summarize_line(line: str, tribe: str) -> Optional[str]:
    """
    Returns: "Day N, HH:MM:SS - <who + what>"
    or None if line doesn't include a Day timestamp.
    """
    line = strip_noise(line)

    # If it contains "Day ..." anywhere, slice from there so we don't include other prefixes
    idx = line.lower().find("day ")
    if idx != -1:
        line = line[idx:].strip()

    m = DAY_LINE_RE.search(line)
    if not m:
        return None

    day = m.group(1)
    ts = m.group(2)
    rest = m.group(3).strip()

    rest = remove_trailing_junk(rest, tribe)

    # Final output only day/time/who/what
    return f"Day {day}, {ts} - {rest}"

def pick_color(text: str) -> int:
    t = text.lower()

    # Yellow first for unclaimed/demolished
    if "unclaimed" in t:
        return COLOR_YELLOW
    if "demolished" in t:
        return COLOR_YELLOW

    # Red
    if ("killed" in t) or ("died" in t) or ("death" in t) or ("destroyed" in t):
        return COLOR_RED

    # Purple
    if "claimed" in t:
        return COLOR_PURPLE

    # Green
    if "tamed" in t:
        return COLOR_GREEN

    # Light blue
    if "alliance" in t:
        return COLOR_LIGHT_BLUE

    # White (froze etc)
    return COLOR_WHITE

def is_for_tribe(line: str, tribe: str) -> bool:
    # Most reliable is simply checking the tribe name appears somewhere (since GetGameLog includes it in many lines)
    return tribe.lower() in line.lower()

def stable_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="surrogatepass")).hexdigest()

# =========================
# RCON (Source-style packets)
# =========================
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

def _decode_rcon_text(b: bytes) -> str:
    """
    Fixes special characters (Øðîñ etc) by trying utf-8 first,
    then cp1252/latin-1 fallback (common for game servers).
    """
    try:
        s = b.decode("utf-8")
        # If it contains replacement chars, try fallback
        if "\ufffd" in s:
            raise UnicodeDecodeError("utf-8", b, 0, 1, "replacement found")
        return s
    except Exception:
        try:
            return b.decode("cp1252")
        except Exception:
            return b.decode("latin-1", errors="ignore")

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
            if i + size > len(data) or size < 10:
                break
            pkt = data[i:i+size]
            i += size
            body = pkt[8:-2]  # strip id/type and two nulls
            txt = _decode_rcon_text(body)
            if txt:
                out.append(txt)

        return "".join(out).strip()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

# =========================
# DISCORD WEBHOOK POST
# =========================
def normalize_webhook_url(base: str, thread_id: str) -> str:
    """
    Ensures we post to forum thread properly:
    - remove any existing querystring
    - append ?thread_id=... if provided
    """
    base = base.strip()
    base = base.split("?", 1)[0]
    if thread_id:
        return f"{base}?thread_id={thread_id}"
    return base

async def post_webhook(session: aiohttp.ClientSession, url: str, payload: dict):
    while True:
        async with session.post(url, json=payload) as r:
            if r.status == 429:
                try:
                    data = await r.json()
                    retry_after = float(data.get("retry_after", 1.0))
                except Exception:
                    retry_after = 1.0
                await asyncio.sleep(max(0.2, retry_after))
                continue

            if 200 <= r.status < 300:
                return

            # log error
            try:
                txt = await r.text()
            except Exception:
                txt = ""
            print(f"Discord webhook error {r.status}: {txt}")
            return

def make_embed_line(text: str) -> dict:
    return {
        "embeds": [
            {
                "description": text,
                "color": pick_color(text),
            }
        ]
    }

# =========================
# MAIN LOOP
# =========================
async def main():
    print("Starting RCON tribe log forwarder")
    print(f"Polling every {POLL_SECONDS:.1f}s | Heartbeat {HEARTBEAT_MINUTES}m (only on inactivity)")

    # per-tribe dedupe set
    seen: Dict[str, set] = {r["tribe"]: set() for r in TRIBE_ROUTES}

    # last activity timestamps
    last_activity_ts: Dict[str, float] = {r["tribe"]: time.time() for r in TRIBE_ROUTES}
    last_heartbeat_ts: Dict[str, float] = {r["tribe"]: 0.0 for r in TRIBE_ROUTES}

    async with aiohttp.ClientSession() as session:
        # Seed dedupe so we don't spam on start unless SEND_BACKLOG_ON_START=1
        try:
            out = await rcon_command("GetGameLog", timeout=10.0)
        except Exception as e:
            print(f"[ERROR] Initial GetGameLog failed: {e}")
            out = ""

        lines = out.splitlines() if out else []
        if not SEND_BACKLOG_ON_START:
            for r in TRIBE_ROUTES:
                tribe = r["tribe"]
                for line in lines:
                    if not is_for_tribe(line, tribe):
                        continue
                    summary = summarize_line(line, tribe)
                    if not summary:
                        continue
                    seen[tribe].add(stable_hash(summary))
            print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")
        else:
            print("First run: backlog enabled, forwarding existing matching logs (capped per poll).")

        while True:
            try:
                out = await rcon_command("GetGameLog", timeout=10.0)
                raw_lines = out.splitlines() if out else []

                for r in TRIBE_ROUTES:
                    tribe = r["tribe"]
                    wh = normalize_webhook_url(r["webhook"], r.get("thread_id", ""))

                    # Collect new summaries for this tribe
                    new_summaries: List[str] = []
                    for line in raw_lines:
                        if not is_for_tribe(line, tribe):
                            continue

                        summary = summarize_line(line, tribe)
                        if not summary:
                            continue

                        h = stable_hash(summary)
                        if h in seen[tribe]:
                            continue

                        seen[tribe].add(h)
                        new_summaries.append(summary)

                    # Send only the most recent N this poll (avoid spam)
                    if new_summaries:
                        # In backlog mode, do oldest->newest; otherwise newest->oldest is fine too
                        # We'll send oldest->newest for nicer order.
                        to_send = new_summaries[-MAX_SEND_PER_POLL:]
                        for s in to_send:
                            await post_webhook(session, wh, make_embed_line(s))
                            last_activity_ts[tribe] = time.time()
                            await asyncio.sleep(0.15)  # small spacing

                    # Heartbeat only if no activity for HEARTBEAT_MINUTES
                    now = time.time()
                    inactivity = now - last_activity_ts[tribe]
                    if inactivity >= (HEARTBEAT_MINUTES * 60):
                        if now - last_heartbeat_ts[tribe] >= (HEARTBEAT_MINUTES * 60):
                            payload = make_embed_line(f"⏱️ No new logs since last check. (Tribe: {tribe})")
                            await post_webhook(session, wh, payload)
                            last_heartbeat_ts[tribe] = now

            except Exception as e:
                print(f"[ERROR] Loop error: {e}")

            await asyncio.sleep(POLL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())