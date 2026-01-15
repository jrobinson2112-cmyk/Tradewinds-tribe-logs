import os
import json
import time
import asyncio
import aiohttp
import discord
from discord import app_commands
from typing import Optional, Tuple

# =========================
# STORAGE (Railway volume)
# =========================
DATA_DIR = os.getenv("DATA_DIR", "/data")
STATE_FILE = os.path.join(DATA_DIR, "time_state.json")
MESSAGE_ID_FILE = os.path.join(DATA_DIR, "time_message_id.json")

# ✅ shared file written by tribelogs_module
LAST_INGAME_TIME_FILE = os.path.join(DATA_DIR, "last_ingame_time.json")

def _ensure_data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass

# =========================
# CONFIG
# =========================
GUILD_ID = int(os.getenv("GUILD_ID", "1430388266393276509"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "1439069787207766076"))

# where daily "new day" text message goes
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))

# webhook that hosts the main time embed
TIME_WEBHOOK_URL = os.getenv("TIME_WEBHOOK_URL") or os.getenv("WEBHOOK_URL")
if not TIME_WEBHOOK_URL:
    raise RuntimeError("Missing required env var: TIME_WEBHOOK_URL (or WEBHOOK_URL)")

# ARK default: 05:30 -> 17:30
SUNRISE = 5 * 60 + 30
SUNSET = 17 * 60 + 30

# your SPM multipliers (as already working in your setup)
DAY_SPM = float(os.getenv("DAY_SPM", "4.7666667"))
NIGHT_SPM = float(os.getenv("NIGHT_SPM", "4.045"))

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

# embed update cadence
TIME_UPDATE_STEP_MINUTES = int(os.getenv("TIME_UPDATE_STEP_MINUTES", "10"))

# ✅ auto-sync every 10 mins
AUTO_SYNC_SECONDS = int(os.getenv("AUTO_SYNC_SECONDS", "600"))
SYNC_DRIFT_MINUTES = int(os.getenv("SYNC_DRIFT_MINUTES", "2"))

# =========================
# STATE
# =========================
def load_state():
    _ensure_data_dir()
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def save_state(s):
    _ensure_data_dir()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def load_message_ids():
    _ensure_data_dir()
    if not os.path.exists(MESSAGE_ID_FILE):
        return {"time": None}
    try:
        with open(MESSAGE_ID_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"time": None}

def save_message_ids(d):
    _ensure_data_dir()
    tmp = MESSAGE_ID_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MESSAGE_ID_FILE)

state = load_state()
message_ids = load_message_ids()
_last_announced_abs_day = None

# =========================
# TIME MATH
# =========================
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
    global state
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
        return minute_of_day, day, year, remaining, cur_spm

def build_time_embed(minute_of_day: int, day: int, year: int):
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    emoji = "☀️" if is_day(minute_of_day) else "🌙"
    color = DAY_COLOR if is_day(minute_of_day) else NIGHT_COLOR
    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    return {"title": title, "color": color}
    def seconds_until_next_round_step(minute_of_day: int, day: int, year: int, seconds_into_minute: float, step: int):
    m = minute_of_day
    mod = m % step
    minutes_to_boundary = (step - mod) if mod != 0 else step

    cur_spm = spm(m)
    remaining_in_current_minute = max(0.0, cur_spm - seconds_into_minute)
    total = remaining_in_current_minute

    m2 = m
    d2, y2 = day, year
    for _ in range(minutes_to_boundary - 1):
        m2, d2, y2 = _advance_one_minute(m2, d2, y2)
        total += spm(m2)

    return max(0.5, total)

