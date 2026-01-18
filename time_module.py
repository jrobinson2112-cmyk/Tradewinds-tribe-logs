import os
import time
import json
import asyncio
import re
import aiohttp
import discord
from discord import app_commands
from datetime import datetime

# =====================
# ENV / CONFIG
# =====================
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # required for time webhook

# ASA day/night boundaries (minutes since midnight)
SUNRISE_MINUTE = int(os.getenv("SUNRISE_MINUTE", str(5 * 60 + 30)))   # 05:30
SUNSET_MINUTE  = int(os.getenv("SUNSET_MINUTE",  str(17 * 60 + 30)))  # 17:30

# REAL SECONDS per IN-GAME MINUTE
DAY_SPM   = float(os.getenv("DAY_SPM", "4.7666667"))
NIGHT_SPM = float(os.getenv("NIGHT_SPM", "4.045"))

# Behaviour
TIME_UPDATE_STEP_MINUTES = int(os.getenv("TIME_UPDATE_STEP_MINUTES", "10"))  # update webhook on round 10 mins
AUTO_SYNC_EVERY_SECONDS  = int(os.getenv("AUTO_SYNC_EVERY_SECONDS", "60"))   # check for timed log lines
SYNC_DRIFT_MINUTES       = int(os.getenv("SYNC_DRIFT_MINUTES", "2"))         # only correct if >= 2 min drift

# /sync retries (so manual sync doesn’t fail between timed events)
SYNC_PULL_ATTEMPTS = int(os.getenv("SYNC_PULL_ATTEMPTS", "6"))               # e.g. 6 attempts
SYNC_PULL_SLEEP    = float(os.getenv("SYNC_PULL_SLEEP", "2.0"))              # e.g. every 2s

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
_state = None
_last_announced_abs_day = None

_rcon_command = None
_webhook_upsert = None

# Cache fed by other modules (e.g. gamelogs_autopost)
_latest_gamelog_text = None
_latest_gamelog_cache_ts = 0.0

def ingest_gamelog_text(text: str):
    """
    Optional: other modules can call this with the raw GetGameLog output they already fetched.
    This makes time sync much more reliable (no “wrong batch” issue).
    """
    global _latest_gamelog_text, _latest_gamelog_cache_ts
    if isinstance(text, str) and text.strip():
        _latest_gamelog_text = text
        _latest_gamelog_cache_ts = time.time()

def _get_cached_gamelog_text(max_age_seconds: float = 120.0) -> str | None:
    if _latest_gamelog_text and (time.time() - _latest_gamelog_cache_ts) <= max_age_seconds:
        return _latest_gamelog_text
    return None

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

def _minute_of_day(hour: int, minute: int) -> int:
    return hour * 60 + minute

def _wrap_day_diff(d: int) -> int:
    if d > 180:
        d -= 365
    elif d < -180:
        d += 365
    return d

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
# PARSING TIMED LINES FROM GetGameLog
# Handles variants:
# "Day 326, 17:32:40:"  or "Day 326,17:32:40" or "Day 326 17:32:40"
# =====================
# Day + time anywhere in line
_DAY_HMS = re.compile(r"Day\s+(\d+)\s*,?\s*(\d{1,2})\s*:\s*(\d{2})\s*:\s*(\d{2})", re.IGNORECASE)

# Real timestamp at start anywhere:
# 2026.01.18_15.24.48
_REAL_TS = re.compile(r"(\d{4}\.\d{2}\.\d{2})_(\d{2}\.\d{2}\.\d{2})")

def _parse_real_epoch_from_line(line: str) -> float | None:
    m = _REAL_TS.search(line or "")
    if not m:
        return None
    ds = m.group(1)  # YYYY.MM.DD
    ts = m.group(2)  # HH.MM.SS
    try:
        dt = datetime.strptime(f"{ds}_{ts}", "%Y.%m.%d_%H.%M.%S")
        return time.mktime(dt.timetuple())
    except Exception:
        return None

def parse_latest_timed_line(text: str) -> dict | None:
    """
    Returns dict:
      {
        "day": int,
        "hour": int,
        "minute": int,
        "second": int,
        "real_epoch": float|None,
        "raw": str
      }
    Picks the newest (last) line that contains Day + HH:MM:SS.
    """
    if not isinstance(text, str) or not text.strip():
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = _DAY_HMS.search(ln)
        if not m:
            continue
        day = int(m.group(1))
        hour = int(m.group(2))
        minute = int(m.group(3))
        second = int(m.group(4))
        real_epoch = _parse_real_epoch_from_line(ln)
        return {
            "day": day,
            "hour": hour,
            "minute": minute,
            "second": second,
            "real_epoch": real_epoch,
            "raw": ln,
        }
    return None
    
