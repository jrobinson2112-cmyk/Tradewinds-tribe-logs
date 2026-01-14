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
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # time webhook (edits one message)
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = os.getenv("RCON_PORT")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

# Nitrado time multipliers (set these in Railway vars)
# Example values: 1.0, 0.5, 2.0 etc
DAY_TIME_SPEED_SCALE = float(os.getenv("DAY_TIME_SPEED_SCALE", "1.0"))
NIGHT_TIME_SPEED_SCALE = float(os.getenv("NIGHT_TIME_SPEED_SCALE", "1.0"))

# =====================
# REQUIRED CHECK
# =====================
def require_env():
    missing = []
    for k in ["WEBHOOK_URL", "RCON_HOST", "RCON_PORT", "RCON_PASSWORD"]:
        if not os.getenv(k):
            missing.append(k)
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

RCON_PORT = int(RCON_PORT) if RCON_PORT else 27020

# =====================
# CONSTANTS (ASA default cycle)
# =====================
# Default ASA cycle on Nitrado guide: full day/night = 60 minutes real-time
# Day: 12 in-game hours (05:30 -> 17:30) = 30 minutes real-time by default
# Night: 12 in-game hours = 30 minutes real-time by default
BASE_FULL_CYCLE_SECONDS = 60 * 60
BASE_DAY_SECONDS = BASE_FULL_CYCLE_SECONDS / 2
BASE_NIGHT_SECONDS = BASE_FULL_CYCLE_SECONDS / 2

# Day (12 hours) = 720 in-game minutes
BASE_SPM_DAY = BASE_DAY_SECONDS / 720.0     # 1800/720 = 2.5 sec per in-game minute at scale 1.0
BASE_SPM_NIGHT = BASE_NIGHT_SECONDS / 720.0 # 2.5

# Sunrise / Sunset in minutes-of-day (05:30, 17:30)
SUNRISE = 5 * 60 + 30
SUNSET = 17 * 60 + 30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

# Update only on round step boundaries (10 mins by default)
TIME_UPDATE_STEP_MINUTES = 10

# Sync settings
GAMELOG_SYNC_SECONDS = 120         # check GetGameLog every 2 minutes
SYNC_DRIFT_MINUTES = 2             # only correct if drift >= 2 in-game minutes
SYNC_COOLDOWN_SECONDS = 600        # no more than once per 10 mins

# Daily announce channel (your existing)
ANNOUNCE_CHANNEL_ID = 1430388267446042666

# State file (put this on your Railway volume path)
STATE_FILE = os.getenv("TIME_STATE_FILE", "/data/time_state.json")

# =====================
# INTERNAL STATE
# =====================
_message_id: Optional[str] = None
_last_announced_abs_day: Optional[int] = None
_last_sync_ts: float = 0.0

# =====================
# HELPERS
# =====================
def is_day(minute_of_day: int) -> bool:
    return SUNRISE <= minute_of_day < SUNSET

def spm(minute_of_day: int) -> float:
    # Higher speed scale = time passes faster => fewer real seconds per in-game minute
    if is_day(minute_of_day):
        scale = max(0.01, DAY_TIME_SPEED_SCALE)
        return BASE_SPM_DAY / scale
    else:
        scale = max(0.01, NIGHT_TIME_SPEED_SCALE)
        return BASE_SPM_NIGHT / scale

def _advance_one_minute(minute_of_day: int, day: int, year: int):
    minute_of_day += 1
    if minute_of_day >= 1440:
        minute_of_day = 0
        day += 1
        if day > 365:
            day = 1
            year += 1
    return minute_of_day, day, year

def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def save_state(s: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f)

_state = load_state()

def calculate_time_details():
    """
    Returns:
      minute_of_day, day, year, seconds_into_current_minute, current_spm
    based on state epoch anchor.
    """
    if not _state:
        return None

    elapsed = float(time.time() - _state["epoch"])
    minute_of_day = int(_state["hour"]) * 60 + int(_state["minute"])
    day = int(_state["day"])
    year = int(_state["year"])

    remaining = elapsed
    while True:
        cur_spm = spm(minute_of_day)
        if remaining >= cur_spm:
            remaining -= cur_spm
            minute_of_day, day, year = _advance_one_minute(minute_of_day, day, year)
            continue
        return minute_of_day, day, year, remaining, cur_spm

