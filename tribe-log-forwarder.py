import os
import re
import time
import json
import hashlib
import asyncio
import urllib.parse
from typing import Dict, Any, Optional, Tuple, List

import aiohttp

# ============================================================
# ENV / CONFIG
# ============================================================

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = os.getenv("RCON_PORT")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

# JSON mapping tribes -> webhook/thread (future proof)
# Example:
# {
#   "Valkyrie": {"webhook": "https://discord.com/api/webhooks/.../....", "thread_id": "1459805053379547199"}
# }
TRIBE_ROUTES_RAW = os.getenv("TRIBE_ROUTES")  # preferred

# Convenience single-tribe fallback (optional)
TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Valkyrie")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")  # optional fallback
DISCORD_THREAD_ID = os.getenv("DISCORD_THREAD_ID")      # optional fallback

# Polling + behavior
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "10"))
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "10"))
SEND_BACKLOG_ON_START = os.getenv("SEND_BACKLOG_ON_START", "0").strip() in ("1", "true", "yes", "on")

# Dedupe persistence
STATE_FILE = os.getenv("STATE_FILE", "dedupe_state.json")
DEDUP_MAX = int(os.getenv("DEDUP_MAX", "5000"))  # how many sent hashes to remember

# ============================================================
# VALIDATION
# ============================================================

missing = []
for k in ("RCON_HOST", "RCON_PORT", "RCON_PASSWORD"):
    if not os.getenv(k):
        missing.append(k)

if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

try:
    RCON_PORT = int(RCON_PORT)
except Exception:
    raise RuntimeError("RCON_PORT must be an integer")

# ============================================================
# ROUTES (tribe -> webhook/thread)
# ============================================================

def _strip_webhook_and_extract_thread(webhook_url: str, thread_id: Optional[str]) -> Tuple[str, Optional[str]]:
    """
    Accepts:
      - base webhook
      - webhook that includes ?Thread=... (user pasted)
      - webhook that includes ?thread_id=...
    Returns (base_webhook_url, thread_id)
    """
    if not webhook_url:
        return webhook_url, thread_id

    parsed = urllib.parse.urlparse(webhook_url)
    qs = urllib.parse.parse_qs(parsed.query)

    # Some people paste ?Thread=... (capital T) — extract it
    extracted = None
    if "thread_id" in qs and qs["thread_id"]:
        extracted = qs["thread_id"][0]
    elif "Thread" in qs and qs["Thread"]:
        extracted = qs["Thread"][0]
    elif "thread" in qs and qs["thread"]:
        extracted = qs["thread"][0]

    base = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    final_thread = thread_id or extracted
    return base, final_thread

def load_routes() -> Dict[str, Dict[str, Optional[str]]]:
    routes: Dict[str, Dict[str, Optional[str]]] = {}

    if TRIBE_ROUTES_RAW:
        try:
            obj = json.loads(TRIBE_ROUTES_RAW)
            if not isinstance(obj, dict):
                raise ValueError("TRIBE_ROUTES must be a JSON object")
            for tribe, cfg in obj.items():
                if not isinstance(cfg, dict):
                    continue
                wh = (cfg.get("webhook") or "").strip()
                th = (cfg.get("thread_id") or "").strip() or None
                wh, th = _strip_webhook_and_extract_thread(wh, th)
                if wh:
                    routes[str(tribe)] = {"webhook": wh, "thread_id": th}
        except Exception as e:
            raise RuntimeError(f"TRIBE_ROUTES is not valid JSON: {e}")

    # Fallback single-tribe mode if TRIBE_ROUTES not supplied
    if not routes:
        if not DISCORD_WEBHOOK_URL:
            raise RuntimeError(
                "Missing required environment variables: TRIBE_ROUTES (preferred) OR DISCORD_WEBHOOK_URL (fallback)"
            )
        wh, th = _strip_webhook_and_extract_thread(DISCORD_WEBHOOK_URL.strip(), DISCORD_THREAD_ID)
        routes[TARGET_TRIBE] = {"webhook": wh, "thread_id": th}

    return routes

TRIBE_ROUTES = load_routes()
TRIBES = list(TRIBE_ROUTES.keys())

