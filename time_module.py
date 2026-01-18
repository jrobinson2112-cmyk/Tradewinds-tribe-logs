# time_module.py
# Accurate-ish ASA clock with:
#  - /settime Year Day Hour Minute (admin role)
#  - /sync (admin role): syncs to MOST RECENT GetGameLog line that contains "Day X, HH:MM:SS"
#  - Auto-sync in the loop: whenever a NEW timed GetGameLog line appears
#
# Uses the REAL timestamp at the start of each GetGameLog line (YYYY.MM.DD_HH.MM.SS) to
# compensate for log delay and improve accuracy.
#
# IMPORTANT:
# - Ignores in-game seconds for display, but uses them internally when parsing.
# - No longer uses old tribe-log parsing. Only "Day X, HH:MM:SS" lines are used for sync.

import os
import time
import json
import asyncio
import re
import aiohttp
import discord
from discord import app_commands
from typing import Optional, Tuple, Dict, Any

# =====================
# ENV / CONFIG
# =====================
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # required for time webhook

# Day/night boundaries (ASA defaults)
SUNRISE_MINUTE = int(os.getenv("SUNRISE_MINUTE", str(5 * 60 + 30)))  # 05:30
SUNSET_MINUTE = int(os.getenv("SUNSET_MINUTE", str(17 * 60 + 30)))   # 17:30

# REAL SECONDS PER IN-GAME MINUTE
DAY_SPM = float(os.getenv("DAY_SPM", "4.7666667"))
NIGHT_SPM = float(os.getenv("NIGHT_SPM", "4.045"))

# Behaviour
TIME_UPDATE_STEP_MINUTES = int(os.getenv("TIME_UPDATE_STEP_MINUTES", "10"))  # update webhook on round step (10 = :00/:10/:20...)
AUTO_SYNC_EVERY_SECONDS = int(os.getenv("AUTO_SYNC_EVERY_SECONDS", "60"))    # check GetGameLog for timed line
SYNC_DRIFT_MINUTES = int(os.getenv("SYNC_DRIFT_MINUTES", "2"))               # only sync if >= drift minutes
SYNC_MAX_LOOKBACK_LINES = int(os.getenv("SYNC_MAX_LOOKBACK_LINES", "2500"))  # how many tail lines to scan

# Daily announce
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "1430388267446042666"))

# State file
STATE_FILE = os.getenv("TIME_STATE_FILE", "/data/time_state.json")

# Webhook embed colours
DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

# =====================
# INTERNAL STATE
# =====================
_state: Optional[Dict[str, Any]] = None
_last_announced_abs_day: Optional[int] = None

_rcon_command = None
_webhook_upsert = None

_last_synced_marker: Optional[str] = None  # prevents re-syncing the same timed line

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

def _advance_one_minute(minute_of_day: int, day: int, year: int) -> Tuple[int, int, int]:
    minute_of_day += 1
    if minute_of_day >= 1440:
        minute_of_day = 0
        day += 1
        if day > 365:
            day = 1
            year += 1
    return minute_of_day, day, year

def _calc_now() -> Optional[Tuple[int, int, int, float]]:
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

def _build_time_embed(minute_of_day: int, day: int, year: int) -> dict:
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    is_day = _is_daytime(minute_of_day)
    icon = "☀️" if is_day else "🌙"
    color = DAY_COLOR if is_day else NIGHT_COLOR
    title = f"{icon} | Solunaris Time — Year {year} | Day {day} | {hour:02d}:{minute:02d}"
    return {"title": title, "color": color}

def _seconds_until_next_round_step(minute_of_day: int, seconds_into_minute: float, step: int) -> float:
    """
    Sleep until the next ingame minute boundary that is divisible by 'step'.
    """
    mod = minute_of_day % step
    minutes_to_boundary = (step - mod) if mod != 0 else step

    remaining_in_this_minute = max(0.0, _spm_for_minute(minute_of_day) - seconds_into_minute)
    total = remaining_in_this_minute

    m = minute_of_day
    d = 1
    y = 1
    for _ in range(minutes_to_boundary - 1):
        m, d, y = _advance_one_minute(m, d, y)
        total += _spm_for_minute(m)

    return max(0.5, total)

def _minute_of_day(hour: int, minute: int) -> int:
    return hour * 60 + minute

def _wrap_day_diff(d: int) -> int:
    if d > 180:
        d -= 365
    elif d < -180:
        d += 365
    return d

def _infer_year_from_day(cur_year: int, cur_day: int, parsed_day: int) -> int:
    """
    Heuristic: if day jumps across year boundary, adjust year.
    """
    diff = parsed_day - cur_day
    if diff > 180:
        return max(1, cur_year - 1)
    if diff < -180:
        return cur_year + 1
    return cur_year

