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
DISCORD_GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0") or "0")

TIME_WEBHOOK_URL = os.getenv("TIME_WEBHOOK_URL") or os.getenv("WEBHOOK_URL")  # allow old name
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0") or "0")

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "0") or "0")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

# Persist state + webhook message id (put this on your Railway volume, e.g. /data)
TIME_STATE_PATH = os.getenv("TIME_STATE_PATH", "/data/time_state.json")

# ============================================================
# NITRADO TIME SETTINGS (your values shown earlier)
# Default ASA: full day-night cycle = 60 mins real time.
# That means 1 in-game minute = 2.5 seconds real time at defaults.
# We apply your multipliers as:
#   base_spm = 2.5 * DayCycleSpeedScale
#   day_spm  = base_spm / DayTimeSpeedScale
#   night_spm= base_spm / NightTimeSpeedScale
# (This matches how your previous “SPM” style behaved—bigger SPM => slower clock.)
# ============================================================
DAY_CYCLE_SPEED_SCALE = float(os.getenv("DAYCYCLE_SPEEDSCALE", "5.92"))
DAY_TIME_SPEED_SCALE = float(os.getenv("DAYTIME_SPEEDSCALE", "1.85"))
NIGHT_TIME_SPEED_SCALE = float(os.getenv("NIGHTTIME_SPEEDSCALE", "2.18"))

BASE_SPM_DEFAULT = 2.5  # 60 mins / 1440 in-game mins = 2.5 sec per in-game minute
BASE_SPM = BASE_SPM_DEFAULT * DAY_CYCLE_SPEED_SCALE
DAY_SPM = BASE_SPM / DAY_TIME_SPEED_SCALE
NIGHT_SPM = BASE_SPM / NIGHT_TIME_SPEED_SCALE

# In-game sunrise/sunset for ASA default
SUNRISE_MIN = 5 * 60 + 30   # 05:30
SUNSET_MIN = 17 * 60 + 30   # 17:30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

# Update the time message on round 10-minute boundaries (00,10,20,...)
TIME_UPDATE_STEP_MINUTES = 10

# Auto sync (every 10 minutes real time)
AUTO_SYNC_EVERY_SECONDS = 600
SYNC_DRIFT_MINUTES = int(os.getenv("TIME_SYNC_DRIFT_MINUTES", "2"))  # only adjust if drift >= N minutes
SYNC_COOLDOWN_SECONDS = int(os.getenv("TIME_SYNC_COOLDOWN_SECONDS", "600"))  # don’t sync more often than this

# ============================================================
# STATE
# ============================================================
_state = None
_last_sync_ts = 0.0

# ============================================================
# HELPERS
# ============================================================
def _require_env():
    missing = []
    if not TIME_WEBHOOK_URL:
        missing.append("TIME_WEBHOOK_URL (or WEBHOOK_URL)")
    if not (RCON_HOST and RCON_PORT and RCON_PASSWORD):
        missing.append("RCON_HOST/RCON_PORT/RCON_PASSWORD")
    if not DISCORD_GUILD_ID:
        missing.append("GUILD_ID")
    if not ADMIN_ROLE_ID:
        missing.append("ADMIN_ROLE_ID")

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


def _calculate_time_details():
    """
    Returns:
      (minute_of_day, day, year, seconds_into_current_minute, current_minute_spm)
    """
    global _state
    if not _state:
        return None

    elapsed = float(time.time() - float(_state["epoch"]))
    minute_of_day = int(_state["hour"]) * 60 + int(_state["minute"])
    day = int(_state["day"])
    year = int(_state["year"])

    remaining = elapsed

    # Walk forward in-game minutes using SPM model
    while True:
        cur_spm = _spm(minute_of_day)
        if remaining >= cur_spm:
            remaining -= cur_spm
            minute_of_day, day, year = _advance_one_minute(minute_of_day, day, year)
            continue

        seconds_into_current_minute = remaining
        return minute_of_day, day, year, seconds_into_current_minute, cur_spm


def _build_time_embed(minute_of_day: int, day: int, year: int):
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    emoji = "☀️" if _is_day(minute_of_day) else "🌙"
    color = DAY_COLOR if _is_day(minute_of_day) else NIGHT_COLOR
    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    return {"title": title, "color": color}


def _url_with_wait(url: str) -> str:
    """
    Ensures Discord returns a JSON message object with an 'id' by adding wait=true.
    Preserves existing query params (including thread_id).
    """
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q["wait"] = "true"
    new_query = urlencode(q, doseq=True)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, p.fragment))


