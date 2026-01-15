import os
import time
import json
import asyncio
import aiohttp
import discord
from discord import app_commands
import re

# ============================================================
# ENV
# ============================================================
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # time webhook (message will be edited)
STATE_FILE = os.getenv("TIME_STATE_FILE", "time_state.json")

# ============================================================
# CONSTANTS (KEEPING YOUR EXISTING VALUES)
# ============================================================
DAY_SPM = 4.7666667
NIGHT_SPM = 4.045
SUNRISE = 5 * 60 + 30
SUNSET = 17 * 60 + 30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

TIME_UPDATE_STEP_MINUTES = 10

# Auto-sync settings
AUTO_SYNC_SECONDS = 600  # 10 minutes
SYNC_DRIFT_MINUTES = 2   # only correct if drift >= 2 in-game minutes
SYNC_COOLDOWN_SECONDS = 600  # don't resync more often than every 10 minutes
_last_sync_ts = 0.0

# IMPORTANT: This regex accepts:
#   Day 237, 01:14:22 - ...
#   Day 237, 01:14:22: ...
#   Day 237, 01:14:22 ...
# It does NOT require a trailing colon.
_DAYTIME_RE = re.compile(r"\bDay\s+(\d+)\s*,\s*(\d{1,2}):(\d{2}):(\d{2})\b", re.IGNORECASE)

# ============================================================
# STATE
# ============================================================
state = None
_last_announced_abs_day = None  # optional if you use daily announcement elsewhere

def _load_state():
    global state, _last_announced_abs_day
    if not os.path.exists(STATE_FILE):
        state = None
        _last_announced_abs_day = None
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        state = data.get("time_state")
        _last_announced_abs_day = data.get("last_announced_abs_day")
    except Exception:
        state = None
        _last_announced_abs_day = None