def _advance_by_real_seconds(
    start_minute_of_day: int,
    start_day: int,
    start_year: int,
    seconds: float
) -> Tuple[int, int, int, float]:
    """
    Advances in-game time forward by 'seconds' real seconds using the per-minute SPM model.
    Returns (minute_of_day, day, year, seconds_into_current_minute).
    """
    m = int(start_minute_of_day)
    d = int(start_day)
    y = int(start_year)

    remaining = float(max(0.0, seconds))
    while True:
        spm = _spm_for_minute(m)
        if remaining >= spm:
            remaining -= spm
            m, d, y = _advance_one_minute(m, d, y)
            continue
        # remaining = seconds into current minute
        return m, d, y, remaining

def _shift_epoch_by_ingame_minutes(cur_minute_of_day: int, minute_diff: int) -> float:
    """
    Convert ingame minute difference -> real seconds using the per-minute SPM model.
    """
    if minute_diff == 0:
        return 0.0

    seconds = 0.0
    m = cur_minute_of_day

    if minute_diff > 0:
        for _ in range(minute_diff):
            seconds += _spm_for_minute(m)
            m, _, _ = _advance_one_minute(m, 1, 1)
        return seconds
    else:
        for _ in range(abs(minute_diff)):
            prev = (m - 1) % 1440
            seconds += _spm_for_minute(prev)
            m = prev
        return -seconds

# =====================
# PARSING: TIMED GAMELOG LINES
# Example:
#   2026.01.18_17.03.38: Tribe The Silent Coven, ID ...: Day 327, 15:45:59: Dravenya froze...
# =====================
_REAL_TS_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})_(\d{2})\.(\d{2})\.(\d{2})")
_INGAME_TS_RE = re.compile(r"Day\s+(\d+),\s*(\d{1,2}):(\d{2}):(\d{2})")

def _parse_real_epoch_from_line(line: str) -> Optional[float]:
    m = _REAL_TS_RE.search(line or "")
    if not m:
        return None
    try:
        yy, mo, dd, hh, mm, ss = map(int, m.groups())
        # interpret as local time (same basis as time.time() local), matching your display usage
        return time.mktime((yy, mo, dd, hh, mm, ss, 0, 0, -1))
    except Exception:
        return None

def _parse_timed_gamelog_line(line: str) -> Optional[Dict[str, Any]]:
    """
    Returns dict with: day, hour, minute, second, real_epoch, marker, line
    """
    if not line:
        return None
    m = _INGAME_TS_RE.search(line)
    if not m:
        return None
    try:
        day = int(m.group(1))
        hour = int(m.group(2))
        minute = int(m.group(3))
        second = int(m.group(4))
    except Exception:
        return None

    real_epoch = _parse_real_epoch_from_line(line)
    marker = f"{real_epoch or 'noReal'}|{day}|{hour:02d}:{minute:02d}:{second:02d}|{hash(line)}"
    return {
        "day": day,
        "hour": hour,
        "minute": minute,
        "second": second,
        "real_epoch": real_epoch,
        "marker": marker,
        "line": line,
    }