def build_time_embed(minute_of_day: int, day: int, year: int):
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    emoji = "☀️" if is_day(minute_of_day) else "🌙"
    color = DAY_COLOR if is_day(minute_of_day) else NIGHT_COLOR
    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    return {"title": title, "color": color}

def seconds_until_next_round_step(minute_of_day: int, seconds_into_minute: float, step: int) -> float:
    mod = minute_of_day % step
    minutes_to_boundary = (step - mod) if mod != 0 else step

    cur_spm = spm(minute_of_day)
    remaining_in_current_minute = max(0.0, cur_spm - seconds_into_minute)
    total = remaining_in_current_minute

    m2 = minute_of_day
    d2, y2 = 1, 1  # unused here
    for _ in range(minutes_to_boundary - 1):
        m2, d2, y2 = _advance_one_minute(m2, d2, y2)
        total += spm(m2)

    return max(0.5, total)

# =====================
# DISCORD WEBHOOK UPSERT (edit same message)
# =====================
def _with_wait_true(url: str) -> str:
    if "wait=true" in url:
        return url
    if "?" in url:
        return url + "&wait=true"
    return url + "?wait=true"

async def upsert_time_webhook(session: aiohttp.ClientSession, embed: dict):
    global _message_id

    url = _with_wait_true(WEBHOOK_URL)

    # Edit existing message if we have one
    if _message_id:
        edit_url = WEBHOOK_URL.split("?")[0] + f"/messages/{_message_id}"
        async with session.patch(edit_url, json={"embeds": [embed]}) as r:
            if r.status == 404:
                _message_id = None
            else:
                return

    # Otherwise create
    async with session.post(url, json={"embeds": [embed]}) as r:
        data = await r.json(content_type=None)
        if r.status >= 400:
            raise RuntimeError(f"Webhook post failed: {r.status} {data}")
        if "id" in data:
            _message_id = data["id"]

# =====================
# RCON (minimal)
# =====================
def _rcon_make_packet(req_id: int, ptype: int, body: str) -> bytes:
    data = body.encode("utf-8", errors="ignore") + b"\x00"
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
        _ = await asyncio.wait_for(reader.read(4096), timeout=timeout)

        # exec
        writer.write(_rcon_make_packet(2, 2, command))
        await writer.drain()

        chunks = []
        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                part = await asyncio.wait_for(reader.read(4096), timeout=0.4)
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
# GAMELOG PARSE + SYNC
# =====================
# Example line contains:
# "... Tribe X ...: Day 216, 18:13:36: ..."
_DAYTIME_RE = re.compile(r"Day\s+(\d+),\s+(\d{1,2}):(\d{2}):(\d{2})\s*:")

def parse_latest_daytime(text: str) -> Optional[Tuple[int, int, int, int]]:
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = _DAYTIME_RE.search(ln)
        if not m:
            continue
        return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return None

def _wrap_minute_diff(diff: int) -> int:
    while diff > 720:
        diff -= 1440
    while diff < -720:
        diff += 1440
    return diff

def apply_gamelog_sync(parsed_day: int, parsed_hour: int, parsed_minute: int, parsed_second: int) -> Tuple[bool, str]:
    global _state

    if not _state:
        return False, "No state set"

    details = calculate_time_details()
    if not details:
        return False, "No calculated time details"

    cur_mod, cur_day, cur_year, sec_into_min, cur_spm = details
    target_mod = parsed_hour * 60 + parsed_minute

    # day diff (wrap within year)
    day_diff = parsed_day - cur_day
    if day_diff > 180:
        day_diff -= 365
    elif day_diff < -180:
        day_diff += 365

    minute_diff = day_diff * 1440 + (target_mod - cur_mod)
    minute_diff = _wrap_minute_diff(minute_diff)

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {minute_diff} min < threshold"

    # shift epoch by approximating in-game minute length at current time
    shift_seconds = minute_diff * spm(cur_mod)

    # Also align the seconds-in-minute loosely using parsed_second
    # We assume parsed_second is within the current in-game minute.
    target_seconds_into_minute = min(max(parsed_second, 0), 59) / 60.0 * spm(cur_mod)
    seconds_adjust = (sec_into_min - target_seconds_into_minute)

    _state["epoch"] = float(_state["epoch"]) - shift_seconds - seconds_adjust
    _state["day"] = int(parsed_day)
    _state["hour"] = int(parsed_hour)
    _state["minute"] = int(parsed_minute)
    save_state(_state)

    return True, f"Synced using GetGameLog (drift {minute_diff} min)"