def _save_state():
    data = {
        "time_state": state,
        "last_announced_abs_day": _last_announced_abs_day,
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

_load_state()

# ============================================================
# TIME MODEL
# ============================================================
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
    """
    Returns:
      minute_of_day, day, year, seconds_into_current_minute, current_minute_spm
    """
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
    """
    Real seconds until next in-game minute where minute_of_day % step == 0.
    """
    m = minute_of_day
    mod = m % step
    minutes_to_boundary = (step - mod) if mod != 0 else step

    cur_spm = spm(m)
    remaining_in_current_minute = max(0.0, cur_spm - seconds_into_minute)

    total = remaining_in_current_minute
    m2 = m
    # add full minutes until boundary
    for _ in range(minutes_to_boundary - 1):
        m2, _, _ = _advance_one_minute(m2, 1, 1)
        total += spm(m2)
    return max(0.5, total)

# ============================================================
# DISCORD WEBHOOK UPSERT (edit message if exists)
# ============================================================
_message_id = None

async def upsert_time_webhook(session: aiohttp.ClientSession, embed: dict):
    global _message_id
    if not WEBHOOK_URL:
        return

    # Patch existing
    if _message_id:
        async with session.patch(f"{WEBHOOK_URL}/messages/{_message_id}", json={"embeds": [embed]}) as r:
            if r.status == 404:
                _message_id = None
            else:
                return

    # Create new
    async with session.post(WEBHOOK_URL + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json()
        # Discord returns id when wait=true
        if isinstance(data, dict) and "id" in data:
            _message_id = data["id"]

# ============================================================
# GAMELOG PARSE + SYNC
# ============================================================
def parse_latest_daytime_any(log_text: str):
    """
    Scan from bottom, return (day, hour, minute, second) for the most recent match.
    """
    if not log_text:
        return None
    lines = [ln.strip() for ln in log_text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = _DAYTIME_RE.search(ln)
        if not m:
            continue
        d = int(m.group(1))
        hh = int(m.group(2))
        mm = int(m.group(3))
        ss = int(m.group(4))
        return d, hh, mm, ss
    return None

def _wrap_minute_diff(diff: int) -> int:
    while diff > 720:
        diff -= 1440
    while diff < -720:
        diff += 1440
    return diff

def apply_sync(parsed_day: int, parsed_hour: int, parsed_minute: int):
    """
    Shift epoch so our predicted time aligns with parsed (day, hour, minute) NOW.
    """
    global state
    if not state:
        return False, "No state set"

    details = calculate_time_details()
    if not details:
        return False, "No calculated time"

    cur_minute_of_day, cur_day, cur_year, seconds_into_minute, cur_spm = details
    target_minute_of_day = parsed_hour * 60 + parsed_minute

    # Day diff in same year range
    day_diff = parsed_day - cur_day
    if day_diff > 180:
        day_diff -= 365
    elif day_diff < -180:
        day_diff += 365

    minute_diff = day_diff * 1440 + (target_minute_of_day - cur_minute_of_day)
    minute_diff = _wrap_minute_diff(minute_diff)

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {minute_diff} min < threshold"

    # Shift epoch by approx current minute spm (good enough; next sync will refine)
    real_seconds_shift = minute_diff * spm(cur_minute_of_day)
    state["epoch"] = float(state["epoch"]) - real_seconds_shift

    # Keep state anchored to parsed values
    state["day"] = int(parsed_day)
    state["hour"] = int(parsed_hour)
    state["minute"] = int(parsed_minute)

    _save_state()
    return True, f"Synced (drift {minute_diff} min)"

async def do_sync_now(rcon_command):
    """
    Returns (ok:bool, msg:str)
    """
    try:
        log_text = await rcon_command("GetGameLog", timeout=10.0)
    except Exception as e:
        return False, f"GetGameLog error: {e}"

    if not log_text.strip():
        return False, "GetGameLog returned empty output."

    parsed = parse_latest_daytime_any(log_text)
    if not parsed:
        return False, "No parsable Day/Time found in GetGameLog."

    d, h, m, s = parsed
    changed, msg = apply_sync(d, h, m)
    return True, msg if changed else msg
    # ============================================================
# LOOP
# ============================================================
async def run_time_loop(client: discord.Client, rcon_command):
    """
    Updates time webhook on 10-minute boundaries and auto-syncs every 10 minutes.
    """
    global _last_sync_ts
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            details = calculate_time_details()
            if not details:
                await asyncio.sleep(5)
                continue

            minute_of_day, day, year, seconds_into_minute, _ = details

            # update the webhook only at round step
            if (minute_of_day % TIME_UPDATE_STEP_MINUTES) == 0:
                embed = build_time_embed(minute_of_day, day, year)
                await upsert_time_webhook(session, embed)

            # auto sync every 10 minutes (cooldown)
            now = time.time()
            if state and (now - _last_sync_ts) >= SYNC_COOLDOWN_SECONDS:
                ok, msg = await do_sync_now(rcon_command)
                _last_sync_ts = time.time()
                print(f"[time_module] Auto-sync: {msg}")

            sleep_for = seconds_until_next_round_step(
                minute_of_day,
                seconds_into_minute,
                TIME_UPDATE_STEP_MINUTES
            )
            await asyncio.sleep(sleep_for)

# ============================================================
# SLASH COMMANDS
# ============================================================
def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int):
    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(name="settime", guild=guild_obj)
    async def settime(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
        if not hasattr(i.user, "roles") or not any(r.id == admin_role_id for r in i.user.roles):
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
        _save_state()
        await i.response.send_message("✅ Time set.", ephemeral=True)

    @tree.command(name="sync", guild=guild_obj)
    async def sync_cmd(i: discord.Interaction):
        await i.response.defer(ephemeral=True)

        # rcon_command is injected via bind_rcon_for_commands()
        rcon = getattr(setup_time_commands, "_rcon_command_ref", None)
        if rcon is None:
            await i.followup.send("❌ RCON not wired to time module.", ephemeral=True)
            return

        ok, msg = await do_sync_now(rcon)
        await i.followup.send(("✅ " if ok else "❌ ") + msg, ephemeral=True)

    print("[time_module] ✅ /settime and /sync registered")

def bind_rcon_for_commands(rcon_command):
    """
    Call this from main.py once you have rcon_command available.
    """
    setup_time_commands._rcon_command_ref = rcon_command