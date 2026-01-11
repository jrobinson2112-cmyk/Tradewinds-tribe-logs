import os
import json
import time
import asyncio
import hashlib
import re
import aiohttp

# =====================
# ENV
# =====================
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "0"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "60"))

TRIBE_ROUTES_RAW = os.getenv("TRIBE_ROUTES")

missing = []
for k in ["RCON_HOST", "RCON_PORT", "RCON_PASSWORD", "TRIBE_ROUTES"]:
    if not os.getenv(k):
        missing.append(k)
if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

TRIBE_ROUTES = json.loads(TRIBE_ROUTES_RAW)
print("Routing tribes:", ", ".join(TRIBE_ROUTES.keys()))

# =====================
# COLORS (as requested)
# =====================
COLORS = {
    "RED": 0xE74C3C,      # killed/died/death/destroyed
    "YELLOW": 0xF1C40F,   # demolished
    "PURPLE": 0x9B59B6,   # claimed/unclaimed
    "GREEN": 0x2ECC71,    # tamed
    "BLUE": 0x5DADE2,     # alliance
    "WHITE": 0xECF0F1,    # everything else (froze)
}

# =====================
# CLEANUP + FORMAT
# =====================
RICHCOLOR_RE = re.compile(r"<RichColor[^>]*>", re.IGNORECASE)
ENDTAG_RE = re.compile(r"</>", re.IGNORECASE)

DAYSTAMP_RE = re.compile(r"^Day\s+(?P<day>\d+),\s*(?P<time>\d{2}:\d{2}:\d{2})\s*:\s*(?P<msg>.+)$")

def clean_ark_markup(s: str) -> str:
    # remove RichColor + </>
    s = RICHCOLOR_RE.sub("", s)
    s = ENDTAG_RE.sub("", s)
    return s.strip()

def tidy_trailing_punct(s: str) -> str:
    """
    Removes the annoying ARK endings like:
      ... (Thing)!)
      ... (Thing)!))
      ... 'Name'!)
    while keeping the meaningful final ')', quotes, etc.
    """
    s = s.strip()

    # common endings: ")!)", ")!))" -> ")"
    s = re.sub(r"\)\s*!+\s*\)+\s*$", ")", s)

    # endings like "'!)" or "'!))" -> "'"
    s = re.sub(r"'\s*!+\s*\)+\s*$", "'", s)

    # fallback: trailing "!))" -> ""
    s = re.sub(r"\s*!+\s*\)+\s*$", "", s)

    return s.strip()

def extract_day_time_who_what(raw_line: str) -> str | None:
    """
    Returns exactly:
      Day X, HH:MM:SS: Who What
    or None if no in-game day/time stamp exists.
    """
    s = clean_ark_markup(raw_line)

    # Find the FIRST "Day " occurrence and take from there
    idx = s.lower().find("day ")
    if idx == -1:
        return None

    tail = s[idx:].strip()

    m = DAYSTAMP_RE.match(tail)
    if not m:
        return None

    day = m.group("day")
    t = m.group("time")
    msg = tidy_trailing_punct(m.group("msg").strip())

    # Final output exactly like your "froze" example
    return f"Day {day}, {t}: {msg}"

def classify_color(formatted_line: str) -> int:
    t = formatted_line.lower()

    # Red
    if any(x in t for x in ["killed", "died", "death", "destroyed", "starved"]):
        return COLORS["RED"]

    # Yellow
    if "demolished" in t:
        return COLORS["YELLOW"]

    # Purple
    if any(x in t for x in ["claimed", "unclaimed"]):
        return COLORS["PURPLE"]

    # Green
    if "tamed" in t:
        return COLORS["GREEN"]

    # Light blue
    if "alliance" in t:
        return COLORS["BLUE"]

    # White for everything else (froze etc)
    return COLORS["WHITE"]

# =====================
# RCON
# =====================
def _rcon_packet(req_id: int, ptype: int, body: str) -> bytes:
    data = body.encode("utf-8") + b"\x00"
    pkt = (
        req_id.to_bytes(4, "little", signed=True)
        + ptype.to_bytes(4, "little", signed=True)
        + data
        + b"\x00"
    )
    return len(pkt).to_bytes(4, "little", signed=True) + pkt

async def rcon_command(cmd: str, timeout: float = 6.0) -> str:
    reader, writer = await asyncio.open_connection(RCON_HOST, RCON_PORT)
    try:
        # auth
        writer.write(_rcon_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        await reader.read(4096)

        # command
        writer.write(_rcon_packet(2, 2, cmd))
        await writer.drain()

        data = b""
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=0.3)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            data += chunk

        out = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i:i+4], "little", signed=True)
            i += 4
            if size < 10 or i + size > len(data):
                break
            pkt = data[i:i+size]
            i += size
            out.append(pkt[8:-2].decode("utf-8", errors="ignore"))

        return "\n".join(out).strip()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

# =====================
# STATE
# =====================
seen_hashes = set()
last_activity_ts = time.time()
last_heartbeat_ts = 0.0

# =====================
# DISCORD SEND
# =====================
async def send_to_discord(tribe: str, text: str, color: int):
    route = TRIBE_ROUTES[tribe]
    payload = {"embeds": [{"description": text, "color": color}]}

    params = {"wait": "true"}
    thread_id = route.get("thread_id")
    if thread_id:
        params["thread_id"] = str(thread_id)

    async with aiohttp.ClientSession() as s:
        async with s.post(route["webhook"], params=params, json=payload) as r:
            if r.status >= 400:
                body = await r.text()
                print(f"Discord webhook error {r.status}: {body}")

async def heartbeat_if_idle():
    global last_heartbeat_ts
    now = time.time()

    # Only if NO activity for HEARTBEAT_MINUTES
    if (now - last_activity_ts) < (HEARTBEAT_MINUTES * 60):
        return

    # Don’t spam; once per HEARTBEAT_MINUTES while idle
    if (now - last_heartbeat_ts) < (HEARTBEAT_MINUTES * 60):
        return

    for tribe in TRIBE_ROUTES:
        await send_to_discord(
            tribe,
            f"⏱️ No new logs since last check. (Tribe: {tribe})",
            COLORS["WHITE"]
        )

    last_heartbeat_ts = now

# =====================
# MAIN
# =====================
async def main():
    global last_activity_ts

    print("Starting RCON GetGameLog polling…")

    # Seed dedupe so we don’t backlog spam on boot
    seed = await rcon_command("GetGameLog")
    for line in seed.splitlines():
        seen_hashes.add(hashlib.sha256(line.encode("utf-8", errors="ignore")).hexdigest())

    print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")

    while True:
        try:
            output = await rcon_command("GetGameLog")

            for raw in output.splitlines():
                h = hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)

                formatted = extract_day_time_who_what(raw)
                if not formatted:
                    continue

                # route by tribe name presence (use raw to detect tribe, formatted to display)
                for tribe in TRIBE_ROUTES:
                    if f"Tribe {tribe}".lower() in raw.lower() or f"({tribe}".lower() in raw.lower():
                        color = classify_color(formatted)
                        await send_to_discord(tribe, formatted, color)
                        last_activity_ts = time.time()

            await heartbeat_if_idle()

        except Exception as e:
            print("Error:", e)

        await asyncio.sleep(POLL_SECONDS)

asyncio.run(main())