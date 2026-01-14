# time_module.py
import os
import time
import json
import asyncio
import re
import discord
from discord import app_commands

# =====================
# ENV
# =====================
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "0") or "0")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # time webhook (required)

if not (RCON_HOST and RCON_PORT and RCON_PASSWORD and WEBHOOK_URL):
    missing = []
    for k in ["RCON_HOST", "RCON_PORT", "RCON_PASSWORD", "WEBHOOK_URL"]:
        if not os.getenv(k):
            missing.append(k)
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

# =====================
# SERVER CONSTANTS
# =====================
DAY_SPM = 4.85      # from your Nitrado screenshot (day)
NIGHT_SPM = 3.88    # from your Nitrado screenshot (night)
SUNRISE = 5 * 60 + 30
SUNSET  = 17 * 60 + 30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

STATE_FILE = "state.json"

TIME_UPDATE_STEP_MINUTES = 10

# GameLog sync
GAMELOG_SYNC_SECONDS = 120
SYNC_DRIFT_MINUTES = 2
SYNC_COOLDOWN_SECONDS = 600

# =====================
# STATE
# =====================
message_id_time = None
last_announced_abs_day = None
_last_sync_ts = 0.0

def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(s):
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

def _advance_one_minute(minute_of_day: int, day: int, year: int):
    minute_of_day += 1
    if minute_of_day >= 1440:
        minute_of_day = 0
        day += 1
        if day > 365:
            day = 1
            year += 1
    return minute_of_day, day, year

def calculate_time_details():
    if not state:
        return None

    elapsed = float(time.time() - state["epoch"])
    minute_of_day = int(state["hour"]) * 60 + int(state["minute"])
    day = int(state["day"])
    year = int(state["year"])

    remaining = elapsed
    while True:
        cur_spm = spm(minute_of_day)
        if remaining >= cur_spm:
            remaining -= cur_spm
            minute_of_day, day, year = _advance_one_minute(minute_of_day, day, year)
            continue
        seconds_into_current_minute = remaining
        return minute_of_day, day, year, seconds_into_current_minute, cur_spm

def build_time_embed(minute_of_day: int, day: int, year: int):
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    emoji = "☀️" if is_day(minute_of_day) else "🌙"
    color = DAY_COLOR if is_day(minute_of_day) else NIGHT_COLOR
    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    return {"title": title, "color": color}

def seconds_until_next_round_step(minute_of_day: int, seconds_into_minute: float, step: int):
    mod = minute_of_day % step
    minutes_to_boundary = (step - mod) if mod != 0 else step
    remaining_in_current_minute = max(0.0, spm(minute_of_day) - seconds_into_minute)

    total = remaining_in_current_minute
    m = minute_of_day
    d = 1
    y = 1
    for _ in range(minutes_to_boundary - 1):
        m = (m + 1) % 1440
        total += spm(m)

    return max(0.5, total)

