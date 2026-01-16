import os
import time
import json
import asyncio
import re
import aiohttp
import discord
from discord import app_commands

# =====================
# ENV / CONFIG
# =====================
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # required for time webhook

# Your Nitrado/ASA day-night boundaries (default ASA)
SUNRISE_MINUTE = int(os.getenv("SUNRISE_MINUTE", str(5 * 60 + 30)))  # 05:30
SUNSET_MINUTE  = int(os.getenv("SUNSET_MINUTE",  str(17 * 60 + 30))) # 17:30

# These are REAL SECONDS PER IN-GAME MINUTE.
# Set these to match your Nitrado multipliers / tuning.
DAY_SPM   = float(os.getenv("DAY_SPM", "4.7666667"))
NIGHT_SPM = float(os.getenv("NIGHT_SPM", "4.045"))

# Behaviour
TIME_UPDATE_STEP_MINUTES = int(os.getenv("TIME_UPDATE_STEP_MINUTES", "10"))  # only update webhook on round 10
AUTO_SYNC_EVERY_SECONDS  = int(os.getenv("AUTO_SYNC_EVERY_SECONDS", "600"))  # autosync every 10 minutes
SYNC_DRIFT_MINUTES       = int(os.getenv("SYNC_DRIFT_MINUTES", "2"))         # only correct if >= 2 minutes drift

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

# used by /sync command
_rcon_command = None
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

    # "big single line" = title only
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

    m = minute_of_day
    d = 1
    y = 1
    # add full minutes between the next minute and the boundary minute
    for _ in range(minutes_to_boundary - 1):
        m, d, y = _advance_one_minute(m, d, y)
        total += _spm_for_minute(m)

    return max(0.5, total)

# =====================
# PARSE TIME FROM TRIBE LOG LINES (GetGameLog output)
# Works on lines like:
# "... Tribe Valkyrie, ID 123: Day 216, 17:42:24: ..."
# "... Day 294, 07:12:15: Atropo claimed ..."
# =====================
_DAYTIME_ANYWHERE = re.compile(r"Day\s+(\d+),\s*(\d{1,2}):(\d{2}):(\d{2})")

def parse_latest_daytime_from_any_log_lines(text: str):
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = _DAYTIME_ANYWHERE.search(ln)
        if not m:
            continue
        day = int(m.group(1))
        hour = int(m.group(2))
        minute = int(m.group(3))
        second = int(m.group(4))
        return day, hour, minute, second
    return None

def _minute_of_day(hour: int, minute: int) -> int:
    return hour * 60 + minute

def _wrap_day_diff(d: int) -> int:
    # keep within ~half year
    if d > 180:
        d -= 365
    elif d < -180:
        d += 365
    return d

def _shift_epoch_by_ingame_minutes(cur_minute_of_day: int, minute_diff: int) -> float:
    """
    Convert ingame minute difference -> real seconds using the per-minute SPM model.
    More accurate than minute_diff * current_spm.
    """
    if minute_diff == 0:
        return 0.0

    seconds = 0.0
    m = cur_minute_of_day

    if minute_diff > 0:
        # our clock is behind -> move epoch back so clock moves forward
        for _ in range(minute_diff):
            seconds += _spm_for_minute(m)
            m, _, _ = _advance_one_minute(m, 1, 1)
        return seconds
    else:
        # our clock is ahead -> move epoch forward so clock moves backward
        for _ in range(abs(minute_diff)):
            # step backwards one ingame minute (approx: use spm of previous minute)
            prev = (m - 1) % 1440
            seconds += _spm_for_minute(prev)
            m = prev
        return -seconds
        # =====================
