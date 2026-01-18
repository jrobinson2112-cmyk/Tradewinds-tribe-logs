import os
import time
import json
import asyncio
import re
from datetime import datetime, timezone
from typing import Optional, Tuple

import aiohttp
import discord
from discord import app_commands

# =====================
# ENV / CONFIG
# =====================
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # required for time webhook

# Your ASA day-night boundaries (default ASA)
SUNRISE_MINUTE = int(os.getenv("SUNRISE_MINUTE", str(5 * 60 + 30)))  # 05:30
SUNSET_MINUTE  = int(os.getenv("SUNSET_MINUTE",  str(17 * 60 + 30))) # 17:30

# REAL SECONDS PER IN-GAME MINUTE
DAY_SPM   = float(os.getenv("DAY_SPM", "4.7666667"))
NIGHT_SPM = float(os.getenv("NIGHT_SPM", "4.045"))

# Behaviour
TIME_UPDATE_STEP_MINUTES = int(os.getenv("TIME_UPDATE_STEP_MINUTES", "10"))  # update webhook on round step
SYNC_DRIFT_MINUTES       = int(os.getenv("SYNC_DRIFT_MINUTES", "2"))         # only correct if >= 2 minutes drift

# Auto-sync from DISCORD gamelog embeds
GAMELOGS_CHANNEL_ID      = int(os.getenv("GAMELOGS_CHANNEL_ID", "1462433999766028427"))
AUTO_SYNC_FROM_DISCORD   = os.getenv("AUTO_SYNC_FROM_DISCORD", "1").lower() in ("1", "true", "yes", "on")
AUTO_SYNC_POLL_SECONDS   = float(os.getenv("AUTO_SYNC_POLL_SECONDS", "15"))  # how often to scan channel
AUTO_SYNC_SCAN_LIMIT     = int(os.getenv("AUTO_SYNC_SCAN_LIMIT", "50"))      # how many messages to scan back

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

# used by /sync command + loop
_rcon_command = None  # unused now, but kept for signature compatibility
_webhook_upsert = None

_last_discord_sync_marker: Optional[str] = None  # prevents resyncing same embed repeatedly

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

    title = f"{icon} | Solunaris Time | Year {year} | Day {day} | {hour:02d}:{minute:02d}"
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

def _minute_of_day(hour: int, minute: int) -> int:
    return hour * 60 + minute

def _wrap_day_diff(d: int) -> int:
    if d > 180:
        d -= 365
    elif d < -180:
        d += 365
    return d

# =====================
# DISCORD EMBED PARSING
# =====================
# Matches: "Day 327, 15:45:59:" anywhere in embed description
_INGAME_TIMED = re.compile(r"Day\s+(\d+),\s*(\d{1,2}):(\d{2}):(\d{2})")

# Matches: "2026.01.18_17.03.38" anywhere in line (real timestamp inside logs)
_REAL_TS = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})_(\d{2})\.(\d{2})\.(\d{2})")

def _parse_real_epoch_from_line(line: str) -> Optional[float]:
    m = _REAL_TS.search(line or "")
    if not m:
        return None
    try:
        y, mo, d, hh, mm, ss = map(int, m.groups())
        # Treat as UTC first. We'll sanity-check before using.
        dt = datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None

def _parse_timed_ingame_from_text(text: str) -> Optional[Tuple[int, int, int]]:
    """
    Returns (day, hour, minute) from the first/last timed line found.
    Ignores seconds on purpose.
    """
    if not text:
        return None
    # We want the MOST RECENT line, so scan from bottom.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = _INGAME_TIMED.search(ln)
        if not m:
            continue
        day = int(m.group(1))
        hour = int(m.group(2))
        minute = int(m.group(3))
        return day, hour, minute
    return None

async def _fetch_latest_timed_from_gamelogs_channel(client: discord.Client):
    """
    Scans recent messages in GAMELOGS_CHANNEL_ID and returns:
      (marker, parsed_day, parsed_hour, parsed_minute, best_epoch)
    marker prevents repeated re-sync on same message.
    """
    ch = client.get_channel(GAMELOGS_CHANNEL_ID)
    if ch is None:
        try:
            ch = await client.fetch_channel(GAMELOGS_CHANNEL_ID)
        except Exception:
            return None

    # Scan recent messages newest -> oldest
    async for msg in ch.history(limit=AUTO_SYNC_SCAN_LIMIT):
        # Check embeds first
        if msg.embeds:
            for emb in msg.embeds:
                desc = getattr(emb, "description", None) or ""
                parsed = _parse_timed_ingame_from_text(desc)
                if not parsed:
                    continue

                day, hour, minute = parsed

                # Try to find a real timestamp in ANY line inside desc
                best_epoch = None
                for ln in desc.splitlines():
                    ep = _parse_real_epoch_from_line(ln)
                    if ep is not None:
                        best_epoch = ep
                        break

                # If the parsed epoch seems wildly off (timezone mismatch), ignore it
                now_utc = datetime.now(timezone.utc).timestamp()
                if best_epoch is not None and abs(now_utc - best_epoch) > (6 * 3600):
                    best_epoch = None

                # Fallback: use the Discord message timestamp (safe)
                if best_epoch is None:
                    try:
                        best_epoch = msg.created_at.replace(tzinfo=timezone.utc).timestamp()
                    except Exception:
                        best_epoch = time.time()

                marker = f"{msg.id}:{day}:{hour:02d}{minute:02d}"
                return marker, day, hour, minute, float(best_epoch)

    return None

