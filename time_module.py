# time_module.py
# Solunaris Time system (webhook embed updater + Discord gamelog embed sync)
#
# Updates in this version (per your request):
# ✅ Auto rollover years (365-day years): Day 366 -> Year+1 Day 1 (display)
# ✅ Option A display: ALL on one line in the EMBED TITLE (larger look)
# ✅ Sends a message to a channel at the start of each NEW DAY (once per day)
# ✅ Keeps compatibility: get_time_state() still returns {"year","day","hour","minute"} for other modules
#
# Env vars:
#   TIME_STATE_DIR=/data
#   TIME_STATE_FILE=/data/time_state.json
#   TIME_UPDATE_SECONDS=30
#   TIME_GAMELOGS_CHANNEL_ID=1462433999766028427
#   TIME_SYNC_SCAN_LIMIT=50
#   TIME_RATE_MIN=0.05
#   TIME_RATE_MAX=20.0
#   TIME_RATE_SMOOTHING=0.2
#   TIME_SHOW_DEBUG=0
#
# NEW:
#   TIME_DAYS_PER_YEAR=365
#   TIME_DAY_ROLLOVER_CHANNEL_ID=1430388267446042666
#   TIME_DAY_ROLLOVER_MESSAGE="🌅 A New Day in Solunaris: Year {year} • Day {day}"
#
# Notes:
# - Internally we track ABSOLUTE day from game logs (abs_day).
# - Public API exposes "day" as DAY-OF-YEAR (1..365) and "year" computed automatically.

from __future__ import annotations

import os
import re
import json
import time
import math
import asyncio
from typing import Optional, Tuple, Dict, Any, List

import discord
from discord import app_commands

# =====================
# CONFIG
# =====================
DATA_DIR = os.getenv("TIME_STATE_DIR", "/data")
STATE_FILE = os.getenv("TIME_STATE_FILE", os.path.join(DATA_DIR, "time_state.json"))

UPDATE_SECONDS = int(os.getenv("TIME_UPDATE_SECONDS", "30"))

# Channel containing the "Game Logs (minute)" embeds
TIME_GAMELOGS_CHANNEL_ID = int(os.getenv("TIME_GAMELOGS_CHANNEL_ID", "1462433999766028427"))

SYNC_SCAN_LIMIT = int(os.getenv("TIME_SYNC_SCAN_LIMIT", "50"))

RATE_MIN = float(os.getenv("TIME_RATE_MIN", "0.05"))
RATE_MAX = float(os.getenv("TIME_RATE_MAX", "20.0"))
RATE_SMOOTHING = float(os.getenv("TIME_RATE_SMOOTHING", "0.2"))

SHOW_DEBUG = os.getenv("TIME_SHOW_DEBUG", "0").lower() in ("1", "true", "yes", "on")

# Year rollover rules
DAYS_PER_YEAR = int(os.getenv("TIME_DAYS_PER_YEAR", "365"))

# Daily rollover announcement channel + message
DAY_ROLLOVER_CHANNEL_ID = int(os.getenv("TIME_DAY_ROLLOVER_CHANNEL_ID", "1430388267446042666"))
DAY_ROLLOVER_MESSAGE = os.getenv(
    "TIME_DAY_ROLLOVER_MESSAGE",
    "🗓️ New day: Year {year} • Day {day}"
)

# =====================
# STATE (single source of truth)
# =====================
# Public state (Traveler Logs reads this).
# NOTE: "day" here is DAY-OF-YEAR (1..DAYS_PER_YEAR), not absolute.
_TIME_STATE: Dict[str, int] = {
    "year": 1,
    "day": 1,      # day-of-year (1..365)
    "hour": 0,
    "minute": 0,
}

# Internal absolute day (as seen in game logs: "Day 366, ...")
_ABS_DAY: int = 1

# Anchor model for forecasting between syncs:
#   game_minutes_now ~= anchor_game_minutes + (real_minutes_delta * rate_game_per_real_min)
_anchor_real_epoch: Optional[float] = None          # seconds since epoch
_anchor_game_minutes: Optional[float] = None        # minutes
_rate_game_per_real_min: float = 1.0                # estimated "game minutes per real minute"
_last_sync_real_epoch: Optional[float] = None       # last synced real time (from logs)
_last_sync_game_minutes: Optional[float] = None     # last synced in-game minute count
_last_timed_line_fingerprint: Optional[str] = None  # prevents re-syncing the same line repeatedly

# Day rollover announcement guard
_last_announced_abs_day: Optional[int] = None