# SYNC APPLY
# =====================
def apply_sync_to_state(parsed_day: int, parsed_hour: int, parsed_minute: int) -> tuple[bool, str]:
    global _state

    if not _state:
        return False, "No state set yet (use /settime first)."

    now_calc = _calc_now()
    if not now_calc:
        return False, "Could not calculate current time (state missing)."

    cur_minute_of_day, cur_day, cur_year, _sec_into = now_calc
    target_minute_of_day = _minute_of_day(parsed_hour, parsed_minute)

    day_diff = _wrap_day_diff(parsed_day - cur_day)
    minute_diff = (day_diff * 1440) + (target_minute_of_day - cur_minute_of_day)

    # clamp huge drift to avoid wild corrections if logs are stale
    if minute_diff > 720:
        minute_diff -= 1440
    elif minute_diff < -720:
        minute_diff += 1440

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {minute_diff} min < threshold ({SYNC_DRIFT_MINUTES})."

    shift_seconds = _shift_epoch_by_ingame_minutes(cur_minute_of_day, minute_diff)

    # epoch shift: subtracting positive seconds moves the displayed time forward
    _state["epoch"] = float(_state["epoch"]) - float(shift_seconds)

    # also set the anchor display components to the parsed values (keeps it intuitive)
    _state["day"] = int(parsed_day)
    _state["hour"] = int(parsed_hour)
    _state["minute"] = int(parsed_minute)

    save_state()
    return True, f"Synced using tribe-log timestamp (drift {minute_diff} min)."

# =====================
# COMMANDS
# =====================
def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int, rcon_command, webhook_upsert):
    """
    Registers:
      /settime Year Day Hour Minute
      /sync  (force one sync)
    """
    global _rcon_command, _webhook_upsert
    _rcon_command = rcon_command
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
        await i.response.defer(ephemeral=True)

        if _rcon_command is None:
            await i.followup.send("❌ RCON not available to time module.", ephemeral=True)
            return

        if not _state:
            await i.followup.send("❌ Time not set yet. Use /settime first.", ephemeral=True)
            return

        try:
            text = await _rcon_command("GetGameLog", timeout=10.0)
            parsed = parse_latest_daytime_from_any_log_lines(text)
            if not parsed:
                await i.followup.send("❌ No Day/Time found in tribe-log lines in GetGameLog.", ephemeral=True)
                return

            d, h, m, s = parsed
            changed, msg = apply_sync_to_state(d, h, m)

            # push webhook right away after manual sync
            if _webhook_upsert is not None:
                now_calc = _calc_now()
                if now_calc:
                    mo, dd, yy, _ = now_calc
                    await _webhook_upsert("time", _build_time_embed(mo, dd, yy))

            await i.followup.send(("✅ " if changed else "ℹ️ ") + msg, ephemeral=True)

        except Exception as e:
            await i.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

    print("[time_module] ✅ /settime and /sync registered")

# =====================
# LOOP
# =====================
async def run_time_loop(client: discord.Client, rcon_command, webhook_upsert):
    """
    - Updates the time webhook only on round 10 minutes
    - Announces new day in ANNOUNCE_CHANNEL_ID
    - Auto-syncs every AUTO_SYNC_EVERY_SECONDS using tribe-log timestamps in GetGameLog
    """
    global _state, _last_announced_abs_day

    _state = load_state()

    last_sync_ts = 0.0

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now_calc = _calc_now()
                if not now_calc:
                    await asyncio.sleep(5)
                    continue

                minute_of_day, day, year, seconds_into_minute = now_calc

                # --- Auto sync every 10 minutes (best-effort) ---
                if rcon_command is not None:
                    if (time.time() - last_sync_ts) >= AUTO_SYNC_EVERY_SECONDS:
                        try:
                            text = await rcon_command("GetGameLog", timeout=10.0)
                            parsed = parse_latest_daytime_from_any_log_lines(text)
                            if parsed:
                                d, h, m, s = parsed
                                changed, msg = apply_sync_to_state(d, h, m)
                                if changed:
                                    print(f"[time_module] Auto-sync: {msg}")
                                last_sync_ts = time.time()
                            else:
                                print("[time_module] Auto-sync: No parsable Day/Time found in tribe-log lines in GetGameLog.")
                                last_sync_ts = time.time()
                        except Exception as e:
                            print(f"[time_module] Auto-sync error: {e}")
                            last_sync_ts = time.time()

                        # recalc after potential sync
                        now_calc = _calc_now()
                        if now_calc:
                            minute_of_day, day, year, seconds_into_minute = now_calc

                # --- Only update webhook on round step ---
                if (minute_of_day % TIME_UPDATE_STEP_MINUTES) == 0:
                    embed = _build_time_embed(minute_of_day, day, year)
                    await webhook_upsert(session, WEBHOOK_URL, "time", embed)

                    # --- New day announce ---
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