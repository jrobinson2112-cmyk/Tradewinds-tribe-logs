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

# OPTIONAL: forward ALL GetGameLog lines (deduped) to this webhook so you can "just see the log"
RAWLOG_WEBHOOK = os.getenv("RAWLOG_WEBHOOK", "").strip()
RAWLOG_MAX_PER_POLL = int(os.getenv("RAWLOG_MAX_PER_POLL", "15"))
RAWLOG_FILTER = os.getenv("RAWLOG_FILTER", "").strip()  # optional substring filter

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

print("Starting RCON tribe log forwarder + GetGameLog raw viewer")
print(f"Polling every {POLL_SECONDS:.1f}s | Heartbeat every {HEARTBEAT_MINUTES:.1f}m")
print("Routing tribes:", ", ".join(TRIBES.keys()))
if RAWLOG_WEBHOOK:
    print("RAWLOG_WEBHOOK enabled (forwarding all new GetGameLog lines).")

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
# FORMATS
# - ASE style: "Day 221, 22:51:49: ..."
# - ASA style: "[2026.01.11-07.54.32] ..."
# ============================================================
DAY_TIME_RE = re.compile(
    r"Day\s+(?P<day>\d+),\s*(?P<time>\d{2}:\d{2}:\d{2})\s*:\s*(?P<body>.+)$",
    re.IGNORECASE
)
ASA_TS_RE = re.compile(
    r"^\[(?P<ts>\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2})\]\s*(?P<body>.+)$"
)

def format_line_compact(line: str) -> str:
    line = clean_unicode(line)

    # ASA style first: [YYYY.MM.DD-HH.MM.SS] message
    m2 = ASA_TS_RE.match(line)
    if m2:
        # Make it more readable: "YYYY-MM-DD HH:MM:SS"
        ts_raw = m2.group("ts")
        yyyy, mm, dd = ts_raw[0:4], ts_raw[5:7], ts_raw[8:10]
        hh, mi, ss = ts_raw[11:13], ts_raw[14:16], ts_raw[17:19]
        ts = f"{yyyy}-{mm}-{dd} {hh}:{mi}:{ss}"
        body = m2.group("body").strip()
        return f"{ts} - {body}"

    # ASE style: find "Day X, HH:MM:SS:" anywhere in the line
    idx = line.lower().find("day ")
    candidate = line[idx:] if idx != -1 else line

    m = DAY_TIME_RE.search(candidate)
    if not m:
        return line

    day = m.group("day")
    t = m.group("time")
    body = m.group("body").strip()

    # Remove leading "Tribe <name>, ID ...:" if still present
    body = re.sub(r"^Tribe\s+[^:]+:\s*", "", body, flags=re.IGNORECASE).strip()
    body = re.sub(r"^Tribe\s+[^,]+,\s*ID\s*\d+\s*:\s*", "", body, flags=re.IGNORECASE).strip()

    # Remove trailing "(TribeName...)" chunks
    body = re.sub(
        r"\(\s*[^)]*\b(?:%s)\b[^)]*\)\s*$" % "|".join(re.escape(k) for k in TRIBES.keys()),
        "",
        body,
        flags=re.IGNORECASE
    ).strip()

    body = body.rstrip(") ").strip()
    return f"Day {day}, {t} - {body}"

# ============================================================
# RCON (Robust Source-style implementation, supports multi-packet)
# ptype: 3=AUTH, 2=EXEC
# ============================================================
PT_AUTH = 3
PT_EXEC = 2

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

async def _read_exact(reader: asyncio.StreamReader, n: int, timeout: float) -> bytes:
    return await asyncio.wait_for(reader.readexactly(n), timeout=timeout)

