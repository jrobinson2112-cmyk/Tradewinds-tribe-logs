# time_module.py
# Accurate in-game clock for ASA using real-seconds-per-in-game-minute (SPM),
# with AUTO-SYNC from *timed* GetGameLog lines (e.g. "Day 327, 06:09:07: ...").
#
# ✅ Uses DAY/NIGHT SPM model for progression
# ✅ Auto-syncs whenever a *new* timed log line appears (poll-based)
# ✅ /sync now ONLY syncs from the latest timed GetGameLog line
# ✅ Ignores in-game seconds for display and syncing target (uses Day + HH:MM)
# ✅ Uses the real timestamp embedded in logs (YYYY.MM.DD_HH.MM.SS) as a better anchor epoch
# ✅ Does NOT parse "tribe-log lines in GetGameLog" anymore — only timed lines
#
# ENV:
#   WEBHOOK_URL (required)
#   SUNRISE_MINUTE=330   (05:30)
#   SUNSET_MINUTE=1050   (17:30)
#   DAY_SPM=...          (real seconds per in-game minute during day)
#   NIGHT_SPM=...        (real seconds per in-game minute during night)
#   TIME_UPDATE_STEP_MINUTES=10
#   AUTO_SYNC_POLL_SECONDS=10        (poll GetGameLog this often looking for timed lines)
#   SYNC_DRIFT_MINUTES=2             (only correct if drift >= this many minutes)
#   ANNOUNCE_CHANNEL_ID=...
#   TIME_STATE_FILE=/data/time_state.json
#
# NOTE:
# - The log real timestamps are assumed to be in the server's local time.
#   If your server logs are UTC but Railway is not UTC, we can add a TZ offset later.

import os
import time
import json
import asyncio
import re
import aiohttp
import discord
from discord import app_commands
from typing import Optional, Tuple, Dict

# =====================
# ENV / CONFIG
# =====================
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # required for time webhook

SUNRISE_MINUTE = int(os.getenv("SUNRISE_MINUTE", str(5 * 60 + 30)))   # 05:30
SUNSET_MINUTE  = int(os.getenv("SUNSET_MINUTE",  str(17 * 60 + 30)))  # 17:30

# REAL SECONDS PER IN-GAME MINUTE (SPM)
DAY_SPM   = float(os.getenv("DAY_SPM", "4.7666667"))
NIGHT_SPM = float(os.getenv("NIGHT_SPM", "4.045"))

TIME_UPDATE_STEP_MINUTES = int(os.getenv("TIME_UPDATE_STEP_MINUTES", "10"))

# Poll GetGameLog for timed lines (this is what makes it accurate now)
AUTO_SYNC_POLL_SECONDS = float(os.getenv("AUTO_SYNC_POLL_SECONDS", "10"))
SYNC_DRIFT_MINUTES     = int(os.getenv("SYNC_DRIFT_MINUTES", "2"))

ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "1430388267446042666"))

STATE_FILE = os.getenv("TIME_STATE_FILE", "/data/time_state.json")

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

# =====================
# INTERNAL STATE
# =====================
_state: Optional[dict] = None
_last_announced_abs_day: Optional[int] = None

_rcon_command = None
_webhook_upsert = None

_last_synced_log_real_epoch: Optional[int] = None  # avoid re-syncing same line repeatedly

# =====================
# STATE FILE HELPERS
# =====================
def _ensure_state_dir():
    d = os.path.dirname(STATE_FILE)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def load_state():
    global _state, _last_synced_log_real_epoch
    _ensure_state_dir()
    if not os.path.exists(STATE_FILE):
        _state = None
        _last_synced_log_real_epoch = None
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            _state = json.load(f)
        # optional field
        _last_synced_log_real_epoch = _state.get("last_synced_log_real_epoch")
        return _state
    except Exception:
        _state = None
        _last_synced_log_real_epoch = None
        return None

def save_state():
    global _state, _last_synced_log_real_epoch
    if _state is None:
        return
    _ensure_state_dir()
    _state["last_synced_log_real_epoch"] = _last_synced_log_real_epoch
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

def _minute_of_day(hour: int, minute: int) -> int:
    return hour * 60 + minute

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
    icon = "☀️" if _is_daytime(minute_of_day) else "🌙"
    color = DAY_COLOR if _is_daytime(minute_of_day) else NIGHT_COLOR

    # "big single line"
    title = f"{icon} | Solunaris Time — Year {year} | Day {day} | {hour:02d}:{minute:02d}"
    return {"title": title, "color": color}

