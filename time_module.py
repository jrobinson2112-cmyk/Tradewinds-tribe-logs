# time_module.py
# Solunaris Time system (webhook embed updater + Discord gamelog embed sync)
#
# Public API used by main.py:
#   setup_time_commands(tree, guild_id, admin_role_id, rcon_cmd, webhook_upsert)
#   run_time_loop(client, rcon_cmd, webhook_upsert)
#   get_time_state()  <-- Traveler logs uses this
#
# Key goals:
# - Single source of truth: _TIME_STATE dict
# - Auto-sync from Discord gamelog embeds that include: "Day 327, 15:45:59: ..."
# - Ignore in-game seconds (we store/display Day + HH:MM)
# - Optional: use real timestamp prefix in the same log line to estimate rate
#
# Env vars:
#   TIME_STATE_DIR=/data
#   TIME_STATE_FILE=/data/time_state.json
#   TIME_UPDATE_SECONDS=30
#   TIME_GAMELOGS_CHANNEL_ID=1462433999766028427   <-- IMPORTANT
#   TIME_SYNC_SCAN_LIMIT=50
#   TIME_RATE_MIN=0.05
#   TIME_RATE_MAX=20.0
#   TIME_RATE_SMOOTHING=0.2
#   TIME_SHOW_DEBUG=0

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

EMBED_COLOR = 0x2F3136

# =====================
# STATE (single source of truth)
# =====================
# This is what Traveler Logs should read.
_TIME_STATE: Dict[str, int] = {
    "year": 1,
    "day": 1,      # in-game day number
    "hour": 0,
    "minute": 0,
}

# Anchor model for forecasting between syncs:
#   game_minutes_now ~= anchor_game_minutes + (real_minutes_delta * rate_game_per_real_min)
_anchor_real_epoch: Optional[float] = None          # seconds since epoch
_anchor_game_minutes: Optional[float] = None        # minutes
_rate_game_per_real_min: float = 1.0                # estimated "game minutes per real minute"
_last_sync_real_epoch: Optional[float] = None       # last synced real time (from logs)
_last_sync_game_minutes: Optional[float] = None     # last synced in-game minute count
_last_timed_line_fingerprint: Optional[str] = None  # prevents re-syncing the same line repeatedly


