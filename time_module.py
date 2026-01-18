# =========================
# time_module.py (PART 1/2)
# =========================

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

# REAL SECONDS PER IN-GAME MINUTE (SPM)
# Based on your measured timings:
# 10:15 -> 17:15 (420 mins) took 1998s => 4.757142857
# 17:30 -> 01:30 (480 mins) took 2046s => 4.2625
DAY_SPM   = float(os.getenv("DAY_SPM", "4.757142857"))
NIGHT_SPM = float(os.getenv("NIGHT_SPM", "4.2625"))

# Behaviour
TIME_UPDATE_STEP_MINUTES = int(os.getenv("TIME_UPDATE_STEP_MINUTES", "10"))  # update webhook on round step (10 by default)
SYNC_DRIFT_MINUTES       = int(os.getenv("SYNC_DRIFT_MINUTES", "2"))         # only correct if >= 2 minutes drift

# Polling timed logs from GetGameLog
TIMED_LOG_POLL_SECONDS = float(os.getenv("TIMED_LOG_POLL_SECONDS", "10"))    # poll GetGameLog this often to find timed lines
TIMED_LOG_MAX_AGE_SECONDS = int(os.getenv("TIMED_LOG_MAX_AGE_SECONDS", "3600"))  # ignore timed entries older than this (safety)

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
    title = f"{icon} | Solunaris Time — Year {year} | Day {day} | {hour:02d}:{minute:02d}"
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
    for _ in range(minutes_to_boundary - 1):
        m, d, y = _advance_one_minute(m, d, y)
        total += _spm_for_minute(m)

    return max(0.5, total)

def _minute_of_day(hour: int, minute: int) -> int:
    return hour * 60 + minute

def _wrap_day_diff(d: int) -> int:
    # keep within ~half year
    if d > 180:
        d -= 365
    elif d < -180:
        d += 365
    return d

def _infer_year_from_day(current_year: int, current_day: int, parsed_day: int) -> int:
    """
    If parsed_day appears to be from previous/next year vs current_day, adjust year.
    """
    dd = _wrap_day_diff(parsed_day - current_day)
    if dd < -180:
        # parsed day is "ahead" in terms of wrap -> likely next year
        return current_year + 1
    if dd > 180:
        # parsed day is "behind" -> likely previous year
        return current_year - 1
    return current_year

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
# ASA GetGameLog TIMED ENTRY PARSER (multi-line block aware)
# Example:
#   2026.01.18_16.17.57: Tribe ...:
#   Day 327, 06:09:07: ...
# =====================
REAL_TS_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})_(\d{2})\.(\d{2})\.(\d{2})")
INGAME_RE  = re.compile(r"Day\s+(\d+),\s*(\d{2}):(\d{2}):\d{2}")

def parse_timed_entry_from_gamelog(text: str):
    """
    Returns MOST RECENT block containing both:
      - real timestamp: YYYY.MM.DD_HH.MM.SS
      - ingame timestamp: Day X, HH:MM:SS
    Returns dict:
      { real_epoch, day, hour, minute }
    """
    if not text:
        return None

    blocks = []
    current = []

    for line in text.splitlines():
        if REAL_TS_RE.search(line):
            if current:
                blocks.append("\n".join(current))
                current = []
        current.append(line)

    if current:
        blocks.append("\n".join(current))

    for block in reversed(blocks):
        rt = REAL_TS_RE.search(block)
        ig = INGAME_RE.search(block)
        if not rt or not ig:
            continue

        y, mo, d, hh, mm, ss = map(int, rt.groups())
        day, ih, im = map(int, ig.groups())

        try:
            real_epoch = time.mktime((y, mo, d, hh, mm, ss, 0, 0, -1))
        except Exception:
            real_epoch = time.time()

        return {"real_epoch": float(real_epoch), "day": int(day), "hour": int(ih), "minute": int(im)}

    return None
    
# =========================
# time_module.py (PART 2/2)
# =========================