def _seconds_until_next_round_step(minute_of_day: int, seconds_into_minute: float, step: int) -> float:
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

def _shift_epoch_by_ingame_minutes(cur_minute_of_day: int, minute_diff: int) -> float:
    """
    Convert ingame minute difference -> real seconds using per-minute SPM model.
    Positive minute_diff means our clock is behind.
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
# PARSE TIMED LINE FROM GetGameLog
# =====================
# Example line (as seen in your embeds):
#   2026.01.18_16.17.57: Tribe The Crossroads... Day 327, 06:09:07: ...
# or:
#   2026.01.18_16.15.17: Saving world...
#
# We only care about lines that contain: "Day X, HH:MM:SS"
#
_REAL_TS = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})_(\d{2})\.(\d{2})\.(\d{2})")
_INGAME_TS = re.compile(r"Day\s+(\d+),\s*(\d{1,2}):(\d{2}):(\d{2})")

def _parse_real_epoch_from_line(line: str) -> Optional[int]:
    m = _REAL_TS.search(line)
    if not m:
        return None
    y, mo, d, hh, mm, ss = (int(m.group(1)), int(m.group(2)), int(m.group(3)),
                           int(m.group(4)), int(m.group(5)), int(m.group(6)))
    try:
        # assume log timestamp is in server-local time
        return int(time.mktime((y, mo, d, hh, mm, ss, 0, 0, -1)))
    except Exception:
        return None

def parse_latest_timed_line(text: str) -> Optional[Dict]:
    """
    Returns dict:
      { real_epoch, day, hour, minute, second, line }
    Picks the *latest* (last in output) line that contains "Day X, HH:MM:SS".
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = _INGAME_TS.search(ln)
        if not m:
            continue
        day = int(m.group(1))
        hour = int(m.group(2))
        minute = int(m.group(3))
        second = int(m.group(4))
        real_epoch = _parse_real_epoch_from_line(ln)
        return {
            "real_epoch": real_epoch,
            "day": day,
            "hour": hour,
            "minute": minute,
            "second": second,
            "line": ln,
        }
    return None

def _adjust_year_for_day_wrap(cur_year: int, cur_day: int, parsed_day: int) -> int:
    """
    If logs jump across a year boundary, adjust year.
    Using a 180-day heuristic to avoid false flips.
    """
    if parsed_day < cur_day - 180:
        return cur_year + 1
    if parsed_day > cur_day + 180:
        return cur_year - 1
    return cur_year

# =====================
# SYNC APPLY (TIMED LINE)
# =====================
def apply_sync_from_timed_log(parsed: Dict) -> Tuple[bool, str]:
    """
    Sync state using the parsed timed log.
    - Target uses Day + HH:MM (ignoring seconds)
    - If parsed["real_epoch"] exists, we re-anchor epoch to that real timestamp (best accuracy)
    """
    global _state, _last_synced_log_real_epoch

    if not _state:
        return False, "No state set yet (use /settime first)."

    now_calc = _calc_now()
    if not now_calc:
        return False, "Could not calculate current time (state missing)."

    cur_minute_of_day, cur_day, cur_year, _sec_into = now_calc

    parsed_day = int(parsed["day"])
    parsed_hour = int(parsed["hour"])
    parsed_minute = int(parsed["minute"])
    real_epoch = parsed.get("real_epoch")  # may be None

    # Avoid repeating the same exact log anchor forever
    if real_epoch is not None and _last_synced_log_real_epoch == int(real_epoch):
        return False, "Already synced to the latest timed log line."

    # Choose year (logs don't include year)
    target_year = _adjust_year_for_day_wrap(int(cur_year), int(cur_day), int(parsed_day))

    target_minute_of_day = _minute_of_day(parsed_hour, parsed_minute)

    # Compute drift in minutes between our predicted now and the log's in-game minute
    # (we ignore in-game seconds)
    day_diff = (parsed_day - cur_day)
    # normalize across year boundary roughly
    if day_diff > 180:
        day_diff -= 365
    elif day_diff < -180:
        day_diff += 365

    minute_diff = (day_diff * 1440) + (target_minute_of_day - cur_minute_of_day)

    # clamp huge drift (stale log line)
    if minute_diff > 720:
        minute_diff -= 1440
    elif minute_diff < -720:
        minute_diff += 1440

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        # still record that we "saw" this timed line so we don't keep re-evaluating it
        if real_epoch is not None:
            _last_synced_log_real_epoch = int(real_epoch)
            save_state()
        return False, f"Drift {minute_diff} min < threshold ({SYNC_DRIFT_MINUTES})."

    # BEST: if we have the log's real timestamp, anchor epoch directly to it
    if real_epoch is not None:
        _state["epoch"] = float(real_epoch)
        _state["year"] = int(target_year)
        _state["day"] = int(parsed_day)
        _state["hour"] = int(parsed_hour)
        _state["minute"] = int(parsed_minute)
        _last_synced_log_real_epoch = int(real_epoch)
        save_state()
        return True, f"Synced using timed GetGameLog line (anchored to real timestamp; drift {minute_diff} min)."

    # Fallback: no real timestamp found; shift epoch using SPM model
    shift_seconds = _shift_epoch_by_ingame_minutes(cur_minute_of_day, minute_diff)
    _state["epoch"] = float(_state["epoch"]) - float(shift_seconds)
    _state["year"] = int(target_year)
    _state["day"] = int(parsed_day)
    _state["hour"] = int(parsed_hour)
    _state["minute"] = int(parsed_minute)
    save_state()
    return True, f"Synced using timed GetGameLog line (no real timestamp; drift {minute_diff} min)."

