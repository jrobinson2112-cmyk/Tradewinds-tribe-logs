import os
import time
import asyncio
import discord
from discord import app_commands
from typing import Optional, Tuple, Callable, Awaitable

import tribelogs_module  # <-- we read latest gametime from here

# =========================
# ENV / CONFIG
# =========================

TIME_WEBHOOK_KEY = "time"

# Post/edit interval for the time embed
TIME_UPDATE_SECONDS = float(os.getenv("TIME_UPDATE_SECONDS", "30") or "30")

# Auto-sync interval (10 mins requested)
TIME_AUTO_SYNC_SECONDS = float(os.getenv("TIME_AUTO_SYNC_SECONDS", "600") or "600")

# Drift threshold: only shift if off by >= N minutes
SYNC_DRIFT_MINUTES = int(os.getenv("SYNC_DRIFT_MINUTES", "2") or "2")

# Icon boundaries (ARK default): day starts 05:30, night starts 17:30
DAY_START_MINUTE = 5 * 60 + 30
NIGHT_START_MINUTE = 17 * 60 + 30

# Nitrado multipliers you showed:
DAY_CYCLE_SPEED = float(os.getenv("DAY_CYCLE_SPEED", "5.92") or "5.92")
DAY_TIME_SPEED = float(os.getenv("DAY_TIME_SPEED", "1.85") or "1.85")
NIGHT_TIME_SPEED = float(os.getenv("NIGHT_TIME_SPEED", "2.18") or "2.18")

# Default total cycle is 1 hour realtime (3600s) per in-game day-night cycle
DEFAULT_CYCLE_SECONDS = float(os.getenv("DEFAULT_CYCLE_SECONDS", "3600") or "3600")

# Channel name prefix
TIME_PREFIX = os.getenv("TIME_PREFIX", "Solunaris")

# =========================
# INTERNAL STATE
# =========================

_state_set = False
_epoch_real = 0.0
_epoch_day = 0
_epoch_minute_of_day = 0  # 0..1439

# rcon + webhook upsert injected by main.py
_rcon_command: Optional[Callable[[str], Awaitable[str]]] = None
_webhook_upsert: Optional[Callable[..., Awaitable[None]]] = None


def _clamp_minute(m: int) -> int:
    return max(0, min(1439, m))


def _is_daytime(minute_of_day: int) -> bool:
    # Day is [05:30, 17:30)
    return DAY_START_MINUTE <= minute_of_day < NIGHT_START_MINUTE


def _calc_rate_seconds_per_game_minute(minute_of_day: int) -> float:
    """
    Convert SPM multipliers into "real seconds per 1 in-game minute"
    using DEFAULT_CYCLE_SECONDS *and* the three multipliers.

    Model:
      - DayCycleSpeedScale scales the whole cycle duration.
      - DayTimeSpeedScale affects the daytime half (720 in-game minutes).
      - NightTimeSpeedScale affects the night half (720 in-game minutes).
    """
    # Base cycle duration adjusted by DayCycleSpeedScale
    cycle_seconds = DEFAULT_CYCLE_SECONDS / max(0.0001, DAY_CYCLE_SPEED)

    # Split cycle across day/night halves then apply time speed scales.
    # Higher time speed => in-game time passes faster => fewer real seconds per in-game minute.
    day_seconds = (cycle_seconds / 2.0) / max(0.0001, DAY_TIME_SPEED)
    night_seconds = (cycle_seconds / 2.0) / max(0.0001, NIGHT_TIME_SPEED)

    if _is_daytime(minute_of_day):
        return day_seconds / 720.0
    else:
        return night_seconds / 720.0


def _advance_from_epoch(now: float) -> Tuple[int, int]:
    """
    Returns (day, minute_of_day) based on epoch and SPM-scaled progression.
    We step minute-by-minute so the rate changes correctly at day/night boundary.
    """
    day = _epoch_day
    minute = _epoch_minute_of_day
    t = _epoch_real

    while t < now:
        spm = _calc_rate_seconds_per_game_minute(minute)
        t_next = t + spm
        if t_next > now:
            break
        t = t_next
        minute += 1
        if minute >= 1440:
            minute = 0
            day += 1

    return day, minute


def _minute_to_hhmm(minute_of_day: int) -> str:
    hh = minute_of_day // 60
    mm = minute_of_day % 60
    return f"{hh:02d}:{mm:02d}"


def _wrap_day_diff(a: int, b: int) -> int:
    diff = a - b
    if diff > 180:
        diff -= 365
    if diff < -180:
        diff += 365
    return diff


