import os
import re
import json
import time
import asyncio
from typing import Optional, Tuple, Dict, Any, List

import discord
from discord import app_commands

# =========================================
# ENV / CONFIG
# =========================================

# Where we persist time state (mount this into your Railway volume)
TIME_STATE_FILE = os.getenv("TIME_STATE_FILE", "/data/time_state.json")

# Where tribelogs routes are stored (same volume) - must match tribelogs_module
TRIBELOG_ROUTES_FILE = os.getenv("TRIBELOG_ROUTES_FILE", "/data/tribelog_routes.json")

# Auto-sync interval (minutes)
AUTO_SYNC_MINUTES = int(os.getenv("TIME_AUTO_SYNC_MINUTES", "10"))

# Poll / update interval for the displayed time (seconds)
TIME_TICK_SECONDS = float(os.getenv("TIME_TICK_SECONDS", "10"))

# Daily post channel (text channel id) - optional but recommended
TIME_DAILY_CHANNEL_ID = int(os.getenv("TIME_DAILY_CHANNEL_ID", "0") or "0")

# If you want the time webhook to only update every X seconds (avoid rate issues)
TIME_WEBHOOK_MIN_SECONDS = float(os.getenv("TIME_WEBHOOK_MIN_SECONDS", "10"))

# Nitrado scales (your values from screenshot as defaults)
DAYCYCLE_SPEED_SCALE = float(os.getenv("DAYCYCLE_SPEED_SCALE", "5.92"))
DAYTIME_SPEED_SCALE = float(os.getenv("DAYTIME_SPEED_SCALE", "1.85"))
NIGHTTIME_SPEED_SCALE = float(os.getenv("NIGHTTIME_SPEED_SCALE", "2.18"))

# ASA "default" day cycle is 1 hour real-time for 24h in-game => 1440 in-game minutes per 3600 real seconds
BASE_REAL_SECONDS_PER_INGAME_MINUTE = 3600.0 / 1440.0  # 2.5 sec per in-game minute at scale 1

# ASA day window from Nitrado guide: 05:30 to 17:30
DAY_START_MINUTE = 5 * 60 + 30   # 330
DAY_END_MINUTE = 17 * 60 + 30    # 1050

# Effective speed multipliers
EFFECTIVE_DAY_SCALE = DAYCYCLE_SPEED_SCALE * DAYTIME_SPEED_SCALE
EFFECTIVE_NIGHT_SCALE = DAYCYCLE_SPEED_SCALE * NIGHTTIME_SPEED_SCALE

# Regex that matches your tribelog output like:
# "Day 297, 00:16:23 - Einar claimed ..."
# Also tolerates "Day 297, 00:16 - ..." or "Day 297, 00:16:23:"
TRIBELOG_TIME_RE = re.compile(
    r"Day\s+(\d+)\s*,\s*(\d{2}):(\d{2})(?::(\d{2}))?"
)

# Optionally detect year if present in any text (some servers/formatters include it)
YEAR_RE = re.compile(r"Year\s+(\d+)", re.IGNORECASE)


# =========================================
# STATE
# =========================================

_state: Dict[str, Any] = {
    "year": 1,
    "day": 1,
    "minute_of_day": 12 * 60,     # 12:00 default
    "last_real_ts": None,         # unix seconds at last state update
    "last_daily_announce_day": None,
    "last_webhook_update_ts": 0.0,
}


def _now() -> float:
    return time.time()


def _load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        return default


def _save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _load_state() -> None:
    global _state
    data = _load_json(TIME_STATE_FILE, None)
    if isinstance(data, dict):
        _state.update(data)
    if not _state.get("last_real_ts"):
        _state["last_real_ts"] = _now()
    if _state.get("last_daily_announce_day") is None:
        _state["last_daily_announce_day"] = _state.get("day", 1)


def _save_state() -> None:
    _save_json(TIME_STATE_FILE, _state)


def _is_daytime(minute_of_day: int) -> bool:
    # Day is 05:30 -> 17:30
    return DAY_START_MINUTE <= minute_of_day < DAY_END_MINUTE


def _sec_per_ingame_minute(minute_of_day: int) -> float:
    scale = EFFECTIVE_DAY_SCALE if _is_daytime(minute_of_day) else EFFECTIVE_NIGHT_SCALE
    if scale <= 0:
        scale = 1.0
    return BASE_REAL_SECONDS_PER_INGAME_MINUTE / scale


def _format_hhmm(minute_of_day: int) -> str:
    h = (minute_of_day // 60) % 24
    m = minute_of_day % 60
    return f"{h:02d}:{m:02d}"


def _make_time_embed_title(year: int, day: int, minute_of_day: int) -> Tuple[str, int]:
    # 1-line "big" embed title like you preferred
    icon = "☀️" if _is_daytime(minute_of_day) else "🌙"
    title = f"{icon} Solunaris Time — Year {year} | Day {day} | {_format_hhmm(minute_of_day)}"
    color = 0xFFD54A if _is_daytime(minute_of_day) else 0x2B6CB0
    return title, color


def _clean_text(s: str) -> str:
    if not s:
        return ""
    # Strip RichColor tags and closers
    s = re.sub(r"<RichColor[^>]*>", "", s)
    s = s.replace("</>", "")
    # Normalize smart quotes
    s = s.replace("’", "'").replace("“", '"').replace("”", '"')
    # Remove weird zero-width/control
    s = s.replace("\u200b", "").replace("\ufeff", "")
    return s.strip()


def _extract_from_text(text: str) -> Optional[Tuple[Optional[int], int, int]]:
    """
    Returns (year_or_None, day, minute_of_day) if found.
    """
    text = _clean_text(text)
    if not text:
        return None

    m = TRIBELOG_TIME_RE.search(text)
    if not m:
        return None

    day = int(m.group(1))
    hour = int(m.group(2))
    minute = int(m.group(3))
    minute_of_day = hour * 60 + minute

    y = None
    ym = YEAR_RE.search(text)
    if ym:
        try:
            y = int(ym.group(1))
        except Exception:
            y = None

    return (y, day, minute_of_day)


def _advance_time_by_real_seconds(real_elapsed: float) -> None:
    """
    Advances _state based on real seconds elapsed using day/night speed multipliers.
    Handles crossing day/night boundaries and day rollover.
    """
    if real_elapsed <= 0:
        return

    year = int(_state.get("year", 1))
    day = int(_state.get("day", 1))
    minute_of_day = int(_state.get("minute_of_day", 0))

    remaining = real_elapsed

    while remaining > 0:
        # Determine next boundary (either day start or day end) relative to current minute
        if _is_daytime(minute_of_day):
            boundary = DAY_END_MINUTE
        else:
            # If we're before day start, boundary is day start; else boundary is next day's day start
            boundary = DAY_START_MINUTE if minute_of_day < DAY_START_MINUTE else 1440 + DAY_START_MINUTE

        # Minutes until boundary
        mins_to_boundary = boundary - minute_of_day
        if mins_to_boundary <= 0:
            mins_to_boundary = 1

        sec_per_min = _sec_per_ingame_minute(minute_of_day)
        # How many in-game minutes can we advance before we hit boundary (or run out of remaining time)
        max_minutes_advance = remaining / sec_per_min

        if max_minutes_advance < mins_to_boundary:
            # advance within segment
            adv = int(max_minutes_advance)
            if adv <= 0:
                # not enough time for a full minute; stop
                break
            minute_of_day += adv
            remaining -= adv * sec_per_min
        else:
            # jump to boundary
            minute_of_day += mins_to_boundary
            remaining -= mins_to_boundary * sec_per_min

        # Handle rollover beyond 1440
        while minute_of_day >= 1440:
            minute_of_day -= 1440
            day += 1
            if day > 365:
                day = 1
                year += 1

    _state["year"] = year
    _state["day"] = day
    _state["minute_of_day"] = minute_of_day
    def _extract_latest_time_from_tribelog_channels(client: discord.Client) -> Optional[Tuple[int, int, int]]:
    """
    Reads recent messages from all tribelog routes (threads/channels) and returns the most recent
    (year, day, minute_of_day) found. Year may fall back to current _state year if not present.
    """
    routes = _load_json(TRIBELOG_ROUTES_FILE, [])
    if not isinstance(routes, list) or not routes:
        return None

    newest_ts = None
    newest_parsed = None

    for route in routes:
        # We try thread_id first, then channel_id
        thread_id = route.get("thread_id")
        channel_id = route.get("channel_id")

        target_id = None
        if thread_id:
            try:
                target_id = int(thread_id)
            except Exception:
                target_id = None
        if not target_id and channel_id:
            try:
                target_id = int(channel_id)
            except Exception:
                target_id = None

        if not target_id:
            continue

        ch = client.get_channel(target_id)
        if ch is None:
            # Might be a thread; try fetch
            try:
                ch = asyncio.get_event_loop().run_until_complete(client.fetch_channel(target_id))  # type: ignore
            except Exception:
                continue

        try:
            # Read last 30 messages
            msgs = []
            async for m in ch.history(limit=30):
                msgs.append(m)
        except Exception:
            continue

        for msg in msgs:
            # Candidates from message content + embeds
            candidates: List[str] = []
            if msg.content:
                candidates.append(msg.content)

            for emb in msg.embeds:
                if emb.title:
                    candidates.append(emb.title)
                if emb.description:
                    candidates.append(emb.description)
                for f in emb.fields:
                    if f.value:
                        candidates.append(f.value)

            for text in candidates:
                parsed = _extract_from_text(text)
                if not parsed:
                    continue
                y, d, mod = parsed
                if y is None:
                    y = int(_state.get("year", 1))

                # Use message timestamp to pick latest
                ts = msg.created_at.timestamp() if msg.created_at else None
                if ts is None:
                    continue

                if newest_ts is None or ts > newest_ts:
                    newest_ts = ts
                    newest_parsed = (int(y), int(d), int(mod))

    return newest_parsed


async def sync_from_tribelogs(client: discord.Client) -> bool:
    found = await _extract_latest_time_from_tribelog_channels_async(client)
    if not found:
        return False
    y, d, mod = found
    _state["year"] = int(y)
    _state["day"] = int(d)
    _state["minute_of_day"] = int(mod)
    _state["last_real_ts"] = _now()
    _save_state()
    return True


async def _extract_latest_time_from_tribelog_channels_async(client: discord.Client) -> Optional[Tuple[int, int, int]]:
    routes = _load_json(TRIBELOG_ROUTES_FILE, [])
    if not isinstance(routes, list) or not routes:
        return None

    newest_ts = None
    newest_parsed = None

    for route in routes:
        thread_id = route.get("thread_id")
        channel_id = route.get("channel_id")

        target_id = None
        if thread_id:
            try:
                target_id = int(thread_id)
            except Exception:
                target_id = None
        if not target_id and channel_id:
            try:
                target_id = int(channel_id)
            except Exception:
                target_id = None

        if not target_id:
            continue

        ch = client.get_channel(target_id)
        if ch is None:
            try:
                ch = await client.fetch_channel(target_id)
            except Exception:
                continue

        try:
            msgs = []
            async for m in ch.history(limit=30):
                msgs.append(m)
        except Exception:
            continue

        for msg in msgs:
            candidates: List[str] = []
            if msg.content:
                candidates.append(msg.content)

            for emb in msg.embeds:
                if emb.title:
                    candidates.append(emb.title)
                if emb.description:
                    candidates.append(emb.description)
                for f in emb.fields:
                    if f.value:
                        candidates.append(f.value)

            for text in candidates:
                parsed = _extract_from_text(text)
                if not parsed:
                    continue
                y, d, mod = parsed
                if y is None:
                    y = int(_state.get("year", 1))

                ts = msg.created_at.timestamp() if msg.created_at else None
                if ts is None:
                    continue

                if newest_ts is None or ts > newest_ts:
                    newest_ts = ts
                    newest_parsed = (int(y), int(d), int(mod))

    return newest_parsed


async def _maybe_daily_announce(client: discord.Client) -> None:
    if not TIME_DAILY_CHANNEL_ID:
        return
    last = _state.get("last_daily_announce_day")
    cur_day = int(_state.get("day", 1))
    cur_year = int(_state.get("year", 1))

    if last is None:
        _state["last_daily_announce_day"] = cur_day
        _save_state()
        return

    if cur_day != int(last):
        ch = client.get_channel(TIME_DAILY_CHANNEL_ID)
        if ch is None:
            try:
                ch = await client.fetch_channel(TIME_DAILY_CHANNEL_ID)
            except Exception:
                _state["last_daily_announce_day"] = cur_day
                _save_state()
                return

        try:
            await ch.send(f"🌅 **A new day has begun!** — Year {cur_year}, Day {cur_day}")
        except Exception:
            pass

        _state["last_daily_announce_day"] = cur_day
        _save_state()


def setup_time_commands(
    tree: app_commands.CommandTree,
    guild_id: int,
    admin_role_id: Optional[int] = None,
    rcon_command=None,
):
    guild_obj = discord.Object(id=int(guild_id))

    def _is_admin(interaction: discord.Interaction) -> bool:
        if not admin_role_id:
            return True
        if not interaction.user or not hasattr(interaction.user, "roles"):
            return False
        return any(getattr(r, "id", None) == int(admin_role_id) for r in interaction.user.roles)

    @tree.command(name="settime", description="Set in-game time (Year, Day, Hour, Minute).", guild=guild_obj)
    @app_commands.describe(year="In-game year", day="In-game day number", hour="Hour (0-23)", minute="Minute (0-59)")
    async def settime_cmd(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
        if not _is_admin(interaction):
            await interaction.response.send_message("❌ You don't have permission.", ephemeral=True)
            return

        hour = max(0, min(23, int(hour)))
        minute = max(0, min(59, int(minute)))
        day = max(1, min(365, int(day)))
        year = max(1, int(year))

        _state["year"] = year
        _state["day"] = day
        _state["minute_of_day"] = hour * 60 + minute
        _state["last_real_ts"] = _now()
        _save_state()

        await interaction.response.send_message("✅ Time set.", ephemeral=True)

    @tree.command(name="sync", description="Sync time from tribe logs.", guild=guild_obj)
    async def sync_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ok = await sync_from_tribelogs(interaction.client)
        if ok:
            await interaction.followup.send("✅ Synced time from tribe logs.", ephemeral=True)
        else:
            await interaction.followup.send("❌ No Day/Time found in tribe logs.", ephemeral=True)

    print("[time_module] ✅ /settime and /sync registered")


async def run_time_loop(client: discord.Client, rcon_command, webhook_upsert):
    """
    Main loop:
      - Advances time using SPM multipliers
      - Updates time webhook message (edit, not spam)
      - Auto-sync from tribe logs every TIME_AUTO_SYNC_MINUTES
      - Posts daily message when day increments
    """
    _load_state()
    await client.wait_until_ready()

    next_auto_sync = _now() + AUTO_SYNC_MINUTES * 60.0

    while True:
        try:
            # Advance time based on real elapsed
            now = _now()
            last_ts = float(_state.get("last_real_ts") or now)
            elapsed = now - last_ts
            _state["last_real_ts"] = now
            _advance_time_by_real_seconds(elapsed)

            # Daily announce
            await _maybe_daily_announce(client)

            # Auto-sync from tribelogs
            if now >= next_auto_sync:
                found = await _extract_latest_time_from_tribelog_channels_async(client)
                if found:
                    y, d, mod = found
                    _state["year"] = int(y)
                    _state["day"] = int(d)
                    _state["minute_of_day"] = int(mod)
                    _state["last_real_ts"] = _now()
                    _save_state()
                next_auto_sync = now + AUTO_SYNC_MINUTES * 60.0

            # Webhook update (rate-limited)
            if now - float(_state.get("last_webhook_update_ts", 0.0)) >= TIME_WEBHOOK_MIN_SECONDS:
                y = int(_state.get("year", 1))
                d = int(_state.get("day", 1))
                mod = int(_state.get("minute_of_day", 0))

                title, color = _make_time_embed_title(y, d, mod)
                embed = {
                    "title": title,
                    "color": color,
                }

                # key defaults to "time" in your main.py wrapper
                await webhook_upsert("time", embed)

                _state["last_webhook_update_ts"] = now
                _save_state()

        except Exception as e:
            print(f"[time_module] loop error: {e}")

        await asyncio.sleep(TIME_TICK_SECONDS)