# =====================
# PUBLIC ACCESSOR (Traveler Logs uses this)
# =====================
def get_time_state() -> dict:
    """
    Public accessor for other modules.
    Returns: {"year","day","hour","minute"} where day=DAY-OF-YEAR.
    """
    return dict(_TIME_STATE)


# =====================
# FILE IO
# =====================
def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _load_state():
    global _anchor_real_epoch, _anchor_game_minutes, _rate_game_per_real_min
    global _last_sync_real_epoch, _last_sync_game_minutes, _last_timed_line_fingerprint
    global _ABS_DAY, _last_announced_abs_day

    try:
        if not os.path.exists(STATE_FILE):
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return

        # public time fields (computed fields)
        ts = data.get("time_state", {})
        if isinstance(ts, dict):
            for k in ("year", "day", "hour", "minute"):
                if k in ts:
                    _TIME_STATE[k] = int(ts[k])

        # internal absolute day (preferred)
        if data.get("abs_day") is not None:
            _ABS_DAY = max(1, int(data["abs_day"]))
        else:
            # fallback: reconstruct from year/day-of-year if present
            y = int(_TIME_STATE.get("year", 1))
            doy = int(_TIME_STATE.get("day", 1))
            _ABS_DAY = max(1, (max(1, y) - 1) * DAYS_PER_YEAR + max(1, doy))

        # anchor fields
        _anchor_real_epoch = data.get("anchor_real_epoch", None)
        _anchor_game_minutes = data.get("anchor_game_minutes", None)
        if data.get("rate_game_per_real_min") is not None:
            _rate_game_per_real_min = float(data["rate_game_per_real_min"])

        _last_sync_real_epoch = data.get("last_sync_real_epoch", None)
        _last_sync_game_minutes = data.get("last_sync_game_minutes", None)
        _last_timed_line_fingerprint = data.get("last_timed_line_fingerprint", None)

        if data.get("last_announced_abs_day") is not None:
            _last_announced_abs_day = int(data["last_announced_abs_day"])

    except Exception as e:
        if SHOW_DEBUG:
            print("[time_module] load_state error:", e)

