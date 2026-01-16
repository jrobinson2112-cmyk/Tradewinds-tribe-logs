# time_module.py
# Tradewinds / Solunaris Time Module
#
# ✅ /settime (admin-only): set baseline Day + HH:MM (optional Year)
# ✅ /sync   (admin-only): force sync from TribeLogs (preferred), fallback GetGameLog
# ✅ Auto-sync every 10 minutes (AUTO_SYNC_SECONDS) from TribeLogs timestamps
# ✅ Updates one webhook embed in-place (via webhook_upsert passed from main.py)
# ✅ Day/Night based on ASA default: DAY = 05:30 -> 17:30
# ✅ Speed scales (SPM multipliers) applied:
#     DayCycleSpeedScale, DayTimeSpeedScale, NightTimeSpeedScale
# ✅ Daily message at start of each new in-game day (TIME_DAILY_CHANNEL_ID)
#
# Notes:
# - This module PREFERS TribeLogs time. GetGameLog is ONLY fallback.

from __future__ import annotations

import os
import re
import json
import time
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple, Dict

import discord
from discord import app_commands

# ----------------------------
# CONFIG (ENV)
# ----------------------------

# ASA default (Nitrado): daytime is 05:30 -> 17:30
DAY_START_MINUTE = 5 * 60 + 30    # 330
DAY_END_MINUTE   = 17 * 60 + 30   # 1050

DAY_LENGTH_INGAME_MINUTES = DAY_END_MINUTE - DAY_START_MINUTE          # 720
NIGHT_LENGTH_INGAME_MINUTES = 1440 - DAY_LENGTH_INGAME_MINUTES         # 720

# ASA default: full cycle = 1 hour realtime
BASE_CYCLE_SECONDS = float(os.getenv("BASE_CYCLE_SECONDS", "3600"))
BASE_DAY_SECONDS = BASE_CYCLE_SECONDS / 2.0
BASE_NIGHT_SECONDS = BASE_CYCLE_SECONDS / 2.0

# Your Nitrado multipliers (defaults to your screenshot)
DAY_CYCLE_SPEED_SCALE = float(os.getenv("DAY_CYCLE_SPEED_SCALE", "5.92"))
DAY_TIME_SPEED_SCALE = float(os.getenv("DAY_TIME_SPEED_SCALE", "1.85"))
NIGHT_TIME_SPEED_SCALE = float(os.getenv("NIGHT_TIME_SPEED_SCALE", "2.18"))

# Loop timing
TIME_UPDATE_SECONDS = float(os.getenv("TIME_UPDATE_SECONDS", "15"))
AUTO_SYNC_SECONDS = float(os.getenv("AUTO_SYNC_SECONDS", "600"))  # 10 minutes

# Sync threshold (minutes)
SYNC_DRIFT_MINUTES = float(os.getenv("SYNC_DRIFT_MINUTES", "2"))

# Daily message channel (optional)
TIME_DAILY_CHANNEL_ID = int(os.getenv("TIME_DAILY_CHANNEL_ID", "0") or "0")

# Persistence (Railway volume path)
TIME_STATE_PATH = os.getenv("TIME_STATE_PATH", "/data/time_state.json")

# Webhook key name used by your webhook_upsert in main.py
TIME_WEBHOOK_KEY = os.getenv("TIME_WEBHOOK_KEY", "time")

# Cosmetics
TIME_TITLE = os.getenv("TIME_TITLE", "Solunaris Time")
DAY_EMOJI = os.getenv("TIME_DAY_EMOJI", "☀️")
NIGHT_EMOJI = os.getenv("TIME_NIGHT_EMOJI", "🌙")

COLOR_DAY = int(os.getenv("TIME_COLOR_DAY", "0xF1C40F"), 16) if str(os.getenv("TIME_COLOR_DAY", "")).startswith("0x") else int(os.getenv("TIME_COLOR_DAY", "15844367"))
COLOR_NIGHT = int(os.getenv("TIME_COLOR_NIGHT", "0x3498DB"), 16) if str(os.getenv("TIME_COLOR_NIGHT", "")).startswith("0x") else int(os.getenv("TIME_COLOR_NIGHT", "3447003"))