# =====================
# SYNC APPLY (minute precision; ignores in-game seconds)
# =====================
def apply_sync_from_timed_log_minute(parsed: dict, year_hint: int | None = None) -> tuple[bool, str]:
    global _state

    if not _state:
        return False, "No state set yet (use /settime first)."

    parsed_day = int(parsed["day"])
    parsed_hour = int(parsed["hour"])
    parsed_minute = int(parsed["minute"])

    now_calc = _calc_now()
    if not now_calc:
        return False, "Could not calculate current time (state missing)."

    cur_minute_of_day, cur_day, cur_year, _sec_into = now_calc

    target_year = int(year_hint) if year_hint is not None else int(cur_year)
    target_minute_of_day = _minute_of_day(parsed_hour, parsed_minute)

    day_diff = _wrap_day_diff(parsed_day - cur_day)
    minute_diff = (day_diff * 1440) + (target_minute_of_day - cur_minute_of_day)

    # stale protection
    if minute_diff > 720:
        minute_diff -= 1440
    elif minute_diff < -720:
        minute_diff += 1440

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {minute_diff} min < threshold ({SYNC_DRIFT_MINUTES})."

    # Best accuracy: anchor to the real timestamp on the log line
    real_epoch = parsed.get("real_epoch", None)
    if isinstance(real_epoch, (int, float)) and real_epoch > 0:
        _state["epoch"] = float(real_epoch)
        _state["year"] = int(target_year)
        _state["day"] = int(parsed_day)
        _state["hour"] = int(parsed_hour)
        _state["minute"] = int(parsed_minute)
        save_state()
        return True, f"Synced to timed GetGameLog line using real timestamp (drift {minute_diff} min)."

    # Fallback: shift epoch by SPM model
    shift_seconds = _shift_epoch_by_ingame_minutes(cur_minute_of_day, minute_diff)
    _state["epoch"] = float(_state["epoch"]) - float(shift_seconds)
    _state["year"] = int(target_year)
    _state["day"] = int(parsed_day)
    _state["hour"] = int(parsed_hour)
    _state["minute"] = int(parsed_minute)
    save_state()
    return True, f"Synced to timed GetGameLog line (no real timestamp; drift {minute_diff} min)."

async def _pull_latest_timed_line_from_rcon(rcon_command) -> dict | None:
    """
    Pulls GetGameLog and returns the latest timed line, or None.
    """
    text = await rcon_command("GetGameLog", timeout=10.0)
    return parse_latest_timed_line(text)

async def _find_timed_line_with_retries(rcon_command) -> dict | None:
    """
    For manual /sync: retry a few times so it doesn't fail just because no timed event happened in that exact batch.
    Also checks cache first (fed by gamelogs_autopost).
    """
    cached = _get_cached_gamelog_text()
    if cached:
        parsed = parse_latest_timed_line(cached)
        if parsed:
            return parsed

    for _ in range(max(1, SYNC_PULL_ATTEMPTS)):
        parsed = await _pull_latest_timed_line_from_rcon(rcon_command)
        if parsed:
            return parsed
        await asyncio.sleep(max(0.2, SYNC_PULL_SLEEP))
    return None

# =====================
# COMMANDS
# =====================
def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int, rcon_command, webhook_upsert):
    """
    /settime Year Day Hour Minute
    /sync (syncs ONLY from timed GetGameLog lines)
    """
    global _rcon_command, _webhook_upsert
    _rcon_command = rcon_command
    _webhook_upsert = webhook_upsert

    guild_obj = discord.Object(id=int(guild_id))

    def _is_admin(interaction: discord.Interaction) -> bool:
        return any(getattr(r, "id", None) == int(admin_role_id) for r in getattr(interaction.user, "roles", []))

    @tree.command(name="settime", guild=guild_obj)
    async def settime_cmd(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
        global _state
        if not _is_admin(i):
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

        if not _is_admin(i):
            await i.followup.send("❌ No permission", ephemeral=True)
            return

        if _rcon_command is None:
            await i.followup.send("❌ RCON not available to time module.", ephemeral=True)
            return

        if not _state:
            await i.followup.send("❌ Time not set yet. Use /settime first.", ephemeral=True)
            return

        try:
            parsed = await _find_timed_line_with_retries(_rcon_command)
            if not parsed:
                await i.followup.send("❌ No timed line found in GetGameLog (no 'Day X, HH:MM:SS' in recent output).", ephemeral=True)
                return

            changed, msg = apply_sync_from_timed_log_minute(parsed)

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
    - Updates time webhook on round step minutes
    - Announces new day
    - Auto-syncs using timed GetGameLog lines (Day X, HH:MM:SS)
      Prefer cached text from gamelogs_autopost if available.
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

                # --- Auto sync ---
                if rcon_command is not None and (time.time() - last_sync_ts) >= AUTO_SYNC_EVERY_SECONDS:
                    try:
                        # Prefer cache (fed by gamelogs_autopost)
                        cached = _get_cached_gamelog_text()
                        parsed = parse_latest_timed_line(cached) if cached else None

                        # If no cache or cache has no timed line, fall back to RCON pull
                        if not parsed:
                            parsed = await _pull_latest_timed_line_from_rcon(rcon_command)

                        if parsed:
                            changed, msg = apply_sync_from_timed_log_minute(parsed)
                            if changed:
                                print(f"[time_module] Auto-sync: {msg}")
                        else:
                            print("[time_module] Auto-sync: No timed line found in GetGameLog.")
                    except Exception as e:
                        print(f"[time_module] Auto-sync error: {e}")

                    last_sync_ts = time.time()

                    # recalc after sync
                    now_calc = _calc_now()
                    if now_calc:
                        minute_of_day, day, year, seconds_into_minute = now_calc

                # --- Webhook update on rounded step minutes ---
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