import os
import time
import json
import asyncio
import re
import aiohttp
import discord
from discord import app_commands
from datetime import datetime, timezone

# =====================
# ENV / CONFIG
# =====================
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # required for time webhook

# ASA day-night boundaries
SUNRISE_MINUTE = int(os.getenv("SUNRISE_MINUTE", str(5 * 60 + 30)))  # 05:30
SUNSET_MINUTE  = int(os.getenv("SUNSET_MINUTE",  str(17 * 60 + 30))) # 17:30

# REAL SECONDS PER IN-GAME MINUTE (base)
DAY_SPM_BASE   = float(os.getenv("DAY_SPM", "4.7666667"))
NIGHT_SPM_BASE = float(os.getenv("NIGHT_SPM", "4.045"))

# Behaviour
TIME_UPDATE_STEP_MINUTES = int(os.getenv("TIME_UPDATE_STEP_MINUTES", "10"))  # update webhook on round step
AUTO_SYNC_POLL_SECONDS   = float(os.getenv("AUTO_SYNC_POLL_SECONDS", "10"))  # poll GetGameLog this often
SYNC_DRIFT_MINUTES       = int(os.getenv("SYNC_DRIFT_MINUTES", "2"))         # only correct if >= X minutes drift

# Optional: auto-tune a scale factor from timed logs (helps long-term drift)
AUTO_TUNE_SPM = os.getenv("AUTO_TUNE_SPM", "true").lower() in ("1", "true", "yes", "y")
SPM_TUNE_ALPHA = float(os.getenv("SPM_TUNE_ALPHA", "0.05"))  # EMA smoothing
MIN_TUNE_SAMPLE_MINUTES = float(os.getenv("MIN_TUNE_SAMPLE_MINUTES", "3"))  # ignore tiny deltas

# Daily announce
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "1430388267446042666"))

# State file (Railway volume path)
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

def _spm_scale() -> float:
    if not _state:
        return 1.0
    try:
        return float(_state.get("spm_scale", 1.0))
    except Exception:
        return 1.0

def _spm_for_minute(minute_of_day: int) -> float:
    scale = _spm_scale()
    base = DAY_SPM_BASE if _is_daytime(minute_of_day) else NIGHT_SPM_BASE
    return base * scale

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

    We anchor to the *start of the in-game minute* at epoch=_state["epoch"].
    """
    if not _state:
        return None

    try:
        epoch = float(_state["epoch"])
        year = int(_state["year"])
        day = int(_state["day"])
        hour = int(_state["hour"])
        minute = int(_state["minute"])
    except Exception:
        return None

    elapsed = float(time.time() - epoch)
    minute_of_day = hour * 60 + minute

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

    # Big single-line (your preferred look)
    title = f"{icon} | Solunaris Time — Year {year} | Day {day} | {hour:02d}:{minute:02d}"
    return {"title": title, "color": color}

def _seconds_until_next_round_step(minute_of_day: int, seconds_into_minute: float, step: int) -> float:
    """
    Sleep until next ingame minute boundary divisible by 'step'
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

# =====================
# PARSE TIMED GAMELOG LINE (Minute precision)
# Example: "Day 326, 03:36:08: ..."
# We IGNORE seconds for the time system, so we only take Day + HH:MM.
# =====================
_DAY_HHMMSS = re.compile(r"Day\s+(\d+),\s*(\d{1,2}):(\d{2}):(\d{2})")

# Real time formats commonly seen in ASA logs:
# [YYYY.MM.DD-HH.MM.SS:ms]
_REAL_A = re.compile(r"\[(\d{4}\.\d{2}\.\d{2})-(\d{2}\.\d{2}\.\d{2})")
# YYYY.MMDD_HH.MM.SS
_REAL_B = re.compile(r"(\d{4})\.(\d{2})(\d{2})[_-](\d{2})\.(\d{2})\.(\d{2})")
# YYYY.MM.DD_HH.MM.SS
_REAL_C = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})[_-](\d{2})\.(\d{2})\.(\d{2})")