async def _upsert_webhook_embed(session: aiohttp.ClientSession, webhook_url: str, key: str, embed: dict):
    """
    Post once, then PATCH the same message using /messages/{id}.
    Stores message id in state.
    """
    global _state
    if not _state:
        return

    if "message_ids" not in _state:
        _state["message_ids"] = {}
    mid = _state["message_ids"].get(key)

    # If we have a message id, patch it
    if mid:
        patch_url = webhook_url.split("?", 1)[0] + f"/messages/{mid}"
        # preserve thread_id/wait etc for patch too (Discord ignores wait on patch, but fine)
        patch_url = _url_with_wait(patch_url + ("?" + webhook_url.split("?", 1)[1] if "?" in webhook_url else ""))
        async with session.patch(patch_url, json={"embeds": [embed]}) as r:
            if r.status == 404:
                # message deleted -> re-create
                _state["message_ids"][key] = None
                _save_state(_state)
                return await _upsert_webhook_embed(session, webhook_url, key, embed)
            if r.status >= 300:
                try:
                    data = await r.json()
                except Exception:
                    data = await r.text()
                raise RuntimeError(f"Webhook patch failed: {r.status} {data}")
        return

    # Else create the message
    post_url = _url_with_wait(webhook_url)
    async with session.post(post_url, json={"embeds": [embed]}) as r:
        try:
            data = await r.json()
        except Exception:
            data = await r.text()
        if r.status >= 300:
            raise RuntimeError(f"Webhook post failed: {r.status} {data}")

        if isinstance(data, dict) and "id" in data:
            _state["message_ids"][key] = data["id"]
            _save_state(_state)
        else:
            # If Discord didn’t return message object, you’ll never be able to patch.
            raise RuntimeError(f"Webhook post did not return message id. Response: {data}")


# ============================================================
# RCON (minimal Source RCON)
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


