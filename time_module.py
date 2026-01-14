# time_module.py
# Solunaris Time Module (RCON autosync, stable)

import os
import time
import json
import asyncio
import aiohttp
import discord
from discord import app_commands
import re
from typing import Optional, Tuple

# =====================
# ENV
# =====================
TIME_WEBHOOK_URL = os.getenv("TIME_WEBHOOK_URL")
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "0"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0") or "0")

STATE_FILE = os.getenv("TIME_STATE_FILE", "/data/time_state.json")

# Day/Night
SUNRISE = 5 * 60 + 30
SUNSET = 17 * 60 + 30

# Colors
DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

# Update cadence
TIME_UPDATE_STEP_MINUTES = 10

# === SPM (USE THESE — matches your Nitrado settings) ===
DAY_SPM = float(os.getenv("DAY_SPM", "4.7666667"))
NIGHT_SPM = float(os.getenv("NIGHT_SPM", "4.045"))

# Gamelog sync
GAMELOG_SYNC_SECONDS = 30
SYNC_DRIFT_MINUTES = 1
SYNC_COOLDOWN_SECONDS = 60
SYNC_LINE_FILTER = None  # accept ANY line with Day X, HH:MM:SS

# =====================
# VALIDATION
# =====================
for k in ("TIME_WEBHOOK_URL", "RCON_HOST", "RCON_PORT", "RCON_PASSWORD"):
    if not os.getenv(k):
        raise RuntimeError(f"Missing required env var: {k}")

# =====================
# STATE
# =====================
message_ids = {"time": None}
last_announced_abs_day = None
_last_sync_ts = 0.0


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(s: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f)


state = load_state()

# =====================
# TIME MODEL
# =====================
def is_day(minute_of_day: int) -> bool:
    return SUNRISE <= minute_of_day < SUNSET


def spm(minute_of_day: int) -> float:
    return DAY_SPM if is_day(minute_of_day) else NIGHT_SPM


def advance_minute(m, d, y):
    m += 1
    if m >= 1440:
        m = 0
        d += 1
        if d > 365:
            d = 1
            y += 1
    return m, d, y


def calculate_time():
    if not state:
        return None

    elapsed = time.time() - state["epoch"]
    minute_of_day = state["hour"] * 60 + state["minute"]
    day = state["day"]
    year = state["year"]

    remaining = elapsed
    while True:
        cur_spm = spm(minute_of_day)
        if remaining >= cur_spm:
            remaining -= cur_spm
            minute_of_day, day, year = advance_minute(minute_of_day, day, year)
        else:
            return minute_of_day, day, year, remaining


def build_embed(minute_of_day, day, year):
    h = minute_of_day // 60
    m = minute_of_day % 60
    emoji = "☀️" if is_day(minute_of_day) else "🌙"
    return {
        "title": f"{emoji} | Solunaris Time | {h:02d}:{m:02d} | Day {day} | Year {year}",
        "color": DAY_COLOR if is_day(minute_of_day) else NIGHT_COLOR,
    }


def seconds_until_next_boundary(minute_of_day, seconds_into_minute):
    mod = minute_of_day % TIME_UPDATE_STEP_MINUTES
    minutes_to_next = TIME_UPDATE_STEP_MINUTES if mod == 0 else TIME_UPDATE_STEP_MINUTES - mod
    total = spm(minute_of_day) - seconds_into_minute
    m = minute_of_day
    d = y = 0
    for _ in range(minutes_to_next - 1):
        m, d, y = advance_minute(m, d, y)
        total += spm(m)
    return max(1.0, total)