def parse_latest_timed_gamelog_minute(text: str) -> dict | None:
    """
    Extract the most recent in-game Day + HH:MM from GetGameLog output.
    Ignores seconds on purpose.
    """
    if not text:
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    for ln in reversed(lines):
        m = DAY_TIME_RE.search(ln)
        if not m:
            continue

        return {
            "day": int(m.group("day")),
            "hour": int(m.group("hour")),
            "minute": int(m.group("minute")),
            "source_line": ln,
        }

    return None

def parse_latest_timed_gamelog_minute(text: str):
    """
    Finds newest line containing Day + HH:MM:SS.
    Returns dict:
      day, hour, minute, real_epoch, raw
    Seconds are ignored for the time system.
    """
    if not text:
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        dm = _DAY_HHMMSS.search(ln)
        if not dm:
            continue

        day = int(dm.group(1))
        hour = int(dm.group(2))
        minute = int(dm.group(3))
        # seconds = int(dm.group(4))  # ignored intentionally

        real_epoch = _parse_real_epoch_from_line(ln)

        return {
            "day": day,
            "hour": hour,
            "minute": minute,
            "real_epoch": real_epoch,
            "raw": ln,
        }
    return None

def _minute_of_day(hour: int, minute: int) -> int:
    return hour * 60 + minute

def _wrap_day_diff(d: int) -> int:
    if d > 180:
        d -= 365
    elif d < -180:
        d += 365
    return d

def _ingame_minutes_between(a_day, a_h, a_m, b_day, b_h, b_m) -> int:
    """
    b - a in in-game minutes (can be negative), with day wrap.
    """
    a_total = a_day * 1440 + a_h * 60 + a_m
    b_total = b_day * 1440 + b_h * 60 + b_m
    delta = b_total - a_total
    # wrap across year boundary roughly
    if delta > (365 * 1440) / 2:
        delta -= 365 * 1440
    elif delta < -(365 * 1440) / 2:
        delta += 365 * 1440
    return int(delta)

def _apply_spm_tune_from_last_sync(new_sync: dict):
    """
    Optional: tune spm_scale from successive timed logs using minute precision.
    This helps long-term drift even if multipliers aren't perfect.
    """
    if not AUTO_TUNE_SPM or not _state:
        return

    last = _state.get("last_timed_sync")
    if not last:
        return

    try:
        a_real = float(last.get("real_epoch") or 0.0)
        b_real = float(new_sync.get("real_epoch") or 0.0)
        if not a_real or not b_real:
            return
        real_delta = b_real - a_real
        if real_delta <= 0:
            return

        ingame_min = _ingame_minutes_between(
            int(last["day"]), int(last["hour"]), int(last["minute"]),
            int(new_sync["day"]), int(new_sync["hour"]), int(new_sync["minute"]),
        )
        if ingame_min <= 0:
            return

        if ingame_min < MIN_TUNE_SAMPLE_MINUTES or ingame_min > 300:
            return

        observed_spm = real_delta / float(ingame_min)

        mid_mod = _minute_of_day(int(new_sync["hour"]), int(new_sync["minute"]))
        model_spm = _spm_for_minute(mid_mod)
        if model_spm <= 0:
            return

        ratio = observed_spm / model_spm
        ratio = max(0.80, min(1.20, ratio))

        cur_scale = _spm_scale()
        new_scale = (1.0 - SPM_TUNE_ALPHA) * cur_scale + (SPM_TUNE_ALPHA) * (cur_scale * ratio)
        _state["spm_scale"] = float(new_scale)

    except Exception:
        return 
            