async def upsert_webhook(session: aiohttp.ClientSession, url: str, key: str, embed: dict):
    mid = message_ids.get(key)
    base_url = url.split("?", 1)[0]

    if mid:
        async with session.patch(f"{base_url}/messages/{mid}", json={"embeds": [embed]}) as r:
            if r.status == 404:
                message_ids[key] = None
                save_message_ids(message_ids)
                return await upsert_webhook(session, url, key, embed)
            if r.status not in (200, 204):
                try:
                    data = await r.json()
                except Exception:
                    data = await r.text()
                raise RuntimeError(f"Webhook patch failed: {r.status} {data}")
        return

    async with session.post(base_url + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json()
        if "id" not in data:
            raise RuntimeError(f"Webhook post missing id: {data}")
        message_ids[key] = data["id"]
        save_message_ids(message_ids)

# =========================
# ✅ Sync from tribe-log timestamp file
# =========================
def read_last_ingame_time(max_age_seconds: int = 6 * 3600) -> Optional[Tuple[int, int, int, int]]:
    try:
        with open(LAST_INGAME_TIME_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        written_at = int(d.get("written_at_epoch", 0))
        if written_at and (time.time() - written_at) > max_age_seconds:
            return None
        return int(d["day"]), int(d["hour"]), int(d["minute"]), int(d["second"])
    except Exception:
        return None

def _wrap_minute_diff(diff: int) -> int:
    while diff > 720:
        diff -= 1440
    while diff < -720:
        diff += 1440
    return diff

def apply_sync(parsed_day: int, parsed_hour: int, parsed_minute: int):
    global state
    if not state:
        return False, "No state set (use /settime once)."

    details = calculate_time_details()
    if not details:
        return False, "No calculated time."

    cur_minute_of_day, cur_day, cur_year, seconds_into_minute, cur_spm = details
    target_minute_of_day = parsed_hour * 60 + parsed_minute

    day_diff = parsed_day - cur_day
    if day_diff > 180:
        day_diff -= 365
    elif day_diff < -180:
        day_diff += 365

    minute_diff = day_diff * 1440 + (target_minute_of_day - cur_minute_of_day)
    minute_diff = _wrap_minute_diff(minute_diff)

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {minute_diff} min < threshold"

    real_seconds_shift = minute_diff * spm(cur_minute_of_day)
    state["epoch"] = float(state["epoch"]) - real_seconds_shift

    # keep display fields aligned
    state["day"] = int(parsed_day)
    state["hour"] = int(parsed_hour)
    state["minute"] = int(parsed_minute)
    save_state(state)

    return True, f"Synced from tribe-log timestamp (drift {minute_diff} min)"

# =========================
# LOOPS
# =========================
async def _time_loop(client: discord.Client):
    global _last_announced_abs_day
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            details = calculate_time_details()
            if not details:
                await asyncio.sleep(2)
                continue

            minute_of_day, day, year, seconds_into_minute, cur_spm = details

            # update embed on step boundary
            if (minute_of_day % TIME_UPDATE_STEP_MINUTES) == 0:
                embed = build_time_embed(minute_of_day, day, year)
                await upsert_webhook(session, TIME_WEBHOOK_URL, "time", embed)

                # ✅ daily message at start of a NEW in-game day
                if ANNOUNCE_CHANNEL_ID:
                    abs_day = year * 365 + day
                    if _last_announced_abs_day is None:
                        _last_announced_abs_day = abs_day
                    elif abs_day > _last_announced_abs_day:
                        ch = client.get_channel(ANNOUNCE_CHANNEL_ID)
                        if ch:
                            await ch.send(f"📅 **New Solunaris Day** — Day **{day}**, Year **{year}**")
                        _last_announced_abs_day = abs_day

            sleep_for = seconds_until_next_round_step(
                minute_of_day, day, year, seconds_into_minute, TIME_UPDATE_STEP_MINUTES
            )
            await asyncio.sleep(sleep_for)

async def _autosync_loop(client: discord.Client):
    await client.wait_until_ready()
    while True:
        try:
            if state:
                parsed = read_last_ingame_time(max_age_seconds=6 * 3600)
                if parsed:
                    d, h, m, s = parsed
                    changed, msg = apply_sync(d, h, m)
                    if changed:
                        print("Auto-sync:", msg)
                else:
                    print("Auto-sync: no recent tribe-log timestamp available")
        except Exception as e:
            print("Auto-sync error:", e)

        await asyncio.sleep(AUTO_SYNC_SECONDS)

def run_time_loop(client: discord.Client):
    async def _runner():
        await asyncio.gather(_time_loop(client), _autosync_loop(client))
    return _runner()

# =========================
# COMMANDS
# =========================
def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int = ADMIN_ROLE_ID):
    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(name="settime", guild=guild_obj)
    async def settime(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
        if not any(r.id == int(admin_role_id) for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
            await i.response.send_message("❌ Invalid values.", ephemeral=True)
            return

        global state, _last_announced_abs_day
        state = {"epoch": time.time(), "year": int(year), "day": int(day), "hour": int(hour), "minute": int(minute)}
        save_state(state)

        # reset daily-message tracking so it can fire correctly after settime
        _last_announced_abs_day = year * 365 + day

        await i.response.send_message("✅ Time set.", ephemeral=True)

    @tree.command(name="sync", guild=guild_obj)
    async def sync(i: discord.Interaction):
        await i.response.defer(ephemeral=True)

        parsed = read_last_ingame_time(max_age_seconds=6 * 3600)
        if not parsed:
            await i.followup.send(
                "❌ No recent in-game Day/Time available yet (need at least one tribe log line).",
                ephemeral=True
            )
            return

        d, h, m, s = parsed
        changed, msg = apply_sync(d, h, m)
        await i.followup.send(("✅ " if changed else "ℹ️ ") + msg, ephemeral=True)