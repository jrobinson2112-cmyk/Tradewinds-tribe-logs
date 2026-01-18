# time_module.py
import os
import time
import json
import asyncio
import aiohttp
import discord
from discord import app_commands

# =====================
# ENV / CONFIG
# =====================
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # required for time webhook

# ASA default boundaries
SUNRISE_MINUTE = int(os.getenv("SUNRISE_MINUTE", str(5 * 60 + 30)))   # 05:30
SUNSET_MINUTE  = int(os.getenv("SUNSET_MINUTE",  str(17 * 60 + 30)))  # 17:30

# ✅ REAL SECONDS PER IN-GAME MINUTE (your measured values)
# Day: 10:15 -> 17:15 is 420 ingame minutes in 1998 real seconds => 4.76 s/min
# Night: 17:30 -> 01:30 is 480 ingame minutes in 2046 real seconds => 4.2625 s/min
DAY_SPM   = float(os.getenv("DAY_SPM", "4.76"))
NIGHT_SPM = float(os.getenv("NIGHT_SPM", "4.2625"))

# Behaviour
TIME_UPDATE_STEP_MINUTES = int(os.getenv("TIME_UPDATE_STEP_MINUTES", "10"))  # update webhook on round 10 mins

# Daily announce
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "1430388267446042666"))

# State file (use your Railway volume path)
STATE_FILE = os.getenv("TIME_STATE_FILE", "/data/time_state.json")

# Webhook embed colours
DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

# =====================
# INTERNAL STATE
# =====================
_state = None
_last_announced_abs_day = None

# provided by main.py
_webhook_upsert = None

# =====================
# STATE FILE HELPERS
# =====================
def _ensure_state_dir():
    d = os.path.dirname(STATE_FILE)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def load_state():
    global _state
    _ensure_state_dir()
    if not os.path.exists(STATE_FILE):
        _state = None
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            _state = json.load(f)
            return _state
    except Exception:
        _state = None
        return None

def save_state():
    _ensure_state_dir()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(_state, f)

# =====================
# TIME MATH
# =====================
def _is_daytime(minute_of_day: int) -> bool:
    return SUNRISE_MINUTE <= minute_of_day < SUNSET_MINUTE

def _spm_for_minute(minute_of_day: int) -> float:
    return DAY_SPM if _is_daytime(minute_of_day) else NIGHT_SPM

def _advance_one_minute(minute_of_day: int, day: int, year: int):
    minute_of_day += 1
    if minute_of_day >= 1440:
        minute_of_day = 0
        day += 1
        if day > 365:
            day = 1
            year += 1
    return minute_of_day, day, year

def _calc_now():
    """
    Returns: (minute_of_day, day, year, seconds_into_current_ingame_minute)
    based on anchor state.
    """
    if not _state:
        return None

    elapsed = float(time.time() - float(_state["epoch"]))
    minute_of_day = int(_state["hour"]) * 60 + int(_state["minute"])
    day = int(_state["day"])
    year = int(_state["year"])

    remaining = elapsed
    while True:
        spm = _spm_for_minute(minute_of_day)
        if remaining >= spm:
            remaining -= spm
            minute_of_day, day, year = _advance_one_minute(minute_of_day, day, year)
            continue
        return minute_of_day, day, year, remaining

def _build_time_embed(minute_of_day: int, day: int, year: int):
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    icon = "☀️" if _is_daytime(minute_of_day) else "🌙"
    color = DAY_COLOR if _is_daytime(minute_of_day) else NIGHT_COLOR

    # big single-line title
    title = f"{icon} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    return {"title": title, "color": color}

def _seconds_until_next_round_step(minute_of_day: int, seconds_into_minute: float, step: int) -> float:
    """
    Sleep until the next ingame minute boundary that is divisible by 'step'
    (00/10/20/30/40/50 if step=10).
    """
    mod = minute_of_day % step
    minutes_to_boundary = (step - mod) if mod != 0 else step

    remaining_in_this_minute = max(0.0, _spm_for_minute(minute_of_day) - seconds_into_minute)
    total = remaining_in_this_minute

    # add full minutes between next minute and boundary minute
    m = minute_of_day
    for _ in range(minutes_to_boundary - 1):
        m, _, _ = _advance_one_minute(m, 1, 1)
        total += _spm_for_minute(m)

    return max(0.5, total)