# ----------------------------
# REGEX (in-game Day/Time)
# ----------------------------

# Matches: "Day 294, 07:12:15"
_DAYTIME_RE = re.compile(r"Day\s+(\d+)\s*,\s*(\d{1,2}):(\d{2}):(\d{2})", re.IGNORECASE)
_DAYTIME_RE2 = re.compile(r"Day\s+(\d+)\s*(?:,)?\s*(\d{1,2}):(\d{2}):(\d{2})", re.IGNORECASE)

# ----------------------------
# STATE
# ----------------------------

@dataclass
class TimeState:
    epoch_real_ts: float = 0.0
    epoch_day: int = 1
    epoch_minute_of_day: int = 0  # 0..1439
    year: int = 1
    last_announced_day: int = 0

    def is_set(self) -> bool:
        return self.epoch_real_ts > 0.0


_state = TimeState()

_rcon_command: Optional[Callable[[str], Any]] = None
_webhook_upsert: Optional[Callable[..., Any]] = None


# ----------------------------
# HELPERS
# ----------------------------

def _is_daytime_minute(minute_of_day: int) -> bool:
    return DAY_START_MINUTE <= minute_of_day < DAY_END_MINUTE

def _scaled_day_seconds() -> float:
    effective = max(0.0001, DAY_CYCLE_SPEED_SCALE * DAY_TIME_SPEED_SCALE)
    return BASE_DAY_SECONDS / effective

def _scaled_night_seconds() -> float:
    effective = max(0.0001, DAY_CYCLE_SPEED_SCALE * NIGHT_TIME_SPEED_SCALE)
    return BASE_NIGHT_SECONDS / effective

def _seconds_per_ingame_minute(minute_of_day: int) -> float:
    if _is_daytime_minute(minute_of_day):
        return _scaled_day_seconds() / float(DAY_LENGTH_INGAME_MINUTES)
    return _scaled_night_seconds() / float(NIGHT_LENGTH_INGAME_MINUTES)

def _format_hhmm(minute_of_day: int) -> str:
    h = minute_of_day // 60
    m = minute_of_day % 60
    return f"{h:02d}:{m:02d}"

def _minute_of_day_from_hms(hh: int, mm: int, ss: int) -> int:
    return hh * 60 + mm

def _wrap_day_diff(d: int) -> int:
    if d > 180:
        d -= 365
    elif d < -180:
        d += 365
    return d

def _wrap_minute_diff(m: int) -> int:
    while m > 720:
        m -= 1440
    while m < -720:
        m += 1440
    return m

async def _maybe_await(x):
    if asyncio.iscoroutine(x):
        return await x
    return x


# ----------------------------
# PERSIST
# ----------------------------

def _load_state() -> None:
    global _state
    try:
        if not os.path.exists(TIME_STATE_PATH):
            return
        with open(TIME_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        _state = TimeState(
            epoch_real_ts=float(data.get("epoch_real_ts", 0.0)),
            epoch_day=int(data.get("epoch_day", 1)),
            epoch_minute_of_day=int(data.get("epoch_minute_of_day", 0)),
            year=int(data.get("year", 1)),
            last_announced_day=int(data.get("last_announced_day", 0)),
        )
    except Exception as e:
        print(f"[time_module] WARN: failed to load state: {e}")

def _save_state() -> None:
    try:
        os.makedirs(os.path.dirname(TIME_STATE_PATH), exist_ok=True)
        with open(TIME_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "epoch_real_ts": _state.epoch_real_ts,
                    "epoch_day": _state.epoch_day,
                    "epoch_minute_of_day": _state.epoch_minute_of_day,
                    "year": _state.year,
                    "last_announced_day": _state.last_announced_day,
                },
                f,
                indent=2,
            )
    except Exception as e:
        print(f"[time_module] WARN: failed to save state: {e}")


# ----------------------------
# TIME MATH
# ----------------------------

