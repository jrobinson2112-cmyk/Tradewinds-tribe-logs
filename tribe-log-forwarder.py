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

TRIBE_ROUTES = os.getenv("TRIBE_ROUTES")

if not all([RCON_HOST, RCON_PASSWORD, TRIBE_ROUTES]):
    raise RuntimeError("Missing required environment variables")

TRIBE_ROUTES = json.loads(TRIBE_ROUTES)

print("Routing tribes:", ", ".join(t["tribe"] for t in TRIBE_ROUTES))

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
    text = RICHCOLOR_RE.sub("", text)
    text = text.replace("!)", ")").replace("!)", ")")
    return text.strip()

def classify_color(text: str) -> int:
    t = text.lower()

    if any(x in t for x in ["killed", "died", "death", "destroyed", "starved"]):
        return COLORS["RED"]

    # UNCLAIMED → YELLOW (changed)
    if "unclaimed" in t:
        return COLORS["YELLOW"]

    if "demolished" in t:
        return COLORS["YELLOW"]

    # CLAIMED → PURPLE
    if "claimed" in t:
        return COLORS["PURPLE"]

    if "tamed" in t:
        return COLORS["GREEN"]

    if "alliance" in t:
        return COLORS["BLUE"]

    return COLORS["WHITE"]

def extract_core(line: str) -> str | None:
    """
    Extract only:
    Day XXX, HH:MM:SS - WHO ACTION
    """
    m = re.search(
        r"Day\s+(\d+),\s+(\d{2}:\d{2}:\d{2}):\s+(.*)",
        line,
    )
    if not m:
        return None

    day, time_, rest = m.groups()
    rest = clean_text(rest)

    return f"Day {day}, {time_} - {rest}"

# =========================
# RCON
# =========================
def rcon_packet(req_id, ptype, body):
    data = body.encode() + b"\x00"
    pkt = req_id.to_bytes(4, "little", signed=True)
    pkt += ptype.to_bytes(4, "little", signed=True)
    pkt += data + b"\x00"
    return len(pkt).to_bytes(4, "little", signed=True) + pkt

async def rcon_command(cmd: str) -> str:
    reader, writer = await asyncio.open_connection(RCON_HOST, RCON_PORT)
    try:
        writer.write(rcon_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        await reader.read(4096)

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

        text = out.decode(errors="ignore")
        return text
    finally:
        writer.close()
        await writer.wait_closed()

# =========================
# DISCORD
# =========================
async def send_webhook(session, route, text, color):
    payload = {
        "embeds": [{
            "description": text,
            "color": color
        }]
    }

    params = {}
    if route.get("thread_id"):
        params["thread_id"] = route["thread_id"]

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

                sent = False

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
                        sent = True

                # Heartbeat ONLY if no activity
                if not sent and time.time() - last_activity >= HEARTBEAT_MINUTES * 60:
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