# =====================
# COMMANDS
# =====================
def setup_time_commands(
    tree: app_commands.CommandTree,
    guild_id: int,
    admin_role_id: int,
    rcon_command,        # kept for compatibility with your main.py, NOT used
    webhook_upsert
):
    """
    Registers:
      /settime Year Day Hour Minute
      /sync  (forces an immediate webhook refresh using current calculated time)
    """
    global _webhook_upsert
    _webhook_upsert = webhook_upsert

    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(name="settime", guild=guild_obj)
    async def settime_cmd(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
        global _state
        if not any(getattr(r, "id", None) == int(admin_role_id) for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
            await i.response.send_message("❌ Invalid values.", ephemeral=True)
            return

        _state = {
            "epoch": time.time(),
            "year": int(year),
            "day": int(day),
            "hour": int(hour),
            "minute": int(minute),
        }
        save_state()
        await i.response.send_message("✅ Time set.", ephemeral=True)

    @tree.command(name="sync", guild=guild_obj)
    async def sync_cmd(i: discord.Interaction):
        """
        GetGameLog parsing removed (as requested).
        This simply forces an immediate webhook update from the bot's current clock state.
        """
        await i.response.defer(ephemeral=True)

        if not _state:
            await i.followup.send("❌ Time not set yet. Use /settime first.", ephemeral=True)
            return

        try:
            now_calc = _calc_now()
            if not now_calc:
                await i.followup.send("❌ Could not calculate time (state missing).", ephemeral=True)
                return

            minute_of_day, day, year, _sec = now_calc
            embed = _build_time_embed(minute_of_day, day, year)

            if _webhook_upsert is not None:
                await _webhook_upsert("time", embed)

            await i.followup.send("✅ Webhook refreshed from current clock state.", ephemeral=True)

        except Exception as e:
            await i.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

    print("[time_module] ✅ /settime and /sync registered (GetGameLog sync removed)")

# =====================
# LOOP
# =====================
async def run_time_loop(client: discord.Client, rcon_command, webhook_upsert):
    """
    - Updates the time webhook only on round 10 minutes
    - Announces new day in ANNOUNCE_CHANNEL_ID
    - ✅ GetGameLog parsing / autosync removed (as requested)
    """
    global _state, _last_announced_abs_day, _webhook_upsert

    _webhook_upsert = webhook_upsert
    _state = load_state()

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now_calc = _calc_now()
                if not now_calc:
                    await asyncio.sleep(5)
                    continue

                minute_of_day, day, year, seconds_into_minute = now_calc

                # Only update webhook on round step
                if (minute_of_day % TIME_UPDATE_STEP_MINUTES) == 0:
                    embed = _build_time_embed(minute_of_day, day, year)
                    await webhook_upsert(session, WEBHOOK_URL, "time", embed)

                    # New day announce
                    abs_day = (int(year) * 365) + int(day)
                    if _last_announced_abs_day is None:
                        _last_announced_abs_day = abs_day
                    elif abs_day > _last_announced_abs_day:
                        ch = client.get_channel(int(ANNOUNCE_CHANNEL_ID))
                        if ch:
                            try:
                                await ch.send(f"📅 **New Solunaris Day** — Day **{day}**, Year **{year}**")
                            except Exception:
                                pass
                        _last_announced_abs_day = abs_day

                sleep_for = _seconds_until_next_round_step(
                    minute_of_day, seconds_into_minute, TIME_UPDATE_STEP_MINUTES
                )
                await asyncio.sleep(sleep_for)

            except Exception as e:
                print(f"[time_module] loop error: {e}")
                await asyncio.sleep(5)