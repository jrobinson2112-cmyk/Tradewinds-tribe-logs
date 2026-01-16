import os
import json
import time
import asyncio
from dataclasses import dataclass
from typing import Optional, Dict, Any, Callable, Tuple

import discord
from discord import app_commands

# ============================================================
# CONFIG
# ============================================================

# These MUST match what tribelogs_module uses
TRIBELOGS_TIMEHINT_FILE = os.getenv("TRIBELOGS_TIMEHINT_FILE", "/data/tribelog_latest_time.json")

# Where we persist time state (also on volume)
TIME_DATA_DIR = os.getenv("TIME_DATA_DIR", "/data")
TIME_STATE_FILE = os.getenv("TIME_STATE_FILE", f"{TIME_DATA_DIR}/time_state.json")

# Channel to post the daily "new day" message into
TIME_DAILY_CHANNEL_ID = int(os.getenv("TIME_DAILY_CHANNEL_ID", "0") or "0")

# How often to update the time embed
TIME_TICK_SECONDS = float(os.getenv("TIME_TICK_SECONDS", "5"))

# Auto-sync from tribe logs every X seconds (10 minutes default)
TIME_AUTOSYNC_SECONDS = int(os.getenv("TIME_AUTOSYNC_SECONDS", "600"))

# If drift is smaller than this, don't resync (minutes)
TIME_SYNC_DRIFT_MINUTES = int(os.getenv("TIME_SYNC_DRIFT_MINUTES", "3"))

# Nitrado multipliers (your SPM)
DAY_CYCLE_SPEED = float(os.getenv("DAY_CYCLE_SPEED_SCALE", "1.0"))      # DayCycleSpeedScale
DAY_TIME_SPEED = float(os.getenv("DAY_TIME_SPEED_SCALE", "1.0"))        # DayTimeSpeedScale
NIGHT_TIME_SPEED = float(os.getenv("NIGHT_TIME_SPEED_SCALE", "1.0"))    # NightTimeSpeedScale

# Default ASA day-night: 1 hour total (from Nitrado guide)
# We model day = 30m, night = 30m at baseline (then apply multipliers separately).
DEFAULT_TOTAL_SECONDS = 3600.0
DEFAULT_DAY_SECONDS = 1800.0
DEFAULT_NIGHT_SECONDS = 1800.0

# Emoji display
DAY_EMOJI = os.getenv("TIME_DAY_EMOJI", "☀️")
NIGHT_EMOJI = os.getenv("TIME_NIGHT_EMOJI", "🌙")

# ============================================================
# STATE
# ============================================================

@dataclass
class TimeState:
    # epoch that corresponds to "minute 0 of in-game day" reference
    epoch_real: float
    # reference in-game day number at epoch_real
    day: int
    # reference in-game minute-of-day (0..1439) at epoch_real
    minute_of_day: int
    # year (optional)
    year: int = 1
    # last day we announced
    last_announced_day: int = -1

_state: Optional[TimeState] = None

def _ensure_dir(path: str):
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass

def _save_state():
    if not _state:
        return
    _ensure_dir(TIME_DATA_DIR)
    payload = {
        "epoch_real": _state.epoch_real,
        "day": _state.day,
        "minute_of_day": _state.minute_of_day,
        "year": _state.year,
        "last_announced_day": _state.last_announced_day,
        "saved_at": int(time.time()),
    }
    with open(TIME_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def _load_state():
    global _state
    try:
        with open(TIME_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _state = TimeState(
            epoch_real=float(data["epoch_real"]),
            day=int(data["day"]),
            minute_of_day=int(data["minute_of_day"]),
            year=int(data.get("year", 1)),
            last_announced_day=int(data.get("last_announced_day", -1)),
        )
    except Exception:
        _state = None

# ============================================================
# TIME MATH (SPM multipliers)
# ============================================================

def _scaled_day_seconds() -> float:
    # Higher DayTimeSpeedScale -> faster day -> fewer real seconds
    # Also DayCycleSpeedScale affects overall cycle. We apply both.
    # Effective speed multiplier:
    speed = max(0.01, DAY_CYCLE_SPEED) * max(0.01, DAY_TIME_SPEED)
    return DEFAULT_DAY_SECONDS / speed

def _scaled_night_seconds() -> float:
    speed = max(0.01, DAY_CYCLE_SPEED) * max(0.01, NIGHT_TIME_SPEED)
    return DEFAULT_NIGHT_SECONDS / speed

def _seconds_per_ingame_minute(minute_of_day: int) -> float:
    # Assume day is 06:00-18:00? We keep simple: first half = "day", second half = "night"
    # If you want a different split later we can adjust.
    if minute_of_day < 720:
        return _scaled_day_seconds() / 720.0
    return _scaled_night_seconds() / 720.0

def _advance_minutes_from_epoch(delta_seconds: float, start_minute: int) -> int:
    # Convert real seconds to in-game minutes progressed, accounting for different day/night speeds.
    m = start_minute
    remaining = delta_seconds
    progressed = 0
    # step minute-by-minute but fast enough (delta small, tick small)
    while remaining > 0:
        spm = _seconds_per_ingame_minute(m)
        if remaining < spm:
            break
        remaining -= spm
        progressed += 1
        m = (m + 1) % 1440
    return progressed

def _compute_now() -> Optional[Tuple[int, int, int, int, bool]]:
    """
    Returns (day, hour, minute, second, is_daytime)
    """
    if not _state:
        return None

    now = time.time()
    delta = max(0.0, now - _state.epoch_real)

    # step in whole in-game minutes
    progressed_minutes = _advance_minutes_from_epoch(delta, _state.minute_of_day)
    cur_minute_of_day = (_state.minute_of_day + progressed_minutes) % 1440

    # compute day rollover based on progressed minutes
    total_minutes = _state.minute_of_day + progressed_minutes
    day_offset = total_minutes // 1440
    cur_day = _state.day + day_offset

    # seconds within current minute (roughly)
    # We compute leftover seconds after consuming full minutes for a nicer display.
    # Re-run a partial to get remaining quickly:
    # (good enough – the embed is for humans)
    # Estimate seconds into minute:
    # we'll approximate using current minute SPM
    approx_spm = _seconds_per_ingame_minute(cur_minute_of_day)
    # how many seconds used by whole minutes (approx): progressed_minutes * avg_spm -> too rough
    # Instead accept "00" seconds always to avoid drift noise.
    sec = 0

    hh = cur_minute_of_day // 60
    mm = cur_minute_of_day % 60
    is_day = cur_minute_of_day < 720

    return cur_day, hh, mm, sec, is_day

# ============================================================
# TRIBE LOG TIMEHINT (the important bit)
# ============================================================

def _read_tribelog_timehint() -> Optional[Dict[str, Any]]:
    """
    Reads the JSON written by tribelogs_module.py:
      {"day":..., "hour":..., "minute":..., "second":..., "updated_at":...}
    """
    try:
        with open(TRIBELOGS_TIMEHINT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _set_state_from_daytime(day: int, hour: int, minute: int, second: int = 0):
    """
    Sets epoch so that NOW corresponds to given in-game time.
    """
    global _state
    minute_of_day = int(hour) * 60 + int(minute)
    _state = TimeState(
        epoch_real=time.time(),
        day=int(day),
        minute_of_day=minute_of_day,
        year=1,
        last_announced_day=(_state.last_announced_day if _state else -1),
    )
    _save_state()

def _minute_diff(a: int, b: int) -> int:
    # wrap-friendly minute difference
    d = a - b
    while d > 720:
        d -= 1440
    while d < -720:
        d += 1440
    return d
    # ============================================================
# DISCORD: EMBED BUILD + DAILY MESSAGE
# ============================================================

def _build_time_embed() -> Dict[str, Any]:
    computed = _compute_now()
    if not computed:
        return {
            "title": "Solunaris Time",
            "description": "⛔ Time not set. Use /settime Day Hour Minute",
            "color": 0xE74C3C,
        }

    day, hh, mm, ss, is_day = computed
    emoji = DAY_EMOJI if is_day else NIGHT_EMOJI

    return {
        "title": f"{emoji} | Solunaris Time",
        "description": f"**{hh:02d}:{mm:02d}** | **Day {day}**",
        "color": 0xF1C40F if is_day else 0x3498DB,
    }

async def _maybe_send_new_day_message(client: discord.Client, current_day: int):
    if not TIME_DAILY_CHANNEL_ID:
        return
    if not _state:
        return

    if _state.last_announced_day == current_day:
        return

    # Only announce when day actually advances (avoid spam if state resets)
    if _state.last_announced_day != -1 and current_day > _state.last_announced_day:
        try:
            ch = client.get_channel(TIME_DAILY_CHANNEL_ID)
            if ch is None:
                ch = await client.fetch_channel(TIME_DAILY_CHANNEL_ID)
            await ch.send(f"🌅 **A new day begins!** — Day **{current_day}**")
        except Exception as e:
            print(f"[time_module] Daily message send error: {e}")

    _state.last_announced_day = current_day
    _save_state()

# ============================================================
# SYNC LOGIC (FROM TRIBE LOGS TIMEHINT, NOT GetGameLog)
# ============================================================

def _apply_sync_from_timehint(timehint: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Sync to tribe-log timehint if drift is big enough.
    """
    if not timehint:
        return False, "No timehint available yet (no tribe logs captured Day/Time)."

    day = int(timehint.get("day", -1))
    hour = int(timehint.get("hour", -1))
    minute = int(timehint.get("minute", -1))
    second = int(timehint.get("second", 0) or 0)

    if day < 0 or hour < 0 or minute < 0:
        return False, "Timehint invalid."

    # If we have no state yet, just set it
    if not _state:
        _set_state_from_daytime(day, hour, minute, second)
        return True, f"Synced from tribe logs: Day {day}, {hour:02d}:{minute:02d}:{second:02d}"

    # compare drift
    cur = _compute_now()
    if not cur:
        _set_state_from_daytime(day, hour, minute, second)
        return True, f"Synced from tribe logs: Day {day}, {hour:02d}:{minute:02d}:{second:02d}"

    cur_day, cur_h, cur_m, _, _ = cur
    cur_mod = cur_h * 60 + cur_m
    target_mod = hour * 60 + minute

    day_diff = day - cur_day
    if day_diff > 180:
        day_diff -= 365
    elif day_diff < -180:
        day_diff += 365

    minute_diff = day_diff * 1440 + _minute_diff(target_mod, cur_mod)

    if abs(minute_diff) < TIME_SYNC_DRIFT_MINUTES:
        return False, f"Drift {minute_diff} minutes (<{TIME_SYNC_DRIFT_MINUTES}); no sync needed."

    _set_state_from_daytime(day, hour, minute, second)
    return True, f"Synced from tribe logs: Day {day}, {hour:02d}:{minute:02d}:{second:02d} (drift {minute_diff}m)"

# ============================================================
# COMMANDS
# ============================================================

def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int = 0, rcon_command=None):
    guild_obj = discord.Object(id=int(guild_id))

    def _is_admin(i: discord.Interaction) -> bool:
        if not admin_role_id:
            return True  # if not set, allow
        try:
            return any(getattr(r, "id", None) == int(admin_role_id) for r in i.user.roles)
        except Exception:
            return False

    @tree.command(name="settime", guild=guild_obj)
    async def settime(i: discord.Interaction, day: int, hour: int, minute: int):
        if not _is_admin(i):
            await i.response.send_message("❌ No permission.", ephemeral=True)
            return
        _set_state_from_daytime(day, hour, minute, 0)
        await i.response.send_message("✅ Time set.", ephemeral=True)

    @tree.command(name="sync", guild=guild_obj)
    async def sync(i: discord.Interaction):
        if not _is_admin(i):
            await i.response.send_message("❌ No permission.", ephemeral=True)
            return

        # IMPORTANT: sync from tribe logs timehint, not GetGameLog
        hint = _read_tribelog_timehint()
        ok, msg = _apply_sync_from_timehint(hint)
        await i.response.send_message(("✅ " if ok else "ℹ️ ") + msg, ephemeral=True)

    print("[time_module] ✅ /settime and /sync registered")

# ============================================================
# LOOP (updates webhook embed + autosync from tribe logs)
# ============================================================

async def run_time_loop(
    client: discord.Client,
    rcon_command,  # kept for compatibility; not used for time sync anymore
    webhook_upsert: Callable[..., Any],
):
    """
    - Updates the time embed continuously
    - Auto-syncs from tribe logs timehint every TIME_AUTOSYNC_SECONDS
    - Posts daily message when day advances
    """
    _load_state()
    await client.wait_until_ready()

    last_autosync = 0.0

    while True:
        try:
            now = time.time()

            # Auto-sync from tribelog timehint every N seconds
            if now - last_autosync >= TIME_AUTOSYNC_SECONDS:
                last_autosync = now
                hint = _read_tribelog_timehint()
                ok, msg = _apply_sync_from_timehint(hint)
                if not ok:
                    # This should NOT mention GetGameLog anymore
                    print(f"[time_module] Auto-sync: {msg}")
                else:
                    print(f"[time_module] Auto-sync: {msg}")

            # Build and upsert embed
            embed = _build_time_embed()
            # modules call webhook_upsert in different styles; yours supports key+embed
            await webhook_upsert("time", embed)

            # Daily message when day changes
            computed = _compute_now()
            if computed:
                cur_day, *_ = computed
                await _maybe_send_new_day_message(client, cur_day)

        except Exception as e:
            print(f"[time_module] loop error: {e}")

        await asyncio.sleep(TIME_TICK_SECONDS)