print(f"Routing tribes: {', '.join(TRIBES)}")
for t, cfg in TRIBE_ROUTES.items():
    print(f" - {t} -> webhook={'set' if cfg.get('webhook') else 'missing'} thread_id={cfg.get('thread_id')}")

# ============================================================
# DEDUPE STATE
# ============================================================

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"sent": [], "last_heartbeat_ts": 0.0}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"sent": [], "last_heartbeat_ts": 0.0}
        data.setdefault("sent", [])
        data.setdefault("last_heartbeat_ts", 0.0)
        return data
    except Exception:
        return {"sent": [], "last_heartbeat_ts": 0.0}

def save_state(state: Dict[str, Any]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass

STATE = load_state()

def _hash_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()

def dedupe_seen(h: str) -> bool:
    return h in set(STATE.get("sent", []))

def dedupe_add(h: str) -> None:
    sent: List[str] = STATE.get("sent", [])
    sent.append(h)
    if len(sent) > DEDUP_MAX:
        # keep the newest
        sent[:] = sent[-DEDUP_MAX:]
    STATE["sent"] = sent

# ============================================================
# RCON (Minimal Source RCON protocol)
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

async def rcon_command(command: str, timeout: float = 8.0) -> str:
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

        # Parse response packets
        out = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i:i+4], "little", signed=True)
            i += 4
            if i + size > len(data) or size < 10:
                break
            pkt = data[i:i+size]
            i += size
            body = pkt[8:-2]  # req_id + type + body + 2 nulls

            # IMPORTANT: keep special chars; do NOT ignore.
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
# LOG PARSING / FORMATTING
# ============================================================

# remove ARK RichColor tags etc
TAG_RE = re.compile(r"<[^>]+>")
# Extract: Day X, HH:MM:SS: message
DAY_RE = re.compile(r"Day\s+(\d+),\s*([0-9]{1,2}:[0-9]{2}:[0-9]{2})\s*:\s*(.*)", re.IGNORECASE)

def clean_ark_text(s: str) -> str:
    s = s.strip()

    # strip leading timestamp blocks like: [2026.01.10-08.24.46:966][441]
    if s.startswith("["):
        # remove one or more bracket blocks at front
        while s.startswith("[") and "]" in s:
            s = s.split("]", 1)[1].lstrip()
            if s.startswith("[") and "]" in s:
                continue
            break

    # remove RichColor and any tags
    s = TAG_RE.sub("", s).strip()

    # Common junk endings we’ve seen from ARK log formatting
    # e.g. "</>)", "!)", ">)", "))"
    s = re.sub(r"\s*(</\)>\)?|</\)>\s*$)", "", s)  # very defensive
    s = re.sub(r"\s*[<]/?\)?\s*$", "", s)
    s = re.sub(r"\s*\)+\s*$", "", s)  # remove trailing ))) etc
    s = re.sub(r"\s*!\)+\s*$", "!", s)  # "!)" -> "!"
    s = s.strip()
    return s

def extract_compact_line(raw_line: str, tribe: str) -> Optional[str]:
    """
    Convert a raw GetGameLog line into:
      Day 221, 22:51:49 - Sir Magnus claimed 'Roan Pinto - Lvl 150'
    Only returns lines matching "Tribe <tribe>".
    """
    line = clean_ark_text(raw_line)

    # Must contain the tribe marker
    if f"Tribe {tribe}".lower() not in line.lower():
        return None

    # Find the "Day X, HH:MM:SS: ..." portion
    m = DAY_RE.search(line)
    if not m:
        return None

    day = m.group(1)
    t = m.group(2)
    msg = m.group(3).strip()

    # msg often begins like: "Tribe Valkyrie, ID ...: Day ...: <player> did thing"
    # We want "player did thing", so remove "Tribe <name>, ID ....:" prefix if present.
    # Keep it flexible:
    msg = re.sub(rf"^Tribe\s+{re.escape(tribe)}\s*,\s*ID\s*\d+\s*:\s*", "", msg, flags=re.IGNORECASE).strip()

    # Now msg might still include "Day X, time:" (double)
    msg = DAY_RE.sub(lambda mm: mm.group(3), msg).strip()

    # Final compact format:
    compact = f"Day {day}, {t} - {msg}"

    # Remove any remaining trailing weirdness again
    compact = clean_ark_text(compact)

    return compact if compact else None