# =====================
# PUBLIC ACCESSOR (Traveler Logs uses this)
# =====================
def get_time_state() -> dict:
    """
    Public accessor for other modules.
    MUST reflect the same state your time loop updates.
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

    try:
        if not os.path.exists(STATE_FILE):
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # time fields
            ts = data.get("time_state", {})
            if isinstance(ts, dict):
                for k in ("year", "day", "hour", "minute"):
                    if k in ts:
                        _TIME_STATE[k] = int(ts[k])

            # anchor fields
            _anchor_real_epoch = data.get("anchor_real_epoch", None)
            _anchor_game_minutes = data.get("anchor_game_minutes", None)
            if data.get("rate_game_per_real_min") is not None:
                _rate_game_per_real_min = float(data["rate_game_per_real_min"])

            _last_sync_real_epoch = data.get("last_sync_real_epoch", None)
            _last_sync_game_minutes = data.get("last_sync_game_minutes", None)
            _last_timed_line_fingerprint = data.get("last_timed_line_fingerprint", None)
    except Exception as e:
        if SHOW_DEBUG:
            print("[time_module] load_state error:", e)

def _save_state():
    try:
        _ensure_dir(STATE_FILE)
        payload = {
            "time_state": dict(_TIME_STATE),
            "anchor_real_epoch": _anchor_real_epoch,
            "anchor_game_minutes": _anchor_game_minutes,
            "rate_game_per_real_min": _rate_game_per_real_min,
            "last_sync_real_epoch": _last_sync_real_epoch,
            "last_sync_game_minutes": _last_sync_game_minutes,
            "last_timed_line_fingerprint": _last_timed_line_fingerprint,
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as e:
        if SHOW_DEBUG:
            print("[time_module] save_state error:", e)


# =====================
# TIME HELPERS
# =====================
def _game_minutes_from_parts(day: int, hour: int, minute: int) -> int:
    # Day 1 at 00:00 => 0 minutes for day component? We treat "day number" as a label.
    # For arithmetic, treat Day N as ((N-1)*1440 + ...)
    day_index = max(1, int(day)) - 1
    return day_index * 1440 + int(hour) * 60 + int(minute)

def _parts_from_game_minutes(game_minutes: float) -> Tuple[int, int, int]:
    gm = int(math.floor(game_minutes))
    if gm < 0:
        gm = 0
    day_index, rem = divmod(gm, 1440)
    hour, minute = divmod(rem, 60)
    day = day_index + 1
    return day, hour, minute

def _set_time_state(year: Optional[int] = None, day: Optional[int] = None, hour: Optional[int] = None, minute: Optional[int] = None):
    # Centralized write so we never forget to update _TIME_STATE.
    if year is not None:
        _TIME_STATE["year"] = int(year)
    if day is not None:
        _TIME_STATE["day"] = int(day)
    if hour is not None:
        _TIME_STATE["hour"] = int(hour)
    if minute is not None:
        _TIME_STATE["minute"] = int(minute)

def _time_icon(hour: int) -> str:
    # Simple day/night icon
    if 6 <= hour < 18:
        return "☀️"
    return "🌙"

def _format_hhmm(hour: int, minute: int) -> str:
    return f"{int(hour):02d}:{int(minute):02d}"

def _make_time_embed_dict() -> dict:
    year = _TIME_STATE["year"]
    day = _TIME_STATE["day"]
    hour = _TIME_STATE["hour"]
    minute = _TIME_STATE["minute"]
    icon = _time_icon(hour)

    title = f"{icon} | Solunaris Time | Year {year} | Day {day} | {_format_hhmm(hour, minute)}"
    desc = ""

    footer = f"Auto-sync: {'ON' if TIME_GAMELOGS_CHANNEL_ID else 'OFF'} | rate={_rate_game_per_real_min:.3f}x"

    return {
        "title": title,
        "description": desc,
        "color": EMBED_COLOR,
        "footer": {"text": footer},
    }


# =====================
# PARSING (Discord embeds -> timed line)
# =====================
# Example lines you showed:
#   "2026.01.18_17.03.38: Tribe ... Day 327, 15:45:59: Dravenya ..."
# Sometimes might be:
#   "Day 326, 03:36:08: Dravenya froze echo."
#
# We accept both "Day 327, 15:45:59" and "Day 327, 15:45" and "Day 327 15:45:59"
TIMED_LINE_RE = re.compile(
    r"(?:^|\b)Day\s+(?P<day>\d+)\s*[, ]\s*(?P<h>\d{1,2})\s*:\s*(?P<m>\d{2})(?:\s*:\s*(?P<s>\d{2}))?",
    re.IGNORECASE,
)

# Real timestamp prefix variants:
#   "2026.01.18_17.03.38:"
#   "2026-01-18 17:03:38"
REAL_TS_RE = re.compile(
    r"(?P<Y>\d{4})[.\-](?P<Mo>\d{2})[.\-](?P<Da>\d{2})[ _](?P<h>\d{2})[.:](?P<m>\d{2})[.:](?P<s>\d{2})"
)

def _parse_real_epoch_from_line(line: str) -> Optional[float]:
    """
    Parse a real timestamp from the log line if present.
    Returns epoch seconds (localtime-based) for relative drift estimation.
    """
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
        # Treat as localtime (good enough for deltas)
        return time.mktime((Y, Mo, Da, hh, mm, ss, 0, 0, -1))
    except Exception:
        return None

def _find_newest_timed_line_in_text(text: str) -> Optional[dict]:
    """
    Given a blob of text (embed description or field), find the newest timed line in it.
    We pick the LAST occurrence in the text (most recent).
    """
    if not text:
        return None

    matches = list(TIMED_LINE_RE.finditer(text))
    if not matches:
        return None

    last = matches[-1]
    day = int(last.group("day"))
    hh = int(last.group("h"))
    mm = int(last.group("m"))

    # Ignore seconds by design
    real_epoch = _parse_real_epoch_from_line(text[last.start(): last.end()+200]) or _parse_real_epoch_from_line(text)

    # Fingerprint helps avoid re-syncing same line again and again
    snippet = text[max(0, last.start()-40): min(len(text), last.end()+80)].strip()
    fingerprint = f"D{day}-{hh:02d}{mm:02d}-{hash(snippet)}"

    return {
        "day": day,
        "hour": hh,
        "minute": mm,
        "real_epoch": real_epoch,  # may be None
        "fingerprint": fingerprint,
        "snippet": snippet,
    }

def _extract_text_from_message(msg: discord.Message) -> str:
    """
    Pull all text we can from a message:
    - content
    - embed descriptions
    - embed field values
    """
    parts: List[str] = []
    if msg.content:
        parts.append(msg.content)

    for emb in msg.embeds or []:
        if emb.description:
            parts.append(emb.description)
        # fields
        for f in getattr(emb, "fields", []) or []:
            if getattr(f, "value", None):
                parts.append(str(f.value))
            if getattr(f, "name", None):
                # sometimes time is in the name
                parts.append(str(f.name))

    return "\n".join(parts)


# =====================
# SYNC + FORECAST
# =====================
def _apply_sync_from_timed(parsed: dict) -> Tuple[bool, str]:
    """
    Apply a sync point from a timed line (Day, HH:MM) + optional real_epoch.
    Updates:
      - _TIME_STATE day/hour/minute
      - anchor values for forecasting
      - rate estimation if we have real_epoch and previous sync point
    """
    global _anchor_real_epoch, _anchor_game_minutes
    global _rate_game_per_real_min, _last_sync_real_epoch, _last_sync_game_minutes
    global _last_timed_line_fingerprint

    if not parsed:
        return False, "No parsed timed line."

    # Prevent re-applying the same exact timed line endlessly
    fp = parsed.get("fingerprint")
    if fp and _last_timed_line_fingerprint == fp:
        return False, "Timed line already applied."

    day = int(parsed["day"])
    hh = int(parsed["hour"])
    mm = int(parsed["minute"])

    game_minutes = _game_minutes_from_parts(day, hh, mm)

    # Use parsed real epoch if present, otherwise "now"
    real_epoch = parsed.get("real_epoch")
    if real_epoch is None:
        real_epoch = time.time()

    # Rate estimation based on previous sync point
    if _last_sync_real_epoch is not None and _last_sync_game_minutes is not None:
        dr = (real_epoch - float(_last_sync_real_epoch)) / 60.0  # real minutes
        dg = float(game_minutes) - float(_last_sync_game_minutes)  # game minutes
        if dr > 0.25:  # need at least 15s apart to avoid noise
            new_rate = dg / dr
            # clamp silly values
            new_rate = max(RATE_MIN, min(RATE_MAX, new_rate))
            # smooth
            _rate_game_per_real_min = (1.0 - RATE_SMOOTHING) * _rate_game_per_real_min + RATE_SMOOTHING * new_rate

    # Update anchor
    _anchor_real_epoch = float(real_epoch)
    _anchor_game_minutes = float(game_minutes)

    # Update last sync point
    _last_sync_real_epoch = float(real_epoch)
    _last_sync_game_minutes = float(game_minutes)
    _last_timed_line_fingerprint = fp

    # Update public time state (YEAR is NOT derivable from logs, keep current year)
    _set_time_state(day=day, hour=hh, minute=mm)

    _save_state()
    return True, f"Synced to Day {day} {hh:02d}:{mm:02d} (rate={_rate_game_per_real_min:.3f}x)."


def _tick_forecast_now():
    """
    Update _TIME_STATE from anchor + current time using estimated rate.
    This runs frequently so TravelerLogs always gets correct year/day.
    """
    if _anchor_real_epoch is None or _anchor_game_minutes is None:
        return  # no anchor yet

    now = time.time()
    dr_min = (now - float(_anchor_real_epoch)) / 60.0
    gm_now = float(_anchor_game_minutes) + dr_min * float(_rate_game_per_real_min)

    day, hh, mm = _parts_from_game_minutes(gm_now)
    _set_time_state(day=day, hour=hh, minute=mm)


async def _sync_from_discord_gamelogs(client: discord.Client) -> Tuple[bool, str]:
    """
    Scan recent messages in the game-logs channel for a timed line and apply sync.
    """
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
        # Newest first
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
        day="In-game day number",
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

        # clamp
        hour = max(0, min(23, int(hour)))
        minute = max(0, min(59, int(minute)))
        year = max(1, int(year))
        day = max(1, int(day))

        # Update state + anchors (anchor uses now)
        global _anchor_real_epoch, _anchor_game_minutes, _last_sync_real_epoch, _last_sync_game_minutes, _last_timed_line_fingerprint

        _set_time_state(year=year, day=day, hour=hour, minute=minute)

        gm = _game_minutes_from_parts(day, hour, minute)
        now = time.time()
        _anchor_real_epoch = now
        _anchor_game_minutes = float(gm)
        _last_sync_real_epoch = now
        _last_sync_game_minutes = float(gm)
        _last_timed_line_fingerprint = None  # manual time overrides

        _save_state()

        # Push webhook immediately
        try:
            await webhook_upsert("time", _make_time_embed_dict())
        except Exception as e:
            if SHOW_DEBUG:
                print("[time_module] webhook_upsert error:", e)

        await interaction.response.send_message(
            f"✅ Set time: Year {year} | Day {day} | {_format_hhmm(hour, minute)}",
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
            # push webhook now
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
        - update time webhook
    """
    _ensure_dir(STATE_FILE)
    _load_state()

    # If we have no anchor but we have a saved displayed time, create a default anchor from now
    global _anchor_real_epoch, _anchor_game_minutes
    if _anchor_real_epoch is None or _anchor_game_minutes is None:
        gm = _game_minutes_from_parts(_TIME_STATE["day"], _TIME_STATE["hour"], _TIME_STATE["minute"])
        _anchor_real_epoch = time.time()
        _anchor_game_minutes = float(gm)
        _save_state()

    print("[time_module] ✅ time loop running")

    last_webhook_push = 0.0
    while True:
        try:
            # Forecast to keep _TIME_STATE current for traveler logs
            _tick_forecast_now()

            # Auto-sync (non-fatal)
            ok, info = await _sync_from_discord_gamelogs(client)
            if ok and SHOW_DEBUG:
                print(f"[time_module] Auto-sync: {info}")
            elif (not ok) and SHOW_DEBUG:
                print(f"[time_module] Auto-sync: {info}")

            # After sync, forecast again (keeps displayed time stable)
            _tick_forecast_now()

            # Push webhook
            now = time.time()
            if now - last_webhook_push >= max(5, UPDATE_SECONDS - 1):
                try:
                    await webhook_upsert("time", _make_time_embed_dict())
                except Exception as e:
                    if SHOW_DEBUG:
                        print("[time_module] webhook_upsert error:", e)
                last_webhook_push = now

            # Persist occasionally
            _save_state()

        except Exception as e:
            print(f"[time_module] loop error: {e}")

        await asyncio.sleep(max(5, UPDATE_SECONDS))