def _save_state():
    try:
        _ensure_dir(STATE_FILE)
        payload = {
            "time_state": dict(_TIME_STATE),
            "abs_day": int(_ABS_DAY),
            "anchor_real_epoch": _anchor_real_epoch,
            "anchor_game_minutes": _anchor_game_minutes,
            "rate_game_per_real_min": _rate_game_per_real_min,
            "last_sync_real_epoch": _last_sync_real_epoch,
            "last_sync_game_minutes": _last_sync_game_minutes,
            "last_timed_line_fingerprint": _last_timed_line_fingerprint,
            "last_announced_abs_day": _last_announced_abs_day,
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as e:
        if SHOW_DEBUG:
            print("[time_module] save_state error:", e)


# =====================
# TIME HELPERS
# =====================
def _compute_year_and_doy(abs_day: int) -> Tuple[int, int]:
    """
    Convert absolute day (1..infinity) to (year, day_of_year) using DAYS_PER_YEAR.
    """
    abs_day = max(1, int(abs_day))
    year = ((abs_day - 1) // DAYS_PER_YEAR) + 1
    doy = ((abs_day - 1) % DAYS_PER_YEAR) + 1
    return year, doy

def _game_minutes_from_parts(abs_day: int, hour: int, minute: int) -> int:
    # For arithmetic, treat AbsDay N as ((N-1)*1440 + ...)
    day_index = max(1, int(abs_day)) - 1
    return day_index * 1440 + int(hour) * 60 + int(minute)

def _parts_from_game_minutes(game_minutes: float) -> Tuple[int, int, int]:
    gm = int(math.floor(game_minutes))
    if gm < 0:
        gm = 0
    day_index, rem = divmod(gm, 1440)
    hour, minute = divmod(rem, 60)
    abs_day = day_index + 1
    return abs_day, hour, minute

def _set_time_state_from_abs(abs_day: int, hour: int, minute: int):
    """
    Centralized write: update internal abs day + computed public year/day-of-year + hh:mm.
    """
    global _ABS_DAY
    _ABS_DAY = max(1, int(abs_day))
    year, doy = _compute_year_and_doy(_ABS_DAY)
    _TIME_STATE["year"] = int(year)
    _TIME_STATE["day"] = int(doy)
    _TIME_STATE["hour"] = int(hour)
    _TIME_STATE["minute"] = int(minute)

def _format_hhmm(hour: int, minute: int) -> str:
    return f"{int(hour):02d}:{int(minute):02d}"

def _make_time_embed_dict() -> dict:
    """
    Option A: ALL on one line in the embed TITLE (bigger look).
    """
    year = _TIME_STATE["year"]
    day = _TIME_STATE["day"]
    hour = _TIME_STATE["hour"]
    minute = _TIME_STATE["minute"]

    is_day = 6 <= hour < 18

    DAY_COLOR = 0xF1C40F   # yellow
    NIGHT_COLOR = 0x0B1C2D # dark blue
    color = DAY_COLOR if is_day else NIGHT_COLOR
    icon = "☀️" if is_day else "🌙"

    title = f"{icon} Solunaris Time — Year {year} • Day {day} • {hour:02d}:{minute:02d}"

    return {
        "title": title,
        "description": "",   # keep single-line look
        "color": color,
    }


# =====================
# PARSING (Discord embeds -> timed line)
# =====================
TIMED_LINE_RE = re.compile(
    r"(?:^|\b)Day\s+(?P<day>\d+)\s*[, ]\s*(?P<h>\d{1,2})\s*:\s*(?P<m>\d{2})(?:\s*:\s*(?P<s>\d{2}))?",
    re.IGNORECASE,
)

REAL_TS_RE = re.compile(
    r"(?P<Y>\d{4})[.\-](?P<Mo>\d{2})[.\-](?P<Da>\d{2})[ _](?P<h>\d{2})[.:](?P<m>\d{2})[.:](?P<s>\d{2})"
)

def _parse_real_epoch_from_line(line: str) -> Optional[float]:
    m = REAL_TS_RE.search(line)
    if not m:
        return None
    try:
        Y = int(m.group("Y"))
        Mo = int(m.group("Mo"))
        Da = int(m.group("Da"))
        hh = int(m.group("h"))
        mm = int(m.group("m"))
        ss = int(m.group("s"))
        return time.mktime((Y, Mo, Da, hh, mm, ss, 0, 0, -1))
    except Exception:
        return None

def _find_newest_timed_line_in_text(text: str) -> Optional[dict]:
    if not text:
        return None

    matches = list(TIMED_LINE_RE.finditer(text))
    if not matches:
        return None

    last = matches[-1]
    abs_day = int(last.group("day"))
    hh = int(last.group("h"))
    mm = int(last.group("m"))

    real_epoch = _parse_real_epoch_from_line(text[last.start(): last.end()+200]) or _parse_real_epoch_from_line(text)

    snippet = text[max(0, last.start()-40): min(len(text), last.end()+80)].strip()
    fingerprint = f"D{abs_day}-{hh:02d}{mm:02d}-{hash(snippet)}"

    return {
        "abs_day": abs_day,
        "hour": hh,
        "minute": mm,
        "real_epoch": real_epoch,
        "fingerprint": fingerprint,
        "snippet": snippet,
    }

def _extract_text_from_message(msg: discord.Message) -> str:
    parts: List[str] = []
    if msg.content:
        parts.append(msg.content)

    for emb in msg.embeds or []:
        if emb.description:
            parts.append(emb.description)
        for f in getattr(emb, "fields", []) or []:
            if getattr(f, "value", None):
                parts.append(str(f.value))
            if getattr(f, "name", None):
                parts.append(str(f.name))

    return "\n".join(parts)


# =====================
# SYNC + FORECAST
# =====================
def _apply_sync_from_timed(parsed: dict) -> Tuple[bool, str]:
    global _anchor_real_epoch, _anchor_game_minutes
    global _rate_game_per_real_min, _last_sync_real_epoch, _last_sync_game_minutes
    global _last_timed_line_fingerprint

    if not parsed:
        return False, "No parsed timed line."

    fp = parsed.get("fingerprint")
    if fp and _last_timed_line_fingerprint == fp:
        return False, "Timed line already applied."

    abs_day = int(parsed["abs_day"])
    hh = int(parsed["hour"])
    mm = int(parsed["minute"])

    game_minutes = _game_minutes_from_parts(abs_day, hh, mm)

    real_epoch = parsed.get("real_epoch")
    if real_epoch is None:
        real_epoch = time.time()

    # Rate estimation based on previous sync point
    if _last_sync_real_epoch is not None and _last_sync_game_minutes is not None:
        dr = (real_epoch - float(_last_sync_real_epoch)) / 60.0  # real minutes
        dg = float(game_minutes) - float(_last_sync_game_minutes)  # game minutes
        if dr > 0.25:
            new_rate = dg / dr
            new_rate = max(RATE_MIN, min(RATE_MAX, new_rate))
            _rate_game_per_real_min = (1.0 - RATE_SMOOTHING) * _rate_game_per_real_min + RATE_SMOOTHING * new_rate

    _anchor_real_epoch = float(real_epoch)
    _anchor_game_minutes = float(game_minutes)

    _last_sync_real_epoch = float(real_epoch)
    _last_sync_game_minutes = float(game_minutes)
    _last_timed_line_fingerprint = fp

    # Update state (this computes year/day-of-year)
    _set_time_state_from_abs(abs_day, hh, mm)

    _save_state()
    return True, f"Synced to AbsDay {abs_day} {hh:02d}:{mm:02d} (rate={_rate_game_per_real_min:.3f}x)."


def _tick_forecast_now() -> Optional[int]:
    """
    Update state from anchor + current time using estimated rate.
    Returns previous abs day if changed can be detected by caller.
    """
    if _anchor_real_epoch is None or _anchor_game_minutes is None:
        return None

    prev_abs = int(_ABS_DAY)

    now = time.time()
    dr_min = (now - float(_anchor_real_epoch)) / 60.0
    gm_now = float(_anchor_game_minutes) + dr_min * float(_rate_game_per_real_min)

    abs_day, hh, mm = _parts_from_game_minutes(gm_now)
    _set_time_state_from_abs(abs_day, hh, mm)

    return prev_abs


async def _sync_from_discord_gamelogs(client: discord.Client) -> Tuple[bool, str]:
    if not TIME_GAMELOGS_CHANNEL_ID:
        return False, "TIME_GAMELOGS_CHANNEL_ID not set."

    ch = client.get_channel(TIME_GAMELOGS_CHANNEL_ID)
    if ch is None:
        try:
            ch = await client.fetch_channel(TIME_GAMELOGS_CHANNEL_ID)
        except Exception as e:
            return False, f"Could not fetch game logs channel: {e}"

    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return False, "Game logs channel is not a text channel."

    newest_parsed: Optional[dict] = None

    try:
        async for msg in ch.history(limit=SYNC_SCAN_LIMIT, oldest_first=False):
            text = _extract_text_from_message(msg)
            parsed = _find_newest_timed_line_in_text(text)
            if parsed:
                newest_parsed = parsed
                break
    except Exception as e:
        return False, f"History scan failed: {e}"

    if not newest_parsed:
        return False, "No timed line found in recent Discord gamelog embeds."

    ok, info = _apply_sync_from_timed(newest_parsed)
    return ok, info


# =====================
# DAY ROLLOVER ANNOUNCEMENT
# =====================
async def _announce_new_day_if_needed(client: discord.Client, prev_abs_day: Optional[int]):
    """
    If abs day advanced and we haven't announced it yet, post message in DAY_ROLLOVER_CHANNEL_ID.
    """
    global _last_announced_abs_day

    if prev_abs_day is None:
        return

    current_abs = int(_ABS_DAY)
    if current_abs == int(prev_abs_day):
        return

    # Only announce the newest day (guard against duplicate calls)
    if _last_announced_abs_day is not None and int(_last_announced_abs_day) == current_abs:
        return

    # Build message
    year = _TIME_STATE["year"]
    doy = _TIME_STATE["day"]
    msg_text = DAY_ROLLOVER_MESSAGE.format(year=year, day=doy)

    # Send
    try:
        ch = client.get_channel(DAY_ROLLOVER_CHANNEL_ID)
        if ch is None:
            ch = await client.fetch_channel(DAY_ROLLOVER_CHANNEL_ID)

        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            await ch.send(msg_text)
            _last_announced_abs_day = current_abs
            _save_state()
    except Exception as e:
        if SHOW_DEBUG:
            print("[time_module] day rollover announce error:", e)


# =====================
# COMMANDS
# =====================
def _is_admin(interaction: discord.Interaction, admin_role_id: int) -> bool:
    try:
        if interaction.user is None:
            return False
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            return False
        return any(r.id == int(admin_role_id) for r in (member.roles or []))
    except Exception:
        return False


def setup_time_commands(
    tree: app_commands.CommandTree,
    guild_id: int,
    admin_role_id: int,
    rcon_cmd,               # unused here, kept for compatibility
    webhook_upsert,         # callable in main.py
):
    """
    Registers /settime and /sync
    """

    @tree.command(
        name="settime",
        description="Admin: set Solunaris time (Year, Day, HH:MM)",
        guild=discord.Object(id=guild_id),
    )
    @app_commands.describe(
        year="Year number",
        day="Day of year (1-365)",
        hour="Hour (0-23)",
        minute="Minute (0-59)",
    )
    async def settime(
        interaction: discord.Interaction,
        year: int,
        day: int,
        hour: int,
        minute: int,
    ):
        if not _is_admin(interaction, admin_role_id):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        hour = max(0, min(23, int(hour)))
        minute = max(0, min(59, int(minute)))
        year = max(1, int(year))
        day = max(1, min(DAYS_PER_YEAR, int(day)))

        # Convert year/day-of-year to absolute day
        abs_day = (year - 1) * DAYS_PER_YEAR + day

        global _anchor_real_epoch, _anchor_game_minutes, _last_sync_real_epoch, _last_sync_game_minutes, _last_timed_line_fingerprint

        _set_time_state_from_abs(abs_day, hour, minute)

        gm = _game_minutes_from_parts(abs_day, hour, minute)
        now = time.time()
        _anchor_real_epoch = now
        _anchor_game_minutes = float(gm)
        _last_sync_real_epoch = now
        _last_sync_game_minutes = float(gm)
        _last_timed_line_fingerprint = None

        _save_state()

        try:
            await webhook_upsert("time", _make_time_embed_dict())
        except Exception as e:
            if SHOW_DEBUG:
                print("[time_module] webhook_upsert error:", e)

        await interaction.response.send_message(
            f"✅ Set time: Year {year} • Day {day} • {_format_hhmm(hour, minute)}",
            ephemeral=True,
        )


    @tree.command(
        name="sync",
        description="Admin: sync Solunaris time from Discord gamelog embed (Day, HH:MM)",
        guild=discord.Object(id=guild_id),
    )
    async def sync(interaction: discord.Interaction):
        if not _is_admin(interaction, admin_role_id):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        ok, info = await _sync_from_discord_gamelogs(interaction.client)

        if ok:
            try:
                await webhook_upsert("time", _make_time_embed_dict())
            except Exception as e:
                if SHOW_DEBUG:
                    print("[time_module] webhook_upsert error:", e)

            await interaction.followup.send(f"✅ {info}", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ {info}", ephemeral=True)

    print("[time_module] ✅ /settime and /sync registered (Discord gamelog embed sync)")


# =====================
# LOOP
# =====================
async def run_time_loop(client: discord.Client, rcon_cmd, webhook_upsert):
    """
    - Loads persisted state
    - Every UPDATE_SECONDS:
        - forecast time from anchor
        - attempt auto-sync from discord gamelog embeds (quietly)
        - detect day rollover and announce
        - update time webhook
    """
    _ensure_dir(STATE_FILE)
    _load_state()

    # If no anchor, create one from saved time
    global _anchor_real_epoch, _anchor_game_minutes, _last_announced_abs_day

    if _anchor_real_epoch is None or _anchor_game_minutes is None:
        gm = _game_minutes_from_parts(_ABS_DAY, _TIME_STATE["hour"], _TIME_STATE["minute"])
        _anchor_real_epoch = time.time()
        _anchor_game_minutes = float(gm)
        _save_state()

    # Prevent announcing immediately on startup unless the day actually changes later
    if _last_announced_abs_day is None:
        _last_announced_abs_day = int(_ABS_DAY)
        _save_state()

    print("[time_module] ✅ time loop running")

    last_webhook_push = 0.0
    while True:
        try:
            # Forecast
            prev_abs = _tick_forecast_now()

            # Auto-sync (non-fatal)
            ok, info = await _sync_from_discord_gamelogs(client)
            if SHOW_DEBUG:
                print(f"[time_module] Auto-sync: {'OK' if ok else 'NO'} - {info}")

            # Forecast again (stabilize)
            prev_abs_2 = _tick_forecast_now()
            # Use the earlier prev_abs if available, otherwise the second
            prev_for_roll = prev_abs if prev_abs is not None else prev_abs_2

            # Announce day rollover if needed
            await _announce_new_day_if_needed(client, prev_for_roll)

            # Push webhook
            now = time.time()
            if now - last_webhook_push >= max(5, UPDATE_SECONDS - 1):
                try:
                    await webhook_upsert("time", _make_time_embed_dict())
                except Exception as e:
                    if SHOW_DEBUG:
                        print("[time_module] webhook_upsert error:", e)
                last_webhook_push = now

            # Persist
            _save_state()

        except Exception as e:
            print(f"[time_module] loop error: {e}")

        await asyncio.sleep(max(5, UPDATE_SECONDS))