def _advance_from_epoch(real_now: float) -> Tuple[int, int, int]:
    if not _state.is_set():
        return (0, 0, 0)

    day = _state.epoch_day
    minute_of_day = _state.epoch_minute_of_day
    year = _state.year

    remaining = max(0.0, real_now - _state.epoch_real_ts)

    # Step minute-by-minute (minutes are short real-time after multipliers; safe)
    max_steps = 250000
    steps = 0

    while remaining > 0.000001 and steps < max_steps:
        spm = _seconds_per_ingame_minute(minute_of_day)
        if remaining < spm:
            break
        remaining -= spm
        minute_of_day += 1
        if minute_of_day >= 1440:
            minute_of_day = 0
            day += 1
            if day > 365:
                day = 1
                year += 1
        steps += 1

    return (day, minute_of_day, year)

def _compute_now_embed() -> Optional[Tuple[Dict[str, Any], int, int, int, bool]]:
    if not _state.is_set():
        return None

    day, minute_of_day, year = _advance_from_epoch(time.time())
    is_day = _is_daytime_minute(minute_of_day)

    emoji = DAY_EMOJI if is_day else NIGHT_EMOJI
    color = COLOR_DAY if is_day else COLOR_NIGHT

    hhmm = _format_hhmm(minute_of_day)

    # Keep your original style
    desc = f"{emoji} | {TIME_TITLE}\n\n{hhmm} | Day {day}"

    # If you want Year displayed too, set env SHOW_YEAR=1
    if os.getenv("SHOW_YEAR", "0") == "1":
        desc = f"{emoji} | {TIME_TITLE}\n\n{hhmm} | Day {day} | Year {year}"

    embed = {"title": TIME_TITLE, "description": desc, "color": color}
    return (embed, day, minute_of_day, year, is_day)


# ----------------------------
# PARSING
# ----------------------------

def _parse_daytime_from_text(text: str) -> Optional[Tuple[int, int, int, int]]:
    if not text:
        return None
    for ln in text.splitlines():
        m = _DAYTIME_RE.search(ln) or _DAYTIME_RE2.search(ln)
        if not m:
            continue
        day = int(m.group(1))
        hh = int(m.group(2))
        mm = int(m.group(3))
        ss = int(m.group(4))
        return (day, hh, mm, ss)
    return None


# ----------------------------
# TRIBELOGS TIME SOURCE (PRIMARY)
# ----------------------------

def _latest_from_dict_with_ts(d: dict) -> Optional[Tuple[int, int, int, int]]:
    # Supports dict values like:
    #   {"tribe": (day,hh,mm,ss)}
    #   {"tribe": {"time": (day,hh,mm,ss), "ts": 123456}}
    best = None
    best_ts = -1.0
    for v in d.values():
        if isinstance(v, tuple) and len(v) >= 4:
            # no timestamp; treat as ts=0
            if best is None:
                best = (int(v[0]), int(v[1]), int(v[2]), int(v[3]))
            continue
        if isinstance(v, dict):
            t = v.get("time") or v.get("game_time") or v.get("latest_time")
            ts = float(v.get("ts") or v.get("timestamp") or 0.0)
            if isinstance(t, tuple) and len(t) >= 4 and ts >= best_ts:
                best_ts = ts
                best = (int(t[0]), int(t[1]), int(t[2]), int(t[3]))
    return best