def _apply_sync(day: int, hh: int, mm: int, ss: int) -> Tuple[bool, str]:
    """
    Shift epoch so predicted time aligns to parsed (day, hh:mm:ss).
    """
    global _epoch_real, _state_set

    if not _state_set:
        return False, "No state set yet (use /settime first)."

    now = time.time()
    cur_day, cur_minute = _advance_from_epoch(now)
    target_minute = _clamp_minute(hh * 60 + mm)

    day_diff = _wrap_day_diff(day, cur_day)
    minute_diff = day_diff * 1440 + (target_minute - cur_minute)

    # wrap minute diff to nearest day direction
    while minute_diff > 720:
        minute_diff -= 1440
    while minute_diff < -720:
        minute_diff += 1440

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {minute_diff} min (<{SYNC_DRIFT_MINUTES}m), no change."

    # Approximate using current spm (good enough; next loop refines)
    spm = _calc_rate_seconds_per_game_minute(cur_minute)
    shift_seconds = minute_diff * spm

    _epoch_real -= shift_seconds
    return True, f"Synced by {minute_diff} minutes."


def _build_time_embed(day: int, minute_of_day: int) -> dict:
    icon = "☀️" if _is_daytime(minute_of_day) else "🌙"
    hhmm = _minute_to_hhmm(minute_of_day)

    return {
        "title": f"{icon} | {TIME_PREFIX} Time",
        "description": f"**{hhmm} | Day {day}**",
        "color": 0x3498DB if icon == "🌙" else 0xF1C40F,
    }


async def _sync_from_tribelogs() -> Tuple[bool, str]:
    """
    Uses the most recently parsed Day/Time from tribelogs_module.
    """
    gt = tribelogs_module.get_latest_gametime()
    if not gt:
        return False, "No Day/Time found in Tribe Logs."
    day, hh, mm, ss = gt
    ok, msg = _apply_sync(day, hh, mm, ss)
    age = tribelogs_module.get_latest_gametime_age_seconds()
    if age is not None:
        msg = f"{msg} (tribelog age: {age:.0f}s)"
    return ok, msg


async def run_time_loop(
    client: discord.Client,
    rcon_command,
    webhook_upsert,
) -> None:
    """
    Main loop: updates time embed and auto-syncs from tribe logs every TIME_AUTO_SYNC_SECONDS.
    """
    global _rcon_command, _webhook_upsert
    _rcon_command = rcon_command
    _webhook_upsert = webhook_upsert

    await client.wait_until_ready()

    last_sync = 0.0
    while True:
        try:
            if _state_set:
                now = time.time()
                day, minute = _advance_from_epoch(now)
                embed = _build_time_embed(day, minute)
                await _webhook_upsert(TIME_WEBHOOK_KEY, embed)

                if now - last_sync >= TIME_AUTO_SYNC_SECONDS:
                    last_sync = now
                    ok, msg = await _sync_from_tribelogs()
                    if ok:
                        print(f"[time_module] Auto-sync OK: {msg}")
                    else:
                        print(f"[time_module] Auto-sync: {msg}")

        except Exception as e:
            print(f"[time_module] loop error: {e}")

        await asyncio.sleep(TIME_UPDATE_SECONDS)


def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: Optional[int] = None, rcon_command=None) -> None:
    guild_obj = discord.Object(id=guild_id)

    def _is_admin(interaction: discord.Interaction) -> bool:
        if not admin_role_id:
            return True
        if not interaction.user or not hasattr(interaction.user, "roles"):
            return False
        return any(getattr(r, "id", None) == admin_role_id for r in interaction.user.roles)

    @tree.command(name="settime", description="Set the time baseline (Day + HH:MM)", guild=guild_obj)
    @app_commands.describe(day="In-game day number", time_hhmm="Time like 07:12 or 07:12:15")
    async def settime_cmd(interaction: discord.Interaction, day: int, time_hhmm: str):
        global _state_set, _epoch_real, _epoch_day, _epoch_minute_of_day

        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)

        try:
            parts = time_hhmm.strip().split(":")
            hh = int(parts[0])
            mm = int(parts[1])
        except Exception:
            return await interaction.response.send_message("❌ Format must be HH:MM (or HH:MM:SS).", ephemeral=True)

        minute = _clamp_minute(hh * 60 + mm)
        _epoch_real = time.time()
        _epoch_day = int(day)
        _epoch_minute_of_day = int(minute)
        _state_set = True

        await interaction.response.send_message("✅ Time set.", ephemeral=True)

    @tree.command(name="sync", description="Sync time using latest Tribe Logs timestamp", guild=guild_obj)
    async def sync_cmd(interaction: discord.Interaction):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)

        ok, msg = await _sync_from_tribelogs()
        if ok:
            await interaction.response.send_message(f"✅ {msg}", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ {msg}", ephemeral=True)

    print("[time_module] ✅ /settime and /sync registered")