async def _read_packet(reader: asyncio.StreamReader, timeout: float) -> tuple[int, int, str] | None:
    """
    Returns (req_id, ptype, body) or None on EOF/timeout.
    """
    try:
        header = await _read_exact(reader, 4, timeout)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        return None

    size = int.from_bytes(header, "little", signed=True)
    if size < 10 or size > 10_000_000:
        return None

    try:
        payload = await _read_exact(reader, size, timeout)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError):
        return None

    req_id = int.from_bytes(payload[0:4], "little", signed=True)
    ptype = int.from_bytes(payload[4:8], "little", signed=True)
    body_bytes = payload[8:-2]  # strip 2 null bytes
    body = body_bytes.decode("utf-8", errors="replace")
    return req_id, ptype, body

async def rcon_command(command: str, timeout: float = 8.0) -> str:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout)
    try:
        # AUTH
        auth_id = 1001
        writer.write(_rcon_make_packet(auth_id, PT_AUTH, RCON_PASSWORD))
        await writer.drain()

        # Read until we see auth response for auth_id, or failure (-1)
        authed = False
        end = time.time() + timeout
        while time.time() < end:
            pkt = await _read_packet(reader, timeout=0.75)
            if pkt is None:
                break
            req_id, ptype, body = pkt
            if req_id == -1:
                raise RuntimeError("RCON auth failed (bad password or protocol mismatch)")
            if req_id == auth_id:
                authed = True
                break

        if not authed:
            raise RuntimeError("RCON auth failed (no auth response)")

        # EXEC with terminator trick (helps multi-packet responses)
        cmd_id = 2001
        term_id = 2002

        writer.write(_rcon_make_packet(cmd_id, PT_EXEC, command))
        writer.write(_rcon_make_packet(term_id, PT_EXEC, ""))  # terminator
        await writer.drain()

        out_parts: list[str] = []
        end = time.time() + timeout

        while time.time() < end:
            pkt = await _read_packet(reader, timeout=0.75)
            if pkt is None:
                break
            req_id, ptype, body = pkt

            if req_id == term_id and body.strip() == "":
                break

            if req_id == cmd_id and body:
                out_parts.append(body)

        return "".join(out_parts).strip()

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
                out = await rcon_command("GetGameLog", timeout=10.0)

                lines = []
                if out:
                    for raw in out.splitlines():
                        raw = raw.strip()
                        if not raw:
                            continue
                        raw = clean_unicode(raw)
                        if raw:
                            lines.append(raw)

                # On first run, either seed dedupe or forward backlog
                if first_run and not FORWARD_BACKLOG:
                    for ln in lines:
                        _remember(_stable_hash(ln))
                        if RAWLOG_WEBHOOK:
                            _remember(_stable_hash("RAW:" + ln))
                    print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")
                    first_run = False
                    await asyncio.sleep(POLL_SECONDS)
                    continue

                # ------------------------------------------------------------
                # RAWLOG: forward ALL new lines to one webhook (optional)
                # ------------------------------------------------------------
                if RAWLOG_WEBHOOK and lines:
                    raw_new = []
                    for ln in lines:
                        if RAWLOG_FILTER and RAWLOG_FILTER.lower() not in ln.lower():
                            continue
                        h = _stable_hash("RAW:" + ln)
                        if h in sent_set:
                            continue
                        raw_new.append(ln)
                        _remember(h)

                    if raw_new:
                        raw_new = raw_new[-RAWLOG_MAX_PER_POLL:]
                        text_block = "\n".join(format_line_compact(x) for x in raw_new)

                        # Discord embed description max ~4096 chars; keep under that
                        if len(text_block) > 3500:
                            text_block = text_block[-3500:]

                        await webhook_send(session, RAWLOG_WEBHOOK, text_block, COL_DEFAULT)
                        print(f"RawLog: sent {len(raw_new)} line(s).")

                # ------------------------------------------------------------
                # Tribe routing (your original behaviour)
                # ------------------------------------------------------------
                to_send = []
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
                    print(f"Sent {len(to_send)} new tribe log(s).")
                else:
                    now = time.time()
                    if now - last_heartbeat >= HEARTBEAT_MINUTES * 60:
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