import os
import time
import json
import asyncio
import re
import aiohttp

# =========================
# ENV
# =========================
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "27020"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "60"))

TRIBE_ROUTES_RAW = os.getenv("TRIBE_ROUTES")

missing = []
for k in ["RCON_HOST", "RCON_PASSWORD", "TRIBE_ROUTES"]:
    if not os.getenv(k):
        missing.append(k)
if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

def load_routes(raw: str):
    """
    Railway sometimes stores JSON as a quoted string (double-encoded).
    This unwraps JSON up to 3 times until it becomes a list/dict.
    """
    val = raw
    for _ in range(3):
        if isinstance(val, (list, dict)):
            break
        if isinstance(val, str):
            val = val.strip()
            val = json.loads(val)
        else:
            break

    if isinstance(val, dict):
        val = [val]
    if not isinstance(val, list):
        raise RuntimeError("TRIBE_ROUTES must be a JSON list of objects (or a single object).")

    # basic validation
    for i, r in enumerate(val):
        if not isinstance(r, dict):
            raise RuntimeError(f"TRIBE_ROUTES item {i} is not an object.")
        if "tribe" not in r or "webhook" not in r:
            raise RuntimeError(f"TRIBE_ROUTES item {i} must include 'tribe' and 'webhook'.")
        # thread_id optional
    return val

TRIBE_ROUTES = load_routes(TRIBE_ROUTES_RAW)
print("Routing tribes:", ", ".join(r["tribe"] for r in TRIBE_ROUTES))

# =========================
# COLOURS
# =========================
COLORS = {
    "RED": 0xE74C3C,
    "YELLOW": 0xF1C40F,
    "PURPLE": 0x9B59B6,
    "GREEN": 0x2ECC71,
    "BLUE": 0x5DADE2,
    "WHITE": 0xECF0F1,
}

# =========================
# HELPERS
# =========================
RICHCOLOR_RE = re.compile(r"<RichColor[^>]*>")

def clean_text(text: str) -> str:
    # remove RichColor tags
    text = RICHCOLOR_RE.sub("", text)

    # remove trailing Ark formatting artifacts
    text = text.replace("</>)", "").replace("</>", "")
    text = text.replace("!)", ")").replace("!'","'").replace("!'", "'")

    # remove lingering extra ! at end like ...')!
    text = re.sub(r"'\)\!$", "')", text)

    # collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text

def classify_color(text: str) -> int:
    t = text.lower()

    # Red - Killed / Died / Death / Destroyed
    if any(x in t for x in ["killed", "died", "death", "destroyed", "starved"]):
        return COLORS["RED"]

    # Yellow - Demolished + Unclaimed (your change)
    if "demolished" in t or "unclaimed" in t:
        return COLORS["YELLOW"]

    # Purple - Claimed
    if "claimed" in t:
        return COLORS["PURPLE"]

    # Green - Tamed
    if "tamed" in t:
        return COLORS["GREEN"]

    # Light blue - Alliance
    if "alliance" in t:
        return COLORS["BLUE"]

    # White - Anything else (froze etc.)
    return COLORS["WHITE"]

def extract_core(line: str) -> str | None:
    """
    Output ONLY:
    Day XXX, HH:MM:SS - WHO ACTION
    """
    m = re.search(r"Day\s+(\d+),\s+(\d{2}:\d{2}:\d{2}):\s+(.*)", line)
    if not m:
        return None

    day, time_, rest = m.groups()
    rest = clean_text(rest)
    return f"Day {day}, {time_} - {rest}"

# =========================
# RCON (minimal Source RCON)
# =========================
def rcon_packet(req_id, ptype, body):
    data = body.encode("utf-8", errors="ignore") + b"\x00"
    pkt = req_id.to_bytes(4, "little", signed=True)
    pkt += ptype.to_bytes(4, "little", signed=True)
    pkt += data + b"\x00"
    return len(pkt).to_bytes(4, "little", signed=True) + pkt

async def rcon_command(cmd: str) -> str:
    reader, writer = await asyncio.open_connection(RCON_HOST, RCON_PORT)
    try:
        # auth
        writer.write(rcon_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        await reader.read(4096)

        # command
        writer.write(rcon_packet(2, 2, cmd))
        await writer.drain()

        out = b""
        while True:
            try:
                part = await asyncio.wait_for(reader.read(4096), timeout=0.3)
            except asyncio.TimeoutError:
                break
            if not part:
                break
            out += part

        return out.decode("utf-8", errors="ignore")
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

# =========================
# DISCORD WEBHOOK
# =========================
async def send_webhook(session, route, text, color):
    payload = {"embeds": [{"description": text, "color": color}]}

    params = {}
    if route.get("thread_id"):
        params["thread_id"] = str(route["thread_id"])

    async with session.post(route["webhook"], json=payload, params=params) as r:
        if r.status >= 300:
            body = await r.text()
            print("Discord webhook error", r.status, body)

# =========================
# MAIN LOOP
# =========================
async def main():
    last_seen = set()
    last_activity = time.time()

    async with aiohttp.ClientSession() as session:
        print("Starting RCON GetGameLog loop")

        while True:
            try:
                raw = await rcon_command("GetGameLog")
                lines = raw.splitlines()

                sent_any = False

                for line in lines:
                    for route in TRIBE_ROUTES:
                        tribe = route["tribe"]

                        if f"Tribe {tribe}" not in line:
                            continue

                        core = extract_core(line)
                        if not core:
                            continue

                        if core in last_seen:
                            continue

                        last_seen.add(core)
                        color = classify_color(core)

                        await send_webhook(session, route, core, color)
                        last_activity = time.time()
                        sent_any = True

                # Heartbeat ONLY if no activity for HEARTBEAT_MINUTES
                if (not sent_any) and (time.time() - last_activity >= HEARTBEAT_MINUTES * 60):
                    for route in TRIBE_ROUTES:
                        await send_webhook(
                            session,
                            route,
                            f"⏱️ No new logs since last check. (Tribe: {route['tribe']})",
                            COLORS["WHITE"],
                        )
                    last_activity = time.time()

            except Exception as e:
                print("Error:", e)

            await asyncio.sleep(POLL_SECONDS)

asyncio.run(main())