def _get_latest_time_from_tribelogs() -> Optional[Tuple[int, int, int, int]]:
    """
    Pull the latest parsed Day/Time from tribelogs_module.
    This is the PRIMARY source (what you asked for).
    """
    try:
        import tribelogs_module  # type: ignore
    except Exception:
        return None

    # 1) Preferred function
    fn = getattr(tribelogs_module, "get_latest_game_time", None)
    if callable(fn):
        try:
            t = fn()
            if isinstance(t, tuple) and len(t) >= 4:
                return (int(t[0]), int(t[1]), int(t[2]), int(t[3]))
        except Exception:
            pass

    # 2) Common global tuple
    for attr in ("LATEST_GAME_TIME", "LAST_GAME_TIME", "LATEST_DAYTIME", "LAST_DAYTIME"):
        t = getattr(tribelogs_module, attr, None)
        if isinstance(t, tuple) and len(t) >= 4:
            try:
                return (int(t[0]), int(t[1]), int(t[2]), int(t[3]))
            except Exception:
                pass

    # 3) Common global dicts (per-tribe)
    for attr in ("LATEST_GAME_TIME_BY_TRIBE", "LATEST_INGAME_TIME", "LAST_GAME_TIME_BY_TRIBE"):
        d = getattr(tribelogs_module, attr, None)
        if isinstance(d, dict) and d:
            t = _latest_from_dict_with_ts(d)
            if t:
                return t

    # 4) Last raw line string
    for attr in ("LATEST_RAW_LOG_LINE", "LAST_RAW_LOG_LINE", "LATEST_TRIBELOG_LINE", "LAST_TRIBELOG_LINE"):
        s = getattr(tribelogs_module, attr, None)
        if isinstance(s, str) and s.strip():
            p = _parse_daytime_from_text(s)
            if p:
                return p

    return None


# ----------------------------
# RCON GetGameLog (FALLBACK ONLY)
# ----------------------------

async def _get_latest_time_from_getgamelog() -> Optional[Tuple[int, int, int, int]]:
    if _rcon_command is None:
        return None
    try:
        out = await _maybe_await(_rcon_command("GetGameLog"))
        if not isinstance(out, str):
            out = str(out)
        return _parse_daytime_from_text(out)
    except Exception as e:
        print(f"[time_module] GetGameLog error: {e}")
        return None

async def _get_reference_time() -> Optional[Tuple[int, int, int, int]]:
    # PRIMARY: Tribe logs
    t = _get_latest_time_from_tribelogs()
    if t:
        return t
    # FALLBACK ONLY: GetGameLog
    return await _get_latest_time_from_getgamelog()


# ----------------------------
# SYNC APPLY
# ----------------------------