# =====================
# RCON (minimal)
# =====================
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
        writer.write(_rcon_make_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        await asyncio.wait_for(reader.read(4096), timeout=timeout)

        writer.write(_rcon_make_packet(2, 2, command))
        await writer.drain()

        chunks = []
        end = time.time() + timeout
        while time.time() < end:
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
            if size < 10 or i + size > len(data):
                break
            pkt = data[i:i+size]
            i += size
            body = pkt[8:-2]
            txt = body.decode("utf-8", errors="ignore")
            if txt:
                out.append(txt)
        return "".join(out).strip()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

# =====================
# WEBHOOK UPDATE
# =====================
async def upsert_time_webhook(session, embed: dict):
    global message_id_time
    if message_id_time:
        async with session.patch(f"{WEBHOOK_URL}/messages/{message_id_time}", json={"embeds": [embed]}) as r:
            if r.status == 404:
                message_id_time = None
                return await upsert_time_webhook(session, embed)
        return

    async with session.post(WEBHOOK_URL + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json()
        if "id" in data:
            message_id_time = data["id"]

# =====================
# GAMELOG -> TIME SYNC
# =====================
# Matches: "Day 216, 18:13:36:" inside tribe log lines
DAYTIME_RE = re.compile(r"Day\s+(\d+),\s+(\d{1,2}):(\d{2}):(\d{2})\s*:")

def parse_latest_daytime(text: str):
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = DAYTIME_RE.search(ln)
        if not m:
            continue
        return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return None

def clamp_minute_diff(diff: int) -> int:
    while diff > 720:
        diff -= 1440
    while diff < -720:
        diff += 1440
    return diff

def apply_sync(parsed_day: int, parsed_hour: int, parsed_minute: int):
    global state
    if not state:
        return False, "No time set (use /settime first)."

    details = calculate_time_details()
    if not details:
        return False, "No calculated time."

    cur_minute_of_day, cur_day, cur_year, sec_into_minute, cur_spm = details
    target_minute_of_day = parsed_hour * 60 + parsed_minute

    day_diff = parsed_day - cur_day
    if day_diff > 180:
        day_diff -= 365
    elif day_diff < -180:
        day_diff += 365

    minute_diff = day_diff * 1440 + (target_minute_of_day - cur_minute_of_day)
    minute_diff = clamp_minute_diff(minute_diff)

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {minute_diff} min < threshold ({SYNC_DRIFT_MINUTES})."

    # Shift epoch so "now" lines up to parsed time
    # Use current minute's SPM for the correction (good enough and stable)
    real_seconds_shift = minute_diff * spm(cur_minute_of_day)
    state["epoch"] = float(state["epoch"]) - real_seconds_shift

    # also store the parsed clock anchor
    state["day"] = int(parsed_day)
    state["hour"] = int(parsed_hour)
    state["minute"] = int(parsed_minute)
    save_state(state)

    return True, f"Synced using GetGameLog (drift {minute_diff} min)."

async def sync_now():
    log_text = await rcon_command("GetGameLog", timeout=10.0)
    parsed = parse_latest_daytime(log_text)
    if not parsed:
        return False, "No Day/Time found in GetGameLog."
    d, h, m, s = parsed
    return apply_sync(d, h, m)

# =====================
# LOOPS
# =====================
async def time_loop(announce_channel_id: int):
    global last_announced_abs_day
    import aiohttp

    async with aiohttp.ClientSession() as session:
        while True:
            details = calculate_time_details()
            if not details:
                await asyncio.sleep(5)
                continue

            minute_of_day, day, year, seconds_into_minute, cur_spm = details

            if (minute_of_day % TIME_UPDATE_STEP_MINUTES) == 0:
                embed = build_time_embed(minute_of_day, day, year)
                await upsert_time_webhook(session, embed)

                abs_day = year * 365 + day
                if last_announced_abs_day is None:
                    last_announced_abs_day = abs_day
                elif abs_day > last_announced_abs_day:
                    ch = discord.utils.get(session._connector._loop._selector.get_map().values(), id=announce_channel_id)  # not used
                    # We won't try to fetch channel here (loop doesn't have client reference).
                    last_announced_abs_day = abs_day

                sleep_for = seconds_until_next_round_step(minute_of_day, seconds_into_minute, TIME_UPDATE_STEP_MINUTES)
                await asyncio.sleep(sleep_for)
            else:
                sleep_for = seconds_until_next_round_step(minute_of_day, seconds_into_minute, TIME_UPDATE_STEP_MINUTES)
                await asyncio.sleep(sleep_for)

async def gamelog_sync_loop():
    global _last_sync_ts
    while True:
        try:
            if state:
                now = time.time()
                if (now - _last_sync_ts) >= SYNC_COOLDOWN_SECONDS:
                    changed, msg = await sync_now()
                    print("GameLog sync:", msg)
                    if changed:
                        _last_sync_ts = time.time()
        except Exception as e:
            print("GameLog sync error:", e)

        await asyncio.sleep(GAMELOG_SYNC_SECONDS)

# =====================
# COMMANDS
# =====================
def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int):
    guild = discord.Object(id=guild_id)

    @tree.command(name="settime", guild=guild)
    async def settime(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
        if not any(r.id == admin_role_id for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
            await i.response.send_message("❌ Invalid values.", ephemeral=True)
            return

        global state
        state = {
            "epoch": time.time(),
            "year": int(year),
            "day": int(day),
            "hour": int(hour),
            "minute": int(minute),
        }
        save_state(state)
        await i.response.send_message("✅ Time set", ephemeral=True)

    @tree.command(name="sync", guild=guild)
    async def sync_cmd(i: discord.Interaction):
        if not any(r.id == admin_role_id for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        await i.response.defer(ephemeral=True)
        try:
            changed, msg = await sync_now()
            await i.followup.send(("✅ " if changed else "ℹ️ ") + msg, ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

    @tree.command(name="debuggamelog", guild=guild)
    async def debuggamelog(i: discord.Interaction):
        if not any(r.id == admin_role_id for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        await i.response.defer(ephemeral=True)
        text = await rcon_command("GetGameLog", timeout=10.0)
        tail = "\n".join(text.splitlines()[-15:]) if text else "(empty)"
        await i.followup.send(f"```text\n{tail}\n```", ephemeral=True)