def color_for_event(text: str) -> int:
    lower = text.lower()
    # your color scheme:
    if "claimed" in lower or "unclaimed" in lower or "claiming" in lower:
        return 0x9B59B6  # purple
    if "tamed" in lower or "taming" in lower:
        return 0x2ECC71  # green
    if "killed" in lower or "was killed" in lower or "died" in lower or "death" in lower:
        return 0xE74C3C  # red
    if "demolished" in lower or "destroyed" in lower:
        return 0xF1C40F  # yellow
    return 0x95A5A6  # default grey

def build_embed(compact: str) -> Dict[str, Any]:
    return {
        "embeds": [
            {
                "description": compact,
                "color": color_for_event(compact),
            }
        ],
        "allowed_mentions": {"parse": []},
    }

# ============================================================
# DISCORD WEBHOOK SENDER (forum thread support + 429 handling)
# ============================================================

async def post_webhook(
    session: aiohttp.ClientSession,
    webhook_url: str,
    payload: Dict[str, Any],
    thread_id: Optional[str] = None,
) -> None:
    params = {}
    if thread_id:
        params["thread_id"] = str(thread_id)

    # Retry on 429
    for attempt in range(1, 6):
        async with session.post(webhook_url, params=params, json=payload) as r:
            if r.status in (200, 204):
                return

            txt = await r.text()

            if r.status == 429:
                try:
                    data = json.loads(txt)
                    retry_after = float(data.get("retry_after", 1.0))
                except Exception:
                    retry_after = 1.0
                await asyncio.sleep(max(0.2, retry_after))
                continue

            raise RuntimeError(f"Discord webhook error {r.status}: {txt}")

# ============================================================
# MAIN LOOP
# ============================================================

async def main():
    print(f"Polling every {POLL_INTERVAL_SECONDS:.1f}s")
    print(f"Heartbeat every {HEARTBEAT_MINUTES} minutes")

    async with aiohttp.ClientSession() as session:
        # Seed dedupe on start unless backlog requested
        if not SEND_BACKLOG_ON_START:
            print("First run: seeding dedupe from current GetGameLog output (no backlog spam).")
            try:
                out = await rcon_command("GetGameLog", timeout=12.0)
                for line in out.splitlines():
                    for tribe in TRIBES:
                        compact = extract_compact_line(line, tribe)
                        if compact:
                            dedupe_add(_hash_text(f"{tribe}|{compact}"))
                save_state(STATE)
            except Exception as e:
                print(f"[WARN] Could not seed dedupe: {e}")

        last_new_ts = time.time()

        while True:
            try:
                out = await rcon_command("GetGameLog", timeout=12.0)
                lines = out.splitlines() if out else []

                sent_any = False

                # If backlog mode, we will send everything we haven't seen yet.
                # Otherwise we still rely on dedupe to prevent repeats.
                for raw in lines:
                    for tribe, route in TRIBE_ROUTES.items():
                        compact = extract_compact_line(raw, tribe)
                        if not compact:
                            continue

                        h = _hash_text(f"{tribe}|{compact}")
                        if dedupe_seen(h):
                            continue

                        payload = build_embed(compact)
                        await post_webhook(
                            session,
                            route["webhook"],
                            payload,
                            route.get("thread_id"),
                        )

                        dedupe_add(h)
                        sent_any = True
                        last_new_ts = time.time()

                if sent_any:
                    save_state(STATE)
                else:
                    # heartbeat
                    now = time.time()
                    last_hb = float(STATE.get("last_heartbeat_ts", 0.0) or 0.0)
                    if (now - last_hb) >= (HEARTBEAT_MINUTES * 60):
                        # Send heartbeat to each route (or just Valkyrie now)
                        for tribe, route in TRIBE_ROUTES.items():
                            hb_text = f"⏱️ No new logs since last check. (Tribe: {tribe})"
                            hb_payload = {
                                "content": hb_text,
                                "allowed_mentions": {"parse": []},
                            }
                            await post_webhook(
                                session,
                                route["webhook"],
                                hb_payload,
                                route.get("thread_id"),
                            )
                        STATE["last_heartbeat_ts"] = now
                        save_state(STATE)
                        print("Heartbeat sent (no new logs).")

            except Exception as e:
                print(f"[ERROR] {e}")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())