def _apply_sync(target_day: int, hh: int, mm: int, ss: int, force: bool = False) -> Tuple[bool, str]:
    if not _state.is_set():
        return False, "No state set (use /settime first)."

    now = time.time()
    cur_day, cur_minute_of_day, _cur_year = _advance_from_epoch(now)
    target_minute_of_day = _minute_of_day_from_hms(hh, mm, ss)

    day_diff = _wrap_day_diff(target_day - cur_day)
    minute_diff = day_diff * 1440 + (target_minute_of_day - cur_minute_of_day)
    minute_diff = _wrap_minute_diff(minute_diff)

    if not force and abs(minute_diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {minute_diff:+.0f} min (< {SYNC_DRIFT_MINUTES} min), not syncing."

    # Convert in-game minutes drift into real seconds drift
    spm = _seconds_per_ingame_minute(cur_minute_of_day)
    delta_real = minute_diff * spm

    _state.epoch_real_ts -= delta_real
    _save_state()
    return True, f"Synced using drift {minute_diff:+.0f} min."


# ----------------------------
# DISCORD COMMANDS
# ----------------------------

def _has_admin_role(interaction: discord.Interaction, admin_role_id: int) -> bool:
    try:
        if not admin_role_id:
            return True
        member = interaction.user
        if isinstance(member, discord.Member):
            return any(r.id == admin_role_id for r in member.roles)
        return False
    except Exception:
        return False

def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int = 0, rcon_command: Optional[Callable[[str], Any]] = None):
    """
    Register /settime and /sync.
    main.py can call this with:
      setup_time_commands(tree, GUILD_ID, ADMIN_ROLE_ID, rcon_cmd)
    """
    global _rcon_command
    if rcon_command is not None:
        _rcon_command = rcon_command

    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(name="settime", description="Set in-game time baseline (Day + HH:MM).", guild=guild_obj)
    @app_commands.describe(day="In-game Day number", hhmm="Time HH:MM (24h)", year="In-game Year (optional)")
    async def settime(interaction: discord.Interaction, day: int, hhmm: str, year: int = 0):
        if admin_role_id and not _has_admin_role(interaction, int(admin_role_id)):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return

        m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", hhmm)
        if not m:
            await interaction.response.send_message("❌ Invalid time format. Use HH:MM (e.g. 05:30).", ephemeral=True)
            return

        hh = int(m.group(1))
        mm = int(m.group(2))
        if hh < 0 or hh > 23 or mm < 0 or mm > 59:
            await interaction.response.send_message("❌ Invalid time values.", ephemeral=True)
            return

        _state.epoch_real_ts = time.time()
        _state.epoch_day = int(day)
        _state.epoch_minute_of_day = hh * 60 + mm
        if year and year > 0:
            _state.year = int(year)

        # Announce baseline
        _state.last_announced_day = int(day)

        _save_state()
        await interaction.response.send_message("✅ Time set.", ephemeral=True)

    @tree.command(name="sync", description="Force sync from TribeLogs timestamps (fallback GetGameLog).", guild=guild_obj)
    async def sync(interaction: discord.Interaction):
        if admin_role_id and not _has_admin_role(interaction, int(admin_role_id)):
            await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
            return

        ref = await _get_reference_time()
        if not ref:
            await interaction.response.send_message("ℹ️ No Day/Time found in Tribe Logs (or fallback GetGameLog).", ephemeral=True)
            return

        ok, msg = _apply_sync(ref[0], ref[1], ref[2], ref[3], force=True)
        if ok:
            await interaction.response.send_message(f"✅ {msg}", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ {msg}", ephemeral=True)

    print("[time_module] ✅ /settime and /sync registered")


# ----------------------------
# DAILY MESSAGE
# ----------------------------

async def _post_daily_message_if_needed(client: discord.Client, cur_day: int, cur_year: int):
    if not TIME_DAILY_CHANNEL_ID:
        return
    if not _state.is_set():
        return

    if _state.last_announced_day == 0:
        _state.last_announced_day = cur_day
        _save_state()
        return

    if cur_day != _state.last_announced_day:
        try:
            ch = client.get_channel(TIME_DAILY_CHANNEL_ID)
            if ch is None:
                ch = await client.fetch_channel(TIME_DAILY_CHANNEL_ID)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                # Match your older daily message style if you had one
                await ch.send(f"Day {cur_day}, Year {cur_year}")
                _state.last_announced_day = cur_day
                _save_state()
        except Exception as e:
            print(f"[time_module] daily message error: {e}")


# ----------------------------
# AUTO SYNC LOOP
# ----------------------------

async def _auto_sync_loop():
    while True:
        await asyncio.sleep(AUTO_SYNC_SECONDS)
        try:
            if not _state.is_set():
                continue

            ref = await _get_reference_time()
            if not ref:
                # This should go away once tribelogs_module exposes latest time.
                print("[time_module] Auto-sync: No parsable Day/Time found in Tribe Logs (fallback GetGameLog).")
                continue

            ok, msg = _apply_sync(ref[0], ref[1], ref[2], ref[3], force=False)
            if ok:
                print(f"[time_module] Auto-sync: {msg}")

        except Exception as e:
            print(f"[time_module] Auto-sync loop error: {e}")


# ----------------------------
# MAIN LOOP
# ----------------------------

async def run_time_loop(client: discord.Client, rcon_command: Optional[Callable[[str], Any]], webhook_upsert: Callable[..., Any]):
    """
    main.py MUST call:
      asyncio.create_task(time_module.run_time_loop(client, rcon_cmd, webhook_upsert))
    """
    global _rcon_command, _webhook_upsert
    _rcon_command = rcon_command
    _webhook_upsert = webhook_upsert

    _load_state()
    await client.wait_until_ready()

    # background auto-sync
    asyncio.create_task(_auto_sync_loop())

    while True:
        try:
            await asyncio.sleep(TIME_UPDATE_SECONDS)

            now = _compute_now_embed()
            if not now:
                continue

            embed, day, _mod, year, _is_day = now

            # Update the webhook embed in-place
            await _maybe_await(_webhook_upsert(TIME_WEBHOOK_KEY, embed))

            # Daily announcement
            await _post_daily_message_if_needed(client, day, year)

        except Exception as e:
            print(f"[time_module] time loop error: {e}")
            await asyncio.sleep(5)