def apply_sync_from_timed_log_minute(parsed: dict) -> tuple[bool, str]:
    """
    Sync the clock using a parsed timed GetGameLog entry.
    Uses the REAL timestamp from the log to set the anchor epoch, improving accuracy.
    Ignores in-game seconds (we already parsed HH:MM).
    """
    global _state

    if not _state:
        return False, "No state set yet (use /settime first)."

    if not parsed:
        return False, "No timed line found in GetGameLog."

    now_calc = _calc_now()
    if not now_calc:
        return False, "Could not calculate current time."

    cur_minute_of_day, cur_day, cur_year, _sec_into = now_calc

    parsed_day = int(parsed["day"])
    parsed_hour = int(parsed["hour"])
    parsed_minute = int(parsed["minute"])
    parsed_real_epoch = float(parsed.get("real_epoch", time.time()))

    # safety: ignore very old log timestamps (prevents snapping to stale lines)
    age = time.time() - parsed_real_epoch
    if age > TIMED_LOG_MAX_AGE_SECONDS:
        return False, f"Ignored timed log (too old: {int(age)}s)."

    # derive a sensible year for the parsed day
    inferred_year = _infer_year_from_day(cur_year, cur_day, parsed_day)

    target_minute_of_day = _minute_of_day(parsed_hour, parsed_minute)
    day_diff = _wrap_day_diff(parsed_day - cur_day)
    minute_diff = (day_diff * 1440) + (target_minute_of_day - cur_minute_of_day)

    # clamp huge drift
    if minute_diff > 720:
        minute_diff -= 1440
    elif minute_diff < -720:
        minute_diff += 1440

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        # still update "last seen log" so we don't reprocess it
        _state["last_timed_log_real_epoch"] = parsed_real_epoch
        save_state()
        return False, f"Drift {minute_diff} min < threshold ({SYNC_DRIFT_MINUTES})."

    # BEST anchor: use the real timestamp of the log entry as the epoch anchor
    # That means: at epoch=parsed_real_epoch, the in-game display is parsed Day HH:MM
    _state["epoch"] = float(parsed_real_epoch)
    _state["year"] = int(inferred_year)
    _state["day"] = int(parsed_day)
    _state["hour"] = int(parsed_hour)
    _state["minute"] = int(parsed_minute)

    _state["last_timed_log_real_epoch"] = parsed_real_epoch
    save_state()
    return True, f"Synced to timed GetGameLog entry (drift {minute_diff} min, age {int(age)}s)."

# =====================
# COMMANDS
# =====================
def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int, rcon_command, webhook_upsert):
    """
    Registers:
      /settime Year Day Hour Minute
      /sync  (syncs ONLY from latest timed GetGameLog entry)
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
            "last_timed_log_real_epoch": 0.0,
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
            text = await _rcon_command("GetGameLog", timeout=12.0)
            parsed = parse_timed_entry_from_gamelog(text)
            if not parsed:
                await i.followup.send("❌ No timed line found in GetGameLog (no 'Day X, HH:MM:SS' in recent output).", ephemeral=True)
                return

            changed, msg = apply_sync_from_timed_log_minute(parsed)

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
    - Updates the time webhook on round step (default every 10 in-game minutes)
    - Announces new day in ANNOUNCE_CHANNEL_ID
    - Polls GetGameLog for timed lines and auto-syncs when a NEW timed log appears
    """
    global _state, _last_announced_abs_day

    _state = load_state()

    last_timed_poll = 0.0

    # Ensure key exists
    if _state and "last_timed_log_real_epoch" not in _state:
        _state["last_timed_log_real_epoch"] = 0.0
        save_state()

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now_calc = _calc_now()
                if not now_calc:
                    await asyncio.sleep(5)
                    continue

                minute_of_day, day, year, seconds_into_minute = now_calc

                # ---- Auto-sync from timed GetGameLog lines ----
                if rcon_command is not None and (time.time() - last_timed_poll) >= TIMED_LOG_POLL_SECONDS:
                    last_timed_poll = time.time()
                    try:
                        text = await rcon_command("GetGameLog", timeout=12.0)
                        parsed = parse_timed_entry_from_gamelog(text)

                        if parsed:
                            last_seen = float(_state.get("last_timed_log_real_epoch", 0.0)) if _state else 0.0
                            # Only sync if it's a NEW timed entry (by real timestamp)
                            if float(parsed["real_epoch"]) > (last_seen + 0.5):
                                changed, msg = apply_sync_from_timed_log_minute(parsed)
                                if changed:
                                    print(f"[time_module] Auto-sync: {msg}")
                                # refresh calc after sync
                                now_calc = _calc_now()
                                if now_calc:
                                    minute_of_day, day, year, seconds_into_minute = now_calc
                        else:
                            # keep this quiet: no spam, only print occasionally if you want
                            pass
                    except Exception as e:
                        print(f"[time_module] Auto-sync error: {e}")

                # ---- Update webhook only on round step ----
                if (minute_of_day % TIME_UPDATE_STEP_MINUTES) == 0:
                    embed = _build_time_embed(minute_of_day, day, year)
                    await webhook_upsert(session, WEBHOOK_URL, "time", embed)

                    # ---- New day announce ----
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