# =====================
# COMMANDS
# =====================
def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int, rcon_command, webhook_upsert):
    """
    Registers:
      /settime Year Day Hour Minute
      /sync  (syncs ONLY from latest timed GetGameLog line)
    """
    global _rcon_command, _webhook_upsert
    _rcon_command = rcon_command
    _webhook_upsert = webhook_upsert

    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(name="settime", guild=guild_obj)
    async def settime_cmd(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
        global _state, _last_synced_log_real_epoch

        # admin locked
        if not any(getattr(r, "id", None) == int(admin_role_id) for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
            await i.response.send_message("❌ Invalid values.", ephemeral=True)
            return

        _state = {
            "epoch": time.time(),  # anchor to "now"
            "year": int(year),
            "day": int(day),
            "hour": int(hour),
            "minute": int(minute),
        }
        _last_synced_log_real_epoch = None
        save_state()
        await i.response.send_message("✅ Time set.", ephemeral=True)

    @tree.command(name="sync", guild=guild_obj)
    async def sync_cmd(i: discord.Interaction):
        await i.response.defer(ephemeral=True)

        # admin locked (same as before)
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
            parsed = parse_latest_timed_line(text)
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

    print("[time_module] ✅ /settime and /sync registered (timed GetGameLog sync)")

# =====================
# LOOP
# =====================
async def run_time_loop(client: discord.Client, rcon_command, webhook_upsert):
    """
    - Updates the time webhook only on round TIME_UPDATE_STEP_MINUTES
    - Announces new day in ANNOUNCE_CHANNEL_ID
    - Auto-syncs from timed GetGameLog lines whenever a new timed line appears
    """
    global _state, _last_announced_abs_day

    _state = load_state()

    last_poll_ts = 0.0

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now_calc = _calc_now()
                if not now_calc:
                    await asyncio.sleep(5)
                    continue

                minute_of_day, day, year, seconds_into_minute = now_calc

                # --- Auto-sync from timed logs (poll GetGameLog) ---
                if rcon_command is not None and (time.time() - last_poll_ts) >= AUTO_SYNC_POLL_SECONDS:
                    last_poll_ts = time.time()
                    try:
                        text = await rcon_command("GetGameLog", timeout=12.0)
                        parsed = parse_latest_timed_line(text)
                        if parsed:
                            changed, msg = apply_sync_from_timed_log(parsed)
                            if changed:
                                print(f"[time_module] Auto-sync: {msg}")
                                # recalc after sync
                                now_calc = _calc_now()
                                if now_calc:
                                    minute_of_day, day, year, seconds_into_minute = now_calc
                        # if no timed line, do nothing (no spam)
                    except Exception as e:
                        print(f"[time_module] Auto-sync error: {e}")

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