def find_latest_timed_entry_from_getgamelog(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # scan from newest backwards (tail only for speed)
    tail = lines[-SYNC_MAX_LOOKBACK_LINES:] if len(lines) > SYNC_MAX_LOOKBACK_LINES else lines
    for ln in reversed(tail):
        parsed = _parse_timed_gamelog_line(ln)
        if parsed:
            return parsed
    return None

# =====================
# SYNC APPLY
# =====================
def apply_sync_from_timed_log(parsed: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Syncs state using the timed log entry.
    - Ignores in-game seconds for display (/state anchor uses HH:MM)
    - Uses real_epoch to advance in-game time forward by the log delay for better accuracy
    """
    global _state

    if not _state:
        return False, "No state set yet (use /settime first)."

    now_calc = _calc_now()
    if not now_calc:
        return False, "Could not calculate current time (state missing)."

    cur_minute_of_day, cur_day, cur_year, _sec_into = now_calc

    parsed_day = int(parsed["day"])
    parsed_hour = int(parsed["hour"])
    parsed_minute = int(parsed["minute"])

    inferred_year = _infer_year_from_day(cur_year, cur_day, parsed_day)

    # Start from the in-game time *at the moment of the log line* (seconds included internally)
    start_m = _minute_of_day(parsed_hour, parsed_minute)
    start_d = parsed_day
    start_y = inferred_year

    # If we have real timestamp on the log line, compensate for delay
    real_epoch = parsed.get("real_epoch")
    if real_epoch is not None:
        delay = time.time() - float(real_epoch)
        # clamp silly delays (server timezone mismatch etc.)
        if delay < 0:
            delay = 0.0
        if delay > 3600 * 6:
            # if delay is huge, don't "advance" for hours; just anchor to the line itself
            delay = 0.0
        adv_m, adv_d, adv_y, adv_sec_into = _advance_by_real_seconds(start_m, start_d, start_y, delay)
    else:
        adv_m, adv_d, adv_y, adv_sec_into = start_m, start_d, start_y, 0.0

    # Compute drift vs current model (minutes)
    target_minute_of_day = adv_m
    day_diff = _wrap_day_diff(adv_d - cur_day)
    minute_diff = (day_diff * 1440) + (target_minute_of_day - cur_minute_of_day)

    # clamp huge drift within +/- 12h
    if minute_diff > 720:
        minute_diff -= 1440
    elif minute_diff < -720:
        minute_diff += 1440

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {minute_diff} min < threshold ({SYNC_DRIFT_MINUTES})."

    # Re-anchor state precisely: set epoch so that _calc_now() == adv_m/adv_d/adv_y at current time
    now = time.time()
    _state["epoch"] = float(now) - float(adv_sec_into)
    _state["year"] = int(adv_y)
    _state["day"] = int(adv_d)
    _state["hour"] = int(target_minute_of_day // 60)
    _state["minute"] = int(target_minute_of_day % 60)

    save_state()
    return True, f"Synced to timed GetGameLog line (drift {minute_diff} min)."

# =====================
# COMMANDS
# =====================
def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int, rcon_command, webhook_upsert):
    """
    Registers:
      /settime Year Day Hour Minute
      /sync  (sync to most recent timed GetGameLog line)
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

        if not any(getattr(r, "id", None) == int(admin_role_id) for r in getattr(i.user, "roles", [])):
            await i.followup.send("❌ No permission", ephemeral=True)
            return

        if _rcon_command is None:
            await i.followup.send("❌ RCON not available to time module.", ephemeral=True)
            return

        if not _state:
            await i.followup.send("❌ Time not set yet. Use /settime first.", ephemeral=True)
            return

        try:
            text = await _rcon_command("GetGameLog", timeout=12.0)
            parsed = find_latest_timed_entry_from_getgamelog(text)
            if not parsed:
                await i.followup.send("❌ No timed line found in GetGameLog (no 'Day X, HH:MM:SS' in recent output).", ephemeral=True)
                return

            changed, msg = apply_sync_from_timed_log(parsed)

            # push webhook right away after manual sync
            if _webhook_upsert is not None:
                now_calc = _calc_now()
                if now_calc:
                    mo, dd, yy, _ = now_calc
                    await _webhook_upsert("time", _build_time_embed(mo, dd, yy))

            await i.followup.send(("✅ " if changed else "ℹ️ ") + msg, ephemeral=True)

        except Exception as e:
            await i.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

    print("[time_module] ✅ /settime and /sync registered (minute-precise GetGameLog sync)")

# =====================
# LOOP
# =====================
async def run_time_loop(client: discord.Client, rcon_command, webhook_upsert):
    """
    - Updates the time webhook only on round step minutes
    - Announces new day in ANNOUNCE_CHANNEL_ID
    - Auto-syncs using the MOST RECENT timed GetGameLog line whenever it changes
    """
    global _state, _last_announced_abs_day, _last_synced_marker

    _state = load_state()

    last_sync_check_ts = 0.0

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now_calc = _calc_now()
                if not now_calc:
                    await asyncio.sleep(5)
                    continue

                minute_of_day, day, year, seconds_into_minute = now_calc

                # --- Auto sync: check for newest timed gamelog line ---
                if rcon_command is not None and (time.time() - last_sync_check_ts) >= AUTO_SYNC_EVERY_SECONDS:
                    last_sync_check_ts = time.time()
                    try:
                        text = await rcon_command("GetGameLog", timeout=12.0)
                        parsed = find_latest_timed_entry_from_getgamelog(text)
                        if parsed:
                            marker = parsed.get("marker")
                            if marker and marker != _last_synced_marker:
                                changed, msg = apply_sync_from_timed_log(parsed)
                                _last_synced_marker = marker
                                if changed:
                                    print(f"[time_module] Auto-sync: {msg}")
                                    # recalc and push webhook immediately after a real sync
                                    now_calc2 = _calc_now()
                                    if now_calc2:
                                        mo, dd, yy, _ = now_calc2
                                        embed2 = _build_time_embed(mo, dd, yy)
                                        await webhook_upsert(session, WEBHOOK_URL, "time", embed2)
                        else:
                            # keep this quiet-ish; only print occasionally if you want
                            print("[time_module] Auto-sync: No timed line found in GetGameLog.")
                    except Exception as e:
                        print(f"[time_module] Auto-sync error: {e}")

                    # refresh local calc after potential sync
                    now_calc = _calc_now()
                    if now_calc:
                        minute_of_day, day, year, seconds_into_minute = now_calc

                # --- Update webhook on round step ---
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