# =====================
# LOOPS
# =====================
async def _time_loop(client: discord.Client):
    global _last_announced_abs_day
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            details = calculate_time_details()
            if not details:
                await asyncio.sleep(5)
                continue

            minute_of_day, day, year, sec_into_min, _ = details

            # post only on 10-min boundaries
            if (minute_of_day % TIME_UPDATE_STEP_MINUTES) == 0:
                embed = build_time_embed(minute_of_day, day, year)
                try:
                    await upsert_time_webhook(session, embed)
                except Exception as e:
                    print("Time webhook error:", e)

                # daily announce message
                abs_day = year * 365 + day
                if _last_announced_abs_day is None:
                    _last_announced_abs_day = abs_day
                elif abs_day > _last_announced_abs_day:
                    ch = client.get_channel(ANNOUNCE_CHANNEL_ID)
                    if ch:
                        try:
                            await ch.send(f"📅 **New Solunaris Day** — Day **{day}**, Year **{year}**")
                        except Exception:
                            pass
                    _last_announced_abs_day = abs_day

            sleep_for = seconds_until_next_round_step(minute_of_day, sec_into_min, TIME_UPDATE_STEP_MINUTES)
            await asyncio.sleep(sleep_for)

async def _gamelog_sync_loop(client: discord.Client):
    global _last_sync_ts
    await client.wait_until_ready()

    while True:
        try:
            if not _state:
                await asyncio.sleep(GAMELOG_SYNC_SECONDS)
                continue

            now = time.time()
            if (now - _last_sync_ts) < SYNC_COOLDOWN_SECONDS:
                await asyncio.sleep(GAMELOG_SYNC_SECONDS)
                continue

            log_text = await rcon_command("GetGameLog", timeout=10.0)
            parsed = parse_latest_daytime(log_text)

            if not parsed:
                print("GameLog sync: no parsable Day/time line found")
                await asyncio.sleep(GAMELOG_SYNC_SECONDS)
                continue

            d, h, m, s = parsed
            changed, msg = apply_gamelog_sync(d, h, m, s)
            print("GameLog sync:", msg)

            if changed:
                _last_sync_ts = time.time()

        except Exception as e:
            print("GameLog sync error:", e)

        await asyncio.sleep(GAMELOG_SYNC_SECONDS)

# =====================
# SLASH COMMANDS
# =====================
def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int):
    guild_obj = discord.Object(id=guild_id)

    @tree.command(name="settime", guild=guild_obj)
    async def settime(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
        # role check
        if not any(getattr(r, "id", None) == admin_role_id for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
            await i.response.send_message("❌ Invalid values.", ephemeral=True)
            return

        global _state
        _state = {
            "epoch": time.time(),
            "year": int(year),
            "day": int(day),
            "hour": int(hour),
            "minute": int(minute),
        }
        save_state(_state)
        await i.response.send_message("✅ Time set", ephemeral=True)

    @tree.command(name="sync", guild=guild_obj)
    async def sync_now(i: discord.Interaction):
        # admin only
        if not any(getattr(r, "id", None) == admin_role_id for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        await i.response.defer(ephemeral=True)
        try:
            log_text = await rcon_command("GetGameLog", timeout=10.0)
            parsed = parse_latest_daytime(log_text)
            if not parsed:
                await i.followup.send("❌ No Day/Time found in GetGameLog.", ephemeral=True)
                return

            d, h, m, s = parsed
            changed, msg = apply_gamelog_sync(d, h, m, s)
            await i.followup.send(f"✅ {msg}" if changed else f"ℹ️ {msg}", ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Sync error: {e}", ephemeral=True)

# =====================
# START TASKS
# =====================
_started = False

def start_time_tasks(client: discord.Client):
    global _started
    if _started:
        return
    require_env()
    _started = True
    asyncio.create_task(_time_loop(client))
    asyncio.create_task(_gamelog_sync_loop(client))