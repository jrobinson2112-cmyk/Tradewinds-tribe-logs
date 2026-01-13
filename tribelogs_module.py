import os
import json
import time
import asyncio
import aiohttp
import re
import discord
from discord import app_commands

# ======================
# ENV
# ======================
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "0"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

ADMIN_ROLE_ID = 1439069787207766076

DATA_DIR = "/data"
ROUTES_FILE = f"{DATA_DIR}/tribe_routes.json"
STATE_FILE = f"{DATA_DIR}/tribe_state.json"

POLL_SECONDS = 30
HEARTBEAT_SECONDS = 3600

# ======================
# STORAGE
# ======================
os.makedirs(DATA_DIR, exist_ok=True)

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

TRIBE_ROUTES = load_json(ROUTES_FILE, [])
STATE = load_json(STATE_FILE, {
    "last_hash": None,
    "last_activity": time.time()
})

# ======================
# RCON (WORKING)
# ======================
def _packet(req_id, ptype, body):
    body = body.encode("utf-8") + b"\x00"
    pkt = (
        req_id.to_bytes(4, "little", signed=True)
        + ptype.to_bytes(4, "little", signed=True)
        + body
        + b"\x00"
    )
    return len(pkt).to_bytes(4, "little", signed=True) + pkt

async def rcon(cmd, timeout=6):
    reader, writer = await asyncio.open_connection(RCON_HOST, RCON_PORT)
    try:
        writer.write(_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        await reader.read(4096)

        writer.write(_packet(2, 2, cmd))
        await writer.drain()

        data = await asyncio.wait_for(reader.read(65535), timeout)
        out = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i:i+4], "little", signed=True)
            i += 4
            pkt = data[i:i+size]
            i += size
            out.append(pkt[8:-2].decode("utf-8", errors="ignore"))
        return "".join(out).strip()
    finally:
        writer.close()
        await writer.wait_closed()

# ======================
# GAMELOG PARSER (KNOWN FORMAT)
# ======================
LOG_LINE = re.compile(
    r"^Day\s+\d+,\s+\d{2}:\d{2}:\d{2}\s+-\s+.+",
    re.MULTILINE
)

def extract_lines(text):
    return LOG_LINE.findall(text)

def hash_line(line):
    return hash(line)

# ======================
# DISCORD SEND
# ======================
async def send(webhook, thread_id, content):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{webhook}?wait=true&thread_id={thread_id}",
            json={"content": content}
        ) as r:
            if r.status not in (200, 204):
                raise RuntimeError(await r.text())

# ======================
# MAIN LOOP
# ======================
async def _tribelogs_loop():
    print(f"Routing tribes: {[t['tribe'] for t in TRIBE_ROUTES]}")
    last_heartbeat = time.time()

    while True:
        try:
            text = await rcon("GetGameLog")
            if not text:
                await asyncio.sleep(POLL_SECONDS)
                continue

            lines = extract_lines(text)
            if not lines:
                await asyncio.sleep(POLL_SECONDS)
                continue

            new = []
            for line in lines:
                h = hash_line(line)
                if h == STATE["last_hash"]:
                    break
                new.append(line)

            if new:
                for line in reversed(new):
                    for route in TRIBE_ROUTES:
                        if route["tribe"].lower() in line.lower():
                            await send(
                                route["webhook"],
                                route["thread_id"],
                                line
                            )

                STATE["last_hash"] = hash_line(new[0])
                STATE["last_activity"] = time.time()
                save_json(STATE_FILE, STATE)

            else:
                if time.time() - last_heartbeat > HEARTBEAT_SECONDS:
                    for route in TRIBE_ROUTES:
                        await send(
                            route["webhook"],
                            route["thread_id"],
                            "💓 Heartbeat — still polling (no new logs)"
                        )
                    last_heartbeat = time.time()

        except Exception as e:
            print("GetGameLog error:", e)

        await asyncio.sleep(POLL_SECONDS)

def run_tribelogs_loop():
    return asyncio.create_task(_tribelogs_loop())

# ======================
# SLASH COMMAND
# ======================
def setup_tribelog_commands(tree: app_commands.CommandTree, guild_id: int):

    @tree.command(
        name="linktribelog",
        description="Link a tribe's logs to a forum thread",
        guild=discord.Object(id=guild_id)
    )
    async def linktribelog(
        i: discord.Interaction,
        tribe: str,
        webhook: str,
        thread_id: str
    ):
        if ADMIN_ROLE_ID not in [r.id for r in i.user.roles]:
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        route = {
            "tribe": tribe,
            "webhook": webhook,
            "thread_id": thread_id
        }

        TRIBE_ROUTES.append(route)
        save_json(ROUTES_FILE, TRIBE_ROUTES)

        await i.response.send_message(
            f"✅ Linked tribe **{tribe}**",
            ephemeral=True
        )