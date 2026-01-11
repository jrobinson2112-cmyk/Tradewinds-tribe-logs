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
# COLORS
# =====================
COLORS = {
    "RED": 0xE74C3C,
    "YELLOW": 0xF1C40F,
    "PURPLE": 0x9B59B6,
    "GREEN": 0x2ECC71,
    "BLUE": 0x5DADE2,
    "WHITE": 0xECF0F1,
}

# =====================
# CLEANUP
# =====================
RICHCOLOR_RE = re.compile(r"<\/?>|<RichColor[^>]*>", re.IGNORECASE)

def clean_ark_text(text: str) -> str:
    return RICHCOLOR_RE.sub("", text).strip()

def classify_color(text: str) -> int:
    t = text.lower()
    if any(x in t for x in ["killed", "died", "death", "destroyed", "starved"]):
        return COLORS["RED"]
    if "demolished" in t:
        return COLORS["YELLOW"]
    if any(x in t for x in ["claimed", "unclaimed"]):
        return COLORS["PURPLE"]
    if "tamed" in t:
        return COLORS["GREEN"]
    if "alliance" in t:
        return COLORS["BLUE"]
    return COLORS["WHITE"]

# =====================
# RCON
# =====================
def _rcon_packet(req_id, ptype, body):
    data = body.encode("utf-8") + b"\x00"
    pkt = req_id.to_bytes(4,"little",signed=True) + ptype.to_bytes(4,"little",signed=True) + data + b"\x00"
    return len(pkt).to_bytes(4,"little",signed=True) + pkt

async def rcon_command(cmd, timeout=6):
    reader, writer = await asyncio.open_connection(RCON_HOST, RCON_PORT)
    writer.write(_rcon_packet(1, 3, RCON_PASSWORD))
    await writer.drain()
    await reader.read(4096)

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

    writer.close()
    await writer.wait_closed()

    out = []
    i = 0
    while i + 4 <= len(data):
        size = int.from_bytes(data[i:i+4], "little", signed=True)
        i += 4
        pkt = data[i:i+size]
        i += size
        out.append(pkt[8:-2].decode("utf-8", errors="ignore"))

    return "\n".join(out)

# =====================
# STATE
# =====================
seen_hashes = set()
last_activity_ts = time.time()
last_heartbeat_ts = 0

# =====================
# DISCORD
# =====================
async def send_log(tribe, text, color):
    route = TRIBE_ROUTES[tribe]
    payload = {
        "embeds": [{
            "description": text,
            "color": color
        }]
    }

    params = {"wait": "true"}
    if route.get("thread_id"):
        params["thread_id"] = route["thread_id"]

    async with aiohttp.ClientSession() as s:
        async with s.post(route["webhook"], params=params, json=payload) as r:
            if r.status >= 400:
                body = await r.text()
                print("Discord error:", r.status, body)

async def send_heartbeat():
    global last_heartbeat_ts
    now = time.time()
    if now - last_activity_ts < HEARTBEAT_MINUTES * 60:
        return
    if now - last_heartbeat_ts < HEARTBEAT_MINUTES * 60:
        return

    for tribe in TRIBE_ROUTES:
        await send_log(
            tribe,
            f"⏱️ No new logs since last check. (Tribe: {tribe})",
            COLORS["WHITE"]
        )

    last_heartbeat_ts = now

# =====================
# MAIN LOOP
# =====================
async def main():
    global last_activity_ts

    print("Starting RCON GetGameLog polling…")
    seed = await rcon_command("GetGameLog")
    for line in seed.splitlines():
        seen_hashes.add(hashlib.sha256(line.encode()).hexdigest())

    print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")

    while True:
        try:
            output = await rcon_command("GetGameLog")
            for line in output.splitlines():
                h = hashlib.sha256(line.encode()).hexdigest()
                if h in seen_hashes:
                    continue

                seen_hashes.add(h)
                clean = clean_ark_text(line)

                for tribe in TRIBE_ROUTES:
                    if f"Tribe {tribe}" in clean:
                        color = classify_color(clean)
                        await send_log(tribe, clean, color)
                        last_activity_ts = time.time()

            await send_heartbeat()

        except Exception as e:
            print("Error:", e)

        await asyncio.sleep(POLL_SECONDS)

asyncio.run(main())