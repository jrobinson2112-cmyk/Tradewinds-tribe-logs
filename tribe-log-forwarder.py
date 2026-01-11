import os
import json
import time
import asyncio
import socket
import struct
import re
import aiohttp

# =========================
# ENV + ROUTES
# =========================
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "0"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")
TRIBE_ROUTES_RAW = os.getenv("TRIBE_ROUTES")

if not all([RCON_HOST, RCON_PORT, RCON_PASSWORD, TRIBE_ROUTES_RAW]):
    raise RuntimeError("Missing required environment variables.")

try:
    TRIBE_ROUTES = json.loads(TRIBE_ROUTES_RAW)
except Exception:
    raise RuntimeError("TRIBE_ROUTES must be valid JSON.")

if not isinstance(TRIBE_ROUTES, list) or not TRIBE_ROUTES:
    raise RuntimeError("TRIBE_ROUTES must be a JSON array with at least one route.")

print("Routing tribes:", ", ".join(r["tribe"] for r in TRIBE_ROUTES))

# =========================
# CONSTANTS
# =========================
POLL_SECONDS = 15
HEARTBEAT_SECONDS = 3600

# =========================
# COLORS
# =========================
COLORS = {
    "red": 0xE74C3C,
    "yellow": 0xF1C40F,
    "purple": 0x9B59B6,
    "green": 0x2ECC71,
    "blue": 0x5DADE2,
    "white": 0xECF0F1,
}

# =========================
# HELPERS
# =========================
def decode_rcon_text(b: bytes) -> str:
    try:
        s = b.decode("utf-8")
        if "\ufffd" in s:
            raise UnicodeError
        return s
    except Exception:
        return b.decode("latin-1", errors="replace")

def strip_rich_color(text: str) -> str:
    return re.sub(r"<RichColor[^>]*>", "", text)

def classify_color(text: str) -> int:
    t = text.lower()
    if any(k in t for k in ["killed", "died", "death", "destroyed"]):
        return COLORS["red"]
    if "demolished" in t or "unclaimed" in t:
        return COLORS["yellow"]
    if "claimed" in t:
        return COLORS["purple"]
    if "tamed" in t:
        return COLORS["green"]
    if "alliance" in t:
        return COLORS["blue"]
    return COLORS["white"]

def extract_core(line: str) -> str | None:
    m = re.search(r"(Day \d+,\s\d{2}:\d{2}:\d{2}:\s.*)", line)
    if not m:
        return None
    text = strip_rich_color(m.group(1))
    text = text.replace("!)", ")").replace("!'","'")
    return text.strip()

# =========================
# RCON
# =========================
def rcon_packet(req_id, cmd_type, payload):
    data = payload.encode("utf-8") + b"\x00"
    pkt = struct.pack("<ii", req_id, cmd_type) + data + b"\x00"
    return struct.pack("<i", len(pkt)) + pkt

async def get_game_log():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect((RCON_HOST, RCON_PORT))

    s.sendall(rcon_packet(1, 3, RCON_PASSWORD))
    s.recv(4096)

    s.sendall(rcon_packet(2, 2, "GetGameLog"))
    data = b""
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        pass
    s.close()

    out = []
    i = 0
    while i + 4 <= len(data):
        size = struct.unpack("<i", data[i:i+4])[0]
        i += 4
        if i + size > len(data):
            break
        pkt = data[i:i+size]
        i += size
        body = pkt[8:-2]
        txt = decode_rcon_text(body)
        if txt:
            out.append(txt)

    return "\n".join(out)

# =========================
# DISCORD
# =========================
async def send_to_discord(route, text, color):
    url = route["webhook"]
    thread_id = route["thread_id"]

    payload = {
        "embeds": [{
            "description": text,
            "color": color
        }],
        "thread_id": thread_id
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as r:
            if r.status >= 400:
                print("Discord error", r.status, await r.text())

# =========================
# MAIN LOOP
# =========================
seen = set()
last_activity = time.time()

async def main():
    global last_activity
    while True:
        try:
            raw = await get_game_log()
            for line in raw.splitlines():
                core = extract_core(line)
                if not core:
                    continue

                for route in TRIBE_ROUTES:
                    if route["tribe"].lower() in core.lower():
                        if core in seen:
                            continue
                        seen.add(core)

                        color = classify_color(core)
                        await send_to_discord(route, core, color)
                        last_activity = time.time()

            if time.time() - last_activity >= HEARTBEAT_SECONDS:
                for route in TRIBE_ROUTES:
                    await send_to_discord(
                        route,
                        f"⏱️ No new logs since last check. (Tribe: {route['tribe']})",
                        COLORS["white"]
                    )
                last_activity = time.time()

        except Exception as e:
            print("Error:", e)

        await asyncio.sleep(POLL_SECONDS)

asyncio.run(main())