async def rcon_command(command: str, timeout: float = 8.0) -> str:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout
    )
    try:
        # auth
        writer.write(_rcon_make_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        if len(raw) < 12:
            raise RuntimeError("RCON auth failed (short response)")

        # cmd
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
# AUTO SYNC (GetGameLog -> Day/Time)
# ============================================================
# Accept both formats:
#   "Day 237, 01:14:22: ..."
#   "Day 237, 01:14:22 - ..."
_DAYTIME_RE = re.compile(r"Day\s+(\d+),\s+(\d{1,2}):(\d{2}):(\d{2})\s*[:\-]", re.IGNORECASE)

def _parse_latest_daytime_from_gamelog(text: str):
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = _DAYTIME_RE.search(ln)
        if not m:
            continue
        day = int(m.group(1))
        hour = int(m.group(2))
        minute = int(m.group(3))
        second = int(m.group(4))
        return day, hour, minute, second
    return None


def _wrap_minute_diff(diff: int) -> int:
    while diff > 720:
        diff -= 1440
    while diff < -720:
        diff += 1440
    return diff


def _apply_sync(parsed_day: int, parsed_hour: int, parsed_minute: int, parsed_second: int):
    """
    Shifts epoch so predicted time aligns to parsed (Day, HH:MM:SS) "now".
    Keeps your SPM model; only corrects drift.
    """
    global _state
    if not _state:
        return False, "No state set"

    details = _calculate_time_details()
    if not details:
        return False, "No calculated time"

    cur_minute_of_day, cur_day, cur_year, seconds_into_minute, cur_spm = details

    target_minute_of_day = parsed_hour * 60 + parsed_minute

    # day diff (within year)
    day_diff = parsed_day - cur_day
    if day_diff > 180:
        day_diff -= 365
    elif day_diff < -180:
        day_diff += 365

    minute_diff = day_diff * 1440 + (target_minute_of_day - cur_minute_of_day)
    minute_diff = _wrap_minute_diff(minute_diff)

    # include seconds (convert seconds -> fraction of minute)
    # We treat parsed_second as position inside its minute (0..59)
    # and seconds_into_minute as how far we think we are in current minute.
    # Convert both into "minute fractions" using current minute SPM.
    # For stability we only use seconds correction if minutes already close.
    second_fraction_diff = (parsed_second / 60.0) - (seconds_into_minute / max(cur_spm, 0.001))

    # Turn seconds fraction into real-seconds shift using current minute SPM
    real_seconds_shift = (minute_diff * cur_spm) + (second_fraction_diff * cur_spm)

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        # If minutes are small, don’t keep micro-adjusting (prevents jitter)
        return False, f"Drift {minute_diff} min < threshold"

    _state["epoch"] = float(_state["epoch"]) - real_seconds_shift
    _state["day"] = int(parsed_day)
    _state["hour"] = int(parsed_hour)
    _state["minute"] = int(parsed_minute)
    _save_state(_state)

    return True, f"Synced (drift {minute_diff} min)"


async def _sync_once_from_gamelog():
    """
    One sync attempt (used by auto loop + /sync).
    """
    global _last_sync_ts
    if not _state:
        return False, "No time state set. Use /settime first."

    now = time.time()
    if (now - _last_sync_ts) < SYNC_COOLDOWN_SECONDS:
        return False, "Sync cooldown active."

    log_text = await rcon_command("GetGameLog", timeout=10.0)
    if not log_text:
        return False, "GetGameLog returned empty output."

    parsed = _parse_latest_daytime_from_gamelog(log_text)
    if not parsed:
        return False, "No Day/Time found in GetGameLog."

    d, h, m, s = parsed
    changed, msg = _apply_sync(d, h, m, s)
    if changed:
        _last_sync_ts = time.time()
    return changed, msg


# ============================================================
# PUBLIC API (what main.py imports)
# ============================================================
def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int):
    """
    Registers /settime and /sync on the provided CommandTree.
    Call this inside on_ready BEFORE tree.sync(...).
    """

    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(name="settime", guild=guild_obj, description="Set Solunaris in-game time anchor")
    async def settime_cmd(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
        # Admin role only
        if not any(getattr(r, "id", None) == int(admin_role_id) for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
            await i.response.send_message("❌ Invalid values.", ephemeral=True)
            return

        global _state
        _state = _load_state() or {}
        _state.update({
            "epoch": time.time(),
            "year": int(year),
            "day": int(day),
            "hour": int(hour),
            "minute": int(minute),
        })
        _state.setdefault("message_ids", {})
        _save_state(_state)

        await i.response.send_message("✅ Time set.", ephemeral=True)

    @tree.command(name="sync", guild=guild_obj, description="Force a one-time RCON GetGameLog sync")
    async def sync_cmd(i: discord.Interaction):
        # Admin role only
        if not any(getattr(r, "id", None) == int(admin_role_id) for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        await i.response.defer(ephemeral=True)
        changed, msg = await _sync_once_from_gamelog()
        emoji = "✅" if changed else "ℹ️"
        await i.followup.send(f"{emoji} {msg}", ephemeral=True)


async def run_time_loop(client: discord.Client):
    """
    Main loop:
      - updates time webhook on in-game round 10 minutes
      - auto-sync from GetGameLog every 10 minutes (real time)
      - posts daily announce when day ticks over
    """
    _require_env()

    global _state
    _state = _load_state()

    last_announced_abs_day = None
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        # small startup delay so other modules can settle
        await asyncio.sleep(2)

        # Run forever
        last_auto_sync_try = 0.0

        while True:
            # auto-sync every 10 mins (real time)
            now = time.time()
            if now - last_auto_sync_try >= AUTO_SYNC_EVERY_SECONDS:
                last_auto_sync_try = now
                try:
                    # attempt sync, ignore "not changed" messages
                    await _sync_once_from_gamelog()
                except Exception as e:
                    print(f"Time auto-sync error: {e}")

            details = _calculate_time_details()
            if not details:
                # nothing set yet
                await asyncio.sleep(5)
                continue

            minute_of_day, day, year, seconds_into_minute, cur_spm = details

            # update webhook only on 10-minute boundaries (in-game)
            if (minute_of_day % TIME_UPDATE_STEP_MINUTES) == 0:
                try:
                    embed = _build_time_embed(minute_of_day, day, year)
                    await _upsert_webhook_embed(session, TIME_WEBHOOK_URL, "time", embed)
                except Exception as e:
                    print(f"Time webhook update error: {e}")

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

                # sleep until next “round step”
                # compute real seconds until next boundary
                mod = minute_of_day % TIME_UPDATE_STEP_MINUTES
                minutes_to_boundary = TIME_UPDATE_STEP_MINUTES if mod == 0 else (TIME_UPDATE_STEP_MINUTES - mod)

                # remaining in current minute
                remaining_in_current_minute = max(0.0, cur_spm - seconds_into_minute)
                total_sleep = remaining_in_current_minute

                # add full minutes after current minute until boundary
                m2, d2, y2 = minute_of_day, day, year
                for _ in range(minutes_to_boundary - 1):
                    m2, d2, y2 = _advance_one_minute(m2, d2, y2)
                    total_sleep += _spm(m2)

                await asyncio.sleep(max(0.5, total_sleep))
            else:
                # not on boundary -> sleep until boundary
                mod = minute_of_day % TIME_UPDATE_STEP_MINUTES
                minutes_to_boundary = TIME_UPDATE_STEP_MINUTES - mod

                remaining_in_current_minute = max(0.0, cur_spm - seconds_into_minute)
                total_sleep = remaining_in_current_minute

                m2, d2, y2 = minute_of_day, day, year
                for _ in range(minutes_to_boundary - 1):
                    m2, d2, y2 = _advance_one_minute(m2, d2, y2)
                    total_sleep += _spm(m2)

                await asyncio.sleep(max(0.5, total_sleep))