# =====================
# WEBHOOK
# =====================
async def upsert_webhook(session, embed):
    mid = message_ids["time"]

    if mid:
        async with session.patch(
            f"{TIME_WEBHOOK_URL}/messages/{mid}", json={"embeds": [embed]}
        ):
            return

    async with session.post(TIME_WEBHOOK_URL + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json(content_type=None)
        if "id" in data:
            message_ids["time"] = data["id"]

# =====================
# RCON
# =====================
def make_packet(req_id, ptype, body):
    data = body.encode() + b"\x00"
    pkt = req_id.to_bytes(4, "little", signed=True) + ptype.to_bytes(4, "little", signed=True) + data + b"\x00"
    return len(pkt).to_bytes(4, "little", signed=True) + pkt


async def rcon(cmd):
    reader, writer = await asyncio.open_connection(RCON_HOST, RCON_PORT)
    writer.write(make_packet(1, 3, RCON_PASSWORD))
    await writer.drain()
    await reader.read(4096)

    writer.write(make_packet(2, 2, cmd))
    await writer.drain()

    data = await reader.read(65535)
    writer.close()
    await writer.wait_closed()

    out = b""
    i = 0
    while i + 4 <= len(data):
        size = int.from_bytes(data[i:i+4], "little", signed=True)
        i += 4
        pkt = data[i:i+size]
        i += size
        out += pkt[8:-2]

    return out.decode(errors="ignore")

# =====================
# GAMELOG SYNC
# =====================
DAY_RE = re.compile(r"Day\s+(\d+),\s+(\d{1,2}):(\d{2}):(\d{2})")

def parse_gamelog(text):
    for line in reversed(text.splitlines()):
        m = DAY_RE.search(line)
        if m:
            return int(m[1]), int(m[2]), int(m[3])
    return None


def sync_time(day, hour, minute):
    global state
    cur = calculate_time()
    if not cur:
        return False

    cur_mod, cur_day, cur_year, _ = cur
    target_mod = hour * 60 + minute
    diff = (day - cur_day) * 1440 + (target_mod - cur_mod)

    if abs(diff) < SYNC_DRIFT_MINUTES:
        return False

    state["epoch"] -= diff * spm(cur_mod)
    state["day"] = day
    state["hour"] = hour
    state["minute"] = minute
    save_state(state)
    return True

# =====================
# LOOPS
# =====================
async def run_time_loop(client):
    global last_announced_abs_day
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            cur = calculate_time()
            if not cur:
                await asyncio.sleep(5)
                continue

            mod, day, year, sec = cur

            if mod % TIME_UPDATE_STEP_MINUTES == 0:
                await upsert_webhook(session, build_embed(mod, day, year))

                abs_day = year * 365 + day
                if ANNOUNCE_CHANNEL_ID and last_announced_abs_day != abs_day:
                    ch = client.get_channel(ANNOUNCE_CHANNEL_ID)
                    if ch:
                        await ch.send(f"📅 **New Solunaris Day** — Day **{day}**, Year **{year}**")
                    last_announced_abs_day = abs_day

            await asyncio.sleep(seconds_until_next_boundary(mod, sec))


async def run_gamelog_sync_loop():
    global _last_sync_ts
    while True:
        try:
            if state and time.time() - _last_sync_ts > SYNC_COOLDOWN_SECONDS:
                log = await rcon("GetGameLog")
                parsed = parse_gamelog(log)
                if parsed and sync_time(*parsed):
                    _last_sync_ts = time.time()
        except Exception as e:
            print("Time sync error:", e)

        await asyncio.sleep(GAMELOG_SYNC_SECONDS)

# =====================
# COMMANDS
# =====================
def setup_time_commands(tree, guild_id, admin_role_id):
    guild = discord.Object(id=guild_id)

    @tree.command(name="settime", guild=guild)
    async def settime(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
        if not any(r.id == admin_role_id for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        global state
        state = {
            "epoch": time.time(),
            "year": year,
            "day": day,
            "hour": hour,
            "minute": minute,
        }
        save_state(state)
        await i.response.send_message("✅ Time set", ephemeral=True)

    @tree.command(name="sync", guild=guild)
    async def sync(i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        log = await rcon("GetGameLog")
        parsed = parse_gamelog(log)
        if not parsed:
            await i.followup.send("❌ No Day/Time found in GetGameLog", ephemeral=True)
            return

        await i.followup.send("✅ Synced" if sync_time(*parsed) else "ℹ️ Already in sync", ephemeral=True)