# =====================
# SYNC APPLY (from Discord embeds)
# =====================
def apply_sync_from_discord_timed_log(parsed_day: int, parsed_hour: int, parsed_minute: int, anchor_epoch: float) -> tuple[bool, str]:
    """
    Sets the clock anchor to the parsed Day/HH:MM at a known real epoch (from log/Discord message).
    Ignores seconds by design.
    """
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

    # clamp huge drift
    if minute_diff > 720:
        minute_diff -= 1440
    elif minute_diff < -720:
        minute_diff += 1440

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {minute_diff} min < threshold ({SYNC_DRIFT_MINUTES})."

    # Infer year rollover if needed (rare, but safe)
    year = int(cur_year)
    if day_diff < -180:
        year += 1
    elif day_diff > 180:
        year = max(1, year - 1)

    _state["epoch"] = float(anchor_epoch)
    _state["year"] = int(year)
    _state["day"] = int(parsed_day)
    _state["hour"] = int(parsed_hour)
    _state["minute"] = int(parsed_minute)

    save_state()
    return True, f"Synced from Discord gamelog embed (drift {minute_diff} min)."

# =====================
# COMMANDS
# =====================
def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int, rcon_command, webhook_upsert):
    """
    Registers:
      /settime Year Day Hour Minute
      /sync  (sync from latest timed gamelog embed in GAMELOGS_CHANNEL_ID)
    """
    global _rcon_command, _webhook_upsert
    _rcon_command = rcon_command  # unused now
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

        if not _state:
            await i.followup.send("❌ Time not set yet. Use /settime first.", ephemeral=True)
            return

        found = await _fetch_latest_timed_from_gamelogs_channel(i.client)
        if not found:
            await i.followup.send("❌ No timed line found in recent gamelog embeds.", ephemeral=True)
            return

        marker, d, h, m, ep = found
        changed, msg = apply_sync_from_discord_timed_log(d, h, m, ep)

        # push webhook right away after manual sync
        if _webhook_upsert is not None:
            now_calc = _calc_now()
            if now_calc:
                mo, dd, yy, _ = now_calc
                await _webhook_upsert("time", _build_time_embed(mo, dd, yy))

        await i.followup.send(("✅ " if changed else "ℹ️ ") + msg, ephemeral=True)

    print("[time_module] ✅ /settime and /sync registered (Discord gamelog embed sync)")

# =====================
# LOOP
# =====================
async def run_time_loop(client: discord.Client, rcon_command, webhook_upsert):
    """
    - Updates the time webhook on round step minutes
    - Announces new day in ANNOUNCE_CHANNEL_ID
    - Auto-syncs by scanning the GAMELOGS_CHANNEL_ID for a timed line:
        "Day X, HH:MM:SS"
      and anchoring clock to that time (ignores seconds).
    """
    global _state, _last_announced_abs_day, _last_discord_sync_marker

    _state = load_state()

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now_calc = _calc_now()
                if not now_calc:
                    await asyncio.sleep(5)
                    continue

                minute_of_day, day, year, seconds_into_minute = now_calc

                # --- Auto sync from Discord gamelog embeds ---
                if AUTO_SYNC_FROM_DISCORD:
                    try:
                        found = await _fetch_latest_timed_from_gamelogs_channel(client)
                        if found:
                            marker, d, h, m, ep = found
                            if marker != _last_discord_sync_marker:
                                changed, msg = apply_sync_from_discord_timed_log(d, h, m, ep)
                                _last_discord_sync_marker = marker
                                if changed:
                                    print(f"[time_module] Auto-sync: {msg}")
                                    # recalc after sync
                                    now_calc = _calc_now()
                                    if now_calc:
                                        minute_of_day, day, year, seconds_into_minute = now_calc
                        # If not found: stay silent (no spam)
                    except Exception as e:
                        print(f"[time_module] Auto-sync error: {e}")

                # --- Update time webhook on round step ---
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

                # Sleep until next update boundary (or poll interval, whichever is sooner)
                sleep_for = _seconds_until_next_round_step(
                    minute_of_day, seconds_into_minute, TIME_UPDATE_STEP_MINUTES
                )
                if AUTO_SYNC_FROM_DISCORD:
                    sleep_for = min(sleep_for, max(2.0, AUTO_SYNC_POLL_SECONDS))

                await asyncio.sleep(sleep_for)

            except Exception as e:
                print(f"[time_module] loop error: {e}")
                await asyncio.sleep(5)