def apply_sync_from_timed_log_minute(parsed: dict, year_hint: int | None = None) -> tuple[bool, str]:
    """
    Sync to the latest timed GetGameLog line using Day + HH:MM only.
    Uses real_epoch if present to anchor accurately.

    We anchor to the START of the minute at _state["epoch"].
    """
    global _state

    if not _state:
        return False, "No state set yet (use /settime first)."

    d = int(parsed["day"])
    h = int(parsed["hour"])
    m = int(parsed["minute"])
    real_epoch = parsed.get("real_epoch")  # may be None

    now_calc = _calc_now()
    if not now_calc:
        return False, "Could not calculate current time (state missing/corrupt)."

    cur_minute_of_day, cur_day, cur_year, _sec_into = now_calc
    target_minute_of_day = _minute_of_day(h, m)

    day_diff = _wrap_day_diff(d - cur_day)
    minute_diff = (day_diff * 1440) + (target_minute_of_day - cur_minute_of_day)

    # clamp huge drift to avoid wild corrections if log is stale
    if minute_diff > 720:
        minute_diff -= 1440
    elif minute_diff < -720:
        minute_diff += 1440

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        # record anyway if newer
        _state["last_timed_sync"] = {
            "day": d, "hour": h, "minute": m,
            "real_epoch": real_epoch,
            "raw": parsed.get("raw", "")
        }
        save_state()
        return False, f"Drift {minute_diff} min < threshold ({SYNC_DRIFT_MINUTES})."

    # If we have real time, anchor epoch to the start of that minute.
    # Since we're ignoring in-game seconds, we anchor to the minute boundary.
    if real_epoch:
        # Treat the log timestamp as belonging to that minute; anchor to start of minute.
        # This is minute-precision but still very accurate (worst-case <= ~1 in-game minute).
        minute_start_epoch = float(real_epoch)

        _apply_spm_tune_from_last_sync(parsed)

        _state["epoch"] = float(minute_start_epoch)
        _state["day"] = int(d)
        _state["hour"] = int(h)
        _state["minute"] = int(m)
        if year_hint is not None:
            _state["year"] = int(year_hint)

        _state["last_timed_sync"] = {
            "day": d, "hour": h, "minute": m,
            "real_epoch": real_epoch,
            "raw": parsed.get("raw", "")
        }
        _state["last_timed_real_epoch"] = float(real_epoch)
        _state["last_sync_sig"] = f"{d}|{h:02d}:{m:02d}|{int(real_epoch)}"
        save_state()
        return True, f"Synced to timed GameLog line (minute precision, real-time anchored). Drift was {minute_diff} min."

    # Fallback if real time missing: shift epoch by minute_diff using model
    shift_seconds = 0.0
    mm = cur_minute_of_day

    if minute_diff > 0:
        for _ in range(minute_diff):
            shift_seconds += _spm_for_minute(mm)
            mm, _, _ = _advance_one_minute(mm, 1, 1)
    else:
        for _ in range(abs(minute_diff)):
            prev = (mm - 1) % 1440
            shift_seconds += _spm_for_minute(prev)
            mm = prev
        shift_seconds = -shift_seconds

    _state["epoch"] = float(_state["epoch"]) - float(shift_seconds)
    _state["day"] = int(d)
    _state["hour"] = int(h)
    _state["minute"] = int(m)

    _state["last_timed_sync"] = {
        "day": d, "hour": h, "minute": m,
        "real_epoch": real_epoch,
        "raw": parsed.get("raw", "")
    }
    _state["last_sync_sig"] = f"{d}|{h:02d}:{m:02d}|no_real"
    save_state()
    return True, f"Synced to timed GameLog line (minute precision, no real timestamp). Drift was {minute_diff} min."

# =====================
# COMMANDS
# =====================
def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int, rcon_command, webhook_upsert):
    """
    /settime year day hour minute
    /sync   -> NEW: sync to latest timed GetGameLog line (Day X, HH:MM:SS) using Day+HH:MM only
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

        prev_scale = float((_state or {}).get("spm_scale", 1.0))

        _state = {
            "epoch": time.time(),
            "year": int(year),
            "day": int(day),
            "hour": int(hour),
            "minute": int(minute),
            "spm_scale": prev_scale,
            "last_sync_sig": None,
            "last_timed_real_epoch": None,
            "last_timed_sync": None,
        }
        save_state()
        await i.response.send_message("✅ Time set.", ephemeral=True)

    @tree.command(name="sync", guild=guild_obj)
    async def sync_cmd(i: discord.Interaction):
        """
        NEW /sync:
          - Fetch GetGameLog once
          - Find newest 'Day X, HH:MM:SS' line
          - Sync to Day + HH:MM (ignore seconds)
          - Use real timestamp if present for better accuracy
        """
        await i.response.defer(ephemeral=True)

        if _rcon_command is None:
            await i.followup.send("❌ RCON not available to time module.", ephemeral=True)
            return

        if not _state:
            await i.followup.send("❌ Time not set yet. Use /settime first.", ephemeral=True)
            return

        try:
            text = await _rcon_command("GetGameLog", timeout=10.0)
            parsed = parse_latest_timed_gamelog_minute(text)
            if not parsed:
                await i.followup.send("❌ No timed line found in GetGameLog (no 'Day X, HH:MM:SS').", ephemeral=True)
                return

            changed, msg = apply_sync_from_timed_log_minute(parsed, year_hint=int(_state.get("year", 1)))

            if _webhook_upsert is not None:
                now_calc = _calc_now()
                if now_calc:
                    mo, dd, yy, _ = now_calc
                    await _webhook_upsert("time", _build_time_embed(mo, dd, yy))

            await i.followup.send(("✅ " if changed else "ℹ️ ") + msg, ephemeral=True)

        except Exception as e:
            await i.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

    print("[time_module] ✅ /settime and /sync registered (minute-precision timed GameLog sync)")

# =====================
# LOOP
# =====================
async def run_time_loop(client: discord.Client, rcon_command, webhook_upsert):
    """
    - Updates the time webhook on round step minutes
    - Announces new day in ANNOUNCE_CHANNEL_ID
    - Auto-syncs whenever a NEW timed GetGameLog line appears (minute precision)
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

                # ---- Auto-sync frequently using newest timed GameLog line ----
                if rcon_command is not None and (time.time() - last_poll_ts) >= AUTO_SYNC_POLL_SECONDS:
                    last_poll_ts = time.time()
                    try:
                        text = await rcon_command("GetGameLog", timeout=10.0)
                        parsed = parse_latest_timed_gamelog_minute(text)
                        if parsed:
                            real_epoch = parsed.get("real_epoch")
                            sig = f"{parsed['day']}|{int(parsed['hour']):02d}:{int(parsed['minute']):02d}|{int(real_epoch) if real_epoch else 'no_real'}"

                            # Only sync when we see a NEWER timed line (prevents spam / resync loops)
                            last_sig = (_state or {}).get("last_sync_sig")
                            last_real = (_state or {}).get("last_timed_real_epoch")

                            is_new = (sig != last_sig)
                            if real_epoch and last_real:
                                try:
                                    is_new = float(real_epoch) > float(last_real)
                                except Exception:
                                    pass

                            if is_new:
                                changed, msg = apply_sync_from_timed_log_minute(parsed, year_hint=int((_state or {}).get("year", 1)))
                                if changed:
                                    print(f"[time_module] Auto-sync: {msg}")

                                # Recalc after sync
                                now_calc = _calc_now()
                                if now_calc:
                                    minute_of_day, day, year, seconds_into_minute = now_calc

                    except Exception as e:
                        print(f"[time_module] Auto-sync error: {e}")

                # ---- Only update webhook on round step ----
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

                sleep_for = _seconds_until_next_round_step(minute_of_day, seconds_into_minute, TIME_UPDATE_STEP_MINUTES)
                await asyncio.sleep(sleep_for)

            except Exception as e:
                print(f"[time_module] loop error: {e}")
                await asyncio.sleep(5)