# time_module.py
import os
import re
import json
import time
import asyncio
import discord
import aiohttp
from discord import app_commands
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

# ============================================================
# ENV
# ============================================================
GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0") or "0")

# Accept either name
TIME_WEBHOOK_URL = os.getenv("TIME_WEBHOOK_URL") or os.getenv("WEBHOOK_URL")

ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0") or "0")

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "0") or "0")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

# Persist on Railway volume (you said you added one)
TIME_STATE_PATH = os.getenv("TIME_STATE_PATH", "/data/time_state.json")

# ============================================================
# ASA day/night boundaries
# ============================================================
SUNRISE_MIN = 5 * 60 + 30   # 05:30
SUNSET_MIN = 17 * 60 + 30   # 17:30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

# ============================================================
# NITRADO multipliers (from your screenshot)
# Defaults: full cycle 60 mins real time => 2.5 sec per in-game minute
# Apply multipliers:
#   base_spm = 2.5 * DayCycleSpeedScale
#   day_spm  = base_spm / DayTimeSpeedScale
#   night_spm= base_spm / NightTimeSpeedScale
# ============================================================
DAY_CYCLE_SPEED_SCALE = float(os.getenv("DAYCYCLE_SPEEDSCALE", "5.92"))
DAY_TIME_SPEED_SCALE = float(os.getenv("DAYTIME_SPEEDSCALE", "1.85"))
NIGHT_TIME_SPEED_SCALE = float(os.getenv("NIGHTTIME_SPEEDSCALE", "2.18"))

BASE_SPM_DEFAULT = 2.5
BASE_SPM = BASE_SPM_DEFAULT * DAY_CYCLE_SPEED_SCALE
DAY_SPM = BASE_SPM / DAY_TIME_SPEED_SCALE
NIGHT_SPM = BASE_SPM / NIGHT_TIME_SPEED_SCALE

# ============================================================
# Update rules
# ============================================================
TIME_UPDATE_STEP_MINUTES = 10      # edit embed only on 00/10/20/...
TIME_TICK_SECONDS = 5              # check loop frequency (safe + responsive)
AUTO_SYNC_EVERY_SECONDS = 600      # auto sync every 10 minutes
SYNC_DRIFT_MINUTES = int(os.getenv("TIME_SYNC_DRIFT_MINUTES", "2"))
SYNC_COOLDOWN_SECONDS = int(os.getenv("TIME_SYNC_COOLDOWN_SECONDS", "600"))

# ============================================================
# Shared state
# ============================================================
_state = None
_http_session: aiohttp.ClientSession | None = None
_last_sync_ts = 0.0

# For “don’t spam edits”
_last_posted_boundary = None  # (year, day, minute_of_day_boundary)

# Force update event (so /settime can instantly push)
_force_update_event = asyncio.Event()

# ============================================================
# Helpers
# ============================================================
def _require_env():
    missing = []
    if not TIME_WEBHOOK_URL:
        missing.append("TIME_WEBHOOK_URL (or WEBHOOK_URL)")
    if not GUILD_ID:
        missing.append("GUILD_ID")
    if not ADMIN_ROLE_ID:
        missing.append("ADMIN_ROLE_ID")
    if not (RCON_HOST and RCON_PORT and RCON_PASSWORD):
        missing.append("RCON_HOST/RCON_PORT/RCON_PASSWORD")

    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


def _load_state():
    if not os.path.exists(TIME_STATE_PATH):
        return None
    try:
        with open(TIME_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_state(s: dict):
    os.makedirs(os.path.dirname(TIME_STATE_PATH), exist_ok=True)
    with open(TIME_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f)


def _is_day(minute_of_day: int) -> bool:
    return SUNRISE_MIN <= minute_of_day < SUNSET_MIN


def _spm(minute_of_day: int) -> float:
    return DAY_SPM if _is_day(minute_of_day) else NIGHT_SPM


def _advance_one_minute(minute_of_day: int, day: int, year: int):
    minute_of_day += 1
    if minute_of_day >= 1440:
        minute_of_day = 0
        day += 1
        if day > 365:
            day = 1
            year += 1
    return minute_of_day, day, year


def _calculate_time():
    """
    Returns:
      (minute_of_day, day, year, seconds_into_current_minute, current_spm)
    """
    global _state
    if not _state:
        return None

    elapsed = float(time.time() - float(_state["epoch"]))
    minute_of_day = int(_state["hour"]) * 60 + int(_state["minute"])
    day = int(_state["day"])
    year = int(_state["year"])

    remaining = elapsed
    while True:
        cur_spm = _spm(minute_of_day)
        if remaining >= cur_spm:
            remaining -= cur_spm
            minute_of_day, day, year = _advance_one_minute(minute_of_day, day, year)
            continue
        return minute_of_day, day, year, remaining, cur_spm


def _build_embed(minute_of_day: int, day: int, year: int):
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    emoji = "☀️" if _is_day(minute_of_day) else "🌙"
    color = DAY_COLOR if _is_day(minute_of_day) else NIGHT_COLOR
    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    return {"title": title, "color": color}


def _url_with_wait(url: str) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q["wait"] = "true"
    new_query = urlencode(q, doseq=True)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, p.fragment))


async def _upsert_webhook_embed(embed: dict):
    """
    Posts once then PATCHes same message forever.
    Stores message id in state.
    """
    global _state, _http_session
    if not _http_session:
        raise RuntimeError("HTTP session not ready")
    if not _state:
        raise RuntimeError("No time state set")

    _state.setdefault("message_ids", {})
    mid = _state["message_ids"].get("time")

    base_url = TIME_WEBHOOK_URL

    if mid:
        patch_base = base_url.split("?", 1)[0] + f"/messages/{mid}"
        # preserve original query params (thread_id etc)
        if "?" in base_url:
            patch_base = patch_base + "?" + base_url.split("?", 1)[1]
        patch_url = _url_with_wait(patch_base)

        async with _http_session.patch(patch_url, json={"embeds": [embed]}) as r:
            if r.status == 404:
                # message deleted; re-create
                _state["message_ids"]["time"] = None
                _save_state(_state)
                return await _upsert_webhook_embed(embed)
            if r.status >= 300:
                try:
                    data = await r.json()
                except Exception:
                    data = await r.text()
                raise RuntimeError(f"Webhook patch failed: {r.status} {data}")
        return

    # Create message
    post_url = _url_with_wait(base_url)
    async with _http_session.post(post_url, json={"embeds": [embed]}) as r:
        try:
            data = await r.json()
        except Exception:
            data = await r.text()
        if r.status >= 300:
            raise RuntimeError(f"Webhook post failed: {r.status} {data}")
        if not (isinstance(data, dict) and "id" in data):
            raise RuntimeError(f"Webhook response missing id: {data}")

        _state["message_ids"]["time"] = data["id"]
        _save_state(_state)


# ============================================================
# RCON
# ============================================================
def _rcon_make_packet(req_id: int, ptype: int, body: str) -> bytes:
    data = body.encode("utf-8") + b"\x00"
    packet = (
        req_id.to_bytes(4, "little", signed=True)
        + ptype.to_bytes(4, "little", signed=True)
        + data
        + b"\x00"
    )
    size = len(packet)
    return size.to_bytes(4, "little", signed=True) + packet


async def rcon_command(command: str, timeout: float = 10.0) -> str:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout
    )
    try:
        writer.write(_rcon_make_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        if len(raw) < 12:
            raise RuntimeError("RCON auth failed (short response)")

        writer.write(_rcon_make_packet(2, 2, command))
        await writer.drain()

        chunks = []
        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                part = await asyncio.wait_for(reader.read(4096), timeout=0.3)
            except asyncio.TimeoutError:
                break
            if not part:
                break
            chunks.append(part)

        if not chunks:
            return ""

        data = b"".join(chunks)
        out = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i:i+4], "little", signed=True)
            i += 4
            if i + size > len(data) or size < 10:
                break
            pkt = data[i:i+size]
            i += size
            body = pkt[8:-2]
            txt = body.decode("utf-8", errors="ignore")
            if txt:
                out.append(txt)

        return "".join(out).strip()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ============================================================
# Sync from GetGameLog
# Accept:
#   "Day 237, 01:14:22: ..."
#   "Day 237, 01:14:22 - ..."
# ============================================================
_DAYTIME_RE = re.compile(r"Day\s+(\d+),\s+(\d{1,2}):(\d{2}):(\d{2})\s*[:\-]", re.IGNORECASE)

def _parse_latest_daytime(text: str):
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = _DAYTIME_RE.search(ln)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return None


def _wrap_minute_diff(diff: int) -> int:
    while diff > 720:
        diff -= 1440
    while diff < -720:
        diff += 1440
    return diff


def _apply_sync(parsed_day: int, parsed_hour: int, parsed_minute: int, parsed_second: int):
    global _state
    if not _state:
        return False, "No state set"

    details = _calculate_time()
    if not details:
        return False, "No calculated time"

    cur_minute_of_day, cur_day, cur_year, seconds_into_minute, cur_spm = details
    target_minute_of_day = parsed_hour * 60 + parsed_minute

    day_diff = parsed_day - cur_day
    if day_diff > 180:
        day_diff -= 365
    elif day_diff < -180:
        day_diff += 365

    minute_diff = day_diff * 1440 + (target_minute_of_day - cur_minute_of_day)
    minute_diff = _wrap_minute_diff(minute_diff)

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {minute_diff} min < threshold"

    # seconds fraction adjustment (small)
    second_frac_diff = (parsed_second / 60.0) - (seconds_into_minute / max(cur_spm, 0.001))

    real_shift = (minute_diff * cur_spm) + (second_frac_diff * cur_spm)
    _state["epoch"] = float(_state["epoch"]) - real_shift

    _state["day"] = int(parsed_day)
    _state["hour"] = int(parsed_hour)
    _state["minute"] = int(parsed_minute)
    _save_state(_state)

    return True, f"Synced (drift {minute_diff} min)"


async def _sync_once():
    global _last_sync_ts
    if not _state:
        return False, "No time set. Use /settime first."

    now = time.time()
    if (now - _last_sync_ts) < SYNC_COOLDOWN_SECONDS:
        return False, "Sync cooldown active."

    text = await rcon_command("GetGameLog", timeout=10.0)
    if not text:
        return False, "GetGameLog returned empty output."

    parsed = _parse_latest_daytime(text)
    if not parsed:
        return False, "No Day/Time found in GetGameLog."

    d, h, m, s = parsed
    changed, msg = _apply_sync(d, h, m, s)
    if changed:
        _last_sync_ts = time.time()
        _force_update_event.set()  # show it immediately
    return changed, msg


# ============================================================
# Public API
# ============================================================
def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int):
    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(name="settime", guild=guild_obj, description="Set Solunaris in-game time anchor")
    async def settime_cmd(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
        if not any(getattr(r, "id", None) == int(admin_role_id) for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
            await i.response.send_message("❌ Invalid values.", ephemeral=True)
            return

        global _state
        _state = _load_state() or {}
        _state.setdefault("message_ids", {})

        _state.update({
            "epoch": time.time(),
            "year": int(year),
            "day": int(day),
            "hour": int(hour),
            "minute": int(minute),
        })
        _save_state(_state)

        # Force immediate webhook update
        _force_update_event.set()

        await i.response.send_message("✅ Time set.", ephemeral=True)

    @tree.command(name="sync", guild=guild_obj, description="Force a one-time RCON GetGameLog sync")
    async def sync_cmd(i: discord.Interaction):
        if not any(getattr(r, "id", None) == int(admin_role_id) for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        await i.response.defer(ephemeral=True)
        try:
            changed, msg = await _sync_once()
            await i.followup.send(("✅ " if changed else "ℹ️ ") + msg, ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Sync failed: {e}", ephemeral=True)


async def run_time_loop(client: discord.Client):
    """
    Reliable loop:
      - checks every 5s
      - edits webhook only when hitting a NEW 10-min boundary (or /settime forces)
      - auto-sync every 10 minutes real time
      - daily announce when day rolls over
    """
    _require_env()
    await client.wait_until_ready()

    global _state, _http_session, _last_posted_boundary
    _state = _load_state()
    last_auto_sync = 0.0
    last_announced_abs_day = None

    async with aiohttp.ClientSession() as session:
        _http_session = session

        while True:
            try:
                # auto sync every 10 mins real time
                now = time.time()
                if now - last_auto_sync >= AUTO_SYNC_EVERY_SECONDS:
                    last_auto_sync = now
                    try:
                        await _sync_once()
                    except Exception as e:
                        print(f"[time] auto-sync error: {e}")

                details = _calculate_time()
                if not details:
                    await asyncio.sleep(TIME_TICK_SECONDS)
                    continue

                minute_of_day, day, year, seconds_into_minute, cur_spm = details

                # daily announce
                if ANNOUNCE_CHANNEL_ID:
                    abs_day = year * 365 + day
                    if last_announced_abs_day is None:
                        last_announced_abs_day = abs_day
                    elif abs_day > last_announced_abs_day:
                        ch = client.get_channel(ANNOUNCE_CHANNEL_ID)
                        if ch:
                            try:
                                await ch.send(f"📅 **New Solunaris Day** — Day **{day}**, Year **{year}**")
                            except Exception:
                                pass
                        last_announced_abs_day = abs_day

                forced = _force_update_event.is_set()
                if forced:
                    _force_update_event.clear()

                # Only post on a boundary OR if forced
                if forced or (minute_of_day % TIME_UPDATE_STEP_MINUTES == 0):
                    boundary_key = (year, day, minute_of_day if (minute_of_day % TIME_UPDATE_STEP_MINUTES == 0) else -1)

                    # If not forced, don't repeat same boundary
                    if forced or (_last_posted_boundary != boundary_key):
                        embed = _build_embed(minute_of_day, day, year)
                        await _upsert_webhook_embed(embed)
                        if minute_of_day % TIME_UPDATE_STEP_MINUTES == 0:
                            _last_posted_boundary = boundary_key

                await asyncio.sleep(TIME_TICK_SECONDS)

            except Exception as e:
                # Never die; log and keep going
                print(f"[time] loop error: {e}")
                await asyncio.sleep(5)