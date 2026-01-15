import os
import re
import json
import time
import asyncio
from typing import Optional, Tuple

import aiohttp
import discord
from discord import app_commands

# ============================================================
# CONFIG (keep these matching your server)
# ============================================================

# Discord IDs
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076
ANNOUNCE_CHANNEL_ID = 1430388267446042666  # daily "new day" message channel

# Webhook for the time card (set ONE of these env vars)
# Prefer TIME_WEBHOOK_URL; WEBHOOK_URL kept for backward-compat
TIME_WEBHOOK_URL = os.getenv("TIME_WEBHOOK_URL") or os.getenv("WEBHOOK_URL")

# RCON
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = os.getenv("RCON_PORT")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

# Poll/schedule
TIME_UPDATE_STEP_MINUTES = 10           # update webhook only on round 10-min marks
AUTO_SYNC_SECONDS = 120                 # poll GetGameLog for sync every 2 mins
SYNC_COOLDOWN_SECONDS = 300             # don't sync more often than this
SYNC_DRIFT_MINUTES = 2                  # ignore tiny drift (minutes)
RCON_TIMEOUT_SECONDS = 10.0

# Day/Night model (from your setup)
# Sunrise 05:30, Sunset 17:30
SUNRISE = 5 * 60 + 30
SUNSET = 17 * 60 + 30

# Your SPM (seconds per in-game minute) you were using from Nitrado settings
DAY_SPM = 4.7666667
NIGHT_SPM = 4.045

# Colors
DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

# Persistence (mount your Railway volume so this survives redeploys)
STATE_FILE = "time_state.json"

# ============================================================
# VALIDATION
# ============================================================

def _require_env():
    missing = []
    if not os.getenv("DISCORD_TOKEN"):
        missing.append("DISCORD_TOKEN")
    if not TIME_WEBHOOK_URL:
        missing.append("TIME_WEBHOOK_URL (or WEBHOOK_URL)")
    if not RCON_HOST:
        missing.append("RCON_HOST")
    if not RCON_PORT:
        missing.append("RCON_PORT")
    if not RCON_PASSWORD:
        missing.append("RCON_PASSWORD")
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

_require_env()
RCON_PORT = int(RCON_PORT)

# ============================================================
# STATE
# ============================================================

# state schema:
# {
#   "epoch": float,   # real timestamp when anchor time was true
#   "year": int,
#   "day": int,
#   "hour": int,
#   "minute": int,
#   "second": int     # optional (0..59) anchor seconds
# }
state = None
_time_message_id = None
_last_synced_ts = 0.0
_last_announced_abs_day = None


def _load_state():
    global state
    if not os.path.exists(STATE_FILE):
        state = None
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = None


def _save_state():
    if not state:
        return
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


_load_state()

# ============================================================
# TIME MATH
# ============================================================

def _minute_of_day(hour: int, minute: int) -> int:
    return hour * 60 + minute

def _is_day(minute_of_day: int) -> bool:
    return SUNRISE <= minute_of_day < SUNSET

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
      (minute_of_day, day, year, seconds_into_current_minute, spm_for_current_minute)
    """
    if not state:
        return None

    anchor_epoch = float(state["epoch"])
    anchor_hour = int(state["hour"])
    anchor_minute = int(state["minute"])
    anchor_day = int(state["day"])
    anchor_year = int(state["year"])
    anchor_second = int(state.get("second", 0) or 0)

    # Treat anchor_second as fraction into that in-game minute
    # i.e. if anchor_second=30, we're half-way through the in-game minute at epoch.
    # Convert to real seconds offset within minute using current minute spm.
    anchor_mod = _minute_of_day(anchor_hour, anchor_minute)
    anchor_spm = _spm(anchor_mod)
    anchor_real_offset = (anchor_second / 60.0) * anchor_spm

    elapsed = time.time() - anchor_epoch + anchor_real_offset

    minute_of_day = anchor_mod
    day = anchor_day
    year = anchor_year

    remaining = float(elapsed)

    while True:
        cur_spm = _spm(minute_of_day)
        if remaining >= cur_spm:
            remaining -= cur_spm
            minute_of_day, day, year = _advance_one_minute(minute_of_day, day, year)
            continue
        return minute_of_day, day, year, remaining, cur_spm

def _build_time_embed(minute_of_day: int, day: int, year: int):
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    emoji = "☀️" if _is_day(minute_of_day) else "🌙"
    color = DAY_COLOR if _is_day(minute_of_day) else NIGHT_COLOR
    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    return {"title": title, "color": color}

def _seconds_until_next_round_step(minute_of_day: int, day: int, year: int, seconds_into_minute: float, step: int):
    """
    Real seconds until the next in-game minute where minute_of_day % step == 0.
    If currently on boundary, schedule the NEXT boundary (step minutes ahead).
    """
    m = minute_of_day
    mod = m % step
    mins_to_boundary = (step - mod) if mod != 0 else step

    cur_spm = _spm(m)
    remaining_in_current_minute = max(0.0, cur_spm - seconds_into_minute)
    total = remaining_in_current_minute

    m2 = m
    d2, y2 = day, year
    for _ in range(mins_to_boundary - 1):
        m2, d2, y2 = _advance_one_minute(m2, d2, y2)
        total += _spm(m2)

    return max(0.5, total)

# ============================================================
# RCON (minimal)
# ============================================================

def _rcon_make_packet(req_id: int, ptype: int, body: str) -> bytes:
    data = body.encode("utf-8", errors="ignore") + b"\x00"
    packet = (
        req_id.to_bytes(4, "little", signed=True)
        + ptype.to_bytes(4, "little", signed=True)
        + data
        + b"\x00"
    )
    size = len(packet)
    return size.to_bytes(4, "little", signed=True) + packet

async def rcon_command(command: str, timeout: float = RCON_TIMEOUT_SECONDS) -> str:
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

        # command
        writer.write(_rcon_make_packet(2, 2, command))
        await writer.drain()

        chunks = []
        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                part = await asyncio.wait_for(reader.read(4096), timeout=0.35)
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
            size = int.from_bytes(data[i:i + 4], "little", signed=True)
            i += 4
            if i + size > len(data) or size < 10:
                break
            pkt = data[i:i + size]
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
# GETGAMELOG PARSE (ANY line)
# ============================================================

# Accept:
# "Day 236, 21:49:58 - ..."
# "Day 236, 21:49 - ..."
# "Day 236, 21:49:58: ..." (some servers)
# and we don't care about the rest of the line.
_DAYTIME_RE = re.compile(
    r"Day\s+(\d+),\s+(\d{1,2}):(\d{2})(?::(\d{2}))?",
    re.IGNORECASE
)

def parse_latest_daytime_any(text: str) -> Optional[Tuple[int, int, int, int]]:
    """
    Returns (day, hour, minute, second) from the most recent line containing a Day/Time stamp.
    """
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
        second = int(m.group(4) or 0)
        return day, hour, minute, second
    return None

# ============================================================
# WEBHOOK UPSERT
# ============================================================

async def _upsert_time_webhook(session: aiohttp.ClientSession, embed: dict):
    """
    Creates once, then edits the same message forever.
    """
    global _time_message_id

    # If we have a message id, patch it
    if _time_message_id:
        async with session.patch(
            f"{TIME_WEBHOOK_URL}/messages/{_time_message_id}",
            json={"embeds": [embed]}
        ) as r:
            if r.status == 404:
                _time_message_id = None
            else:
                return

    # Otherwise create it (wait=true returns the message object including id)
    async with session.post(
        TIME_WEBHOOK_URL + ("&" if "?" in TIME_WEBHOOK_URL else "?") + "wait=true",
        json={"embeds": [embed]}
    ) as r:
        data = await r.json(content_type=None)
        if r.status >= 300:
            raise RuntimeError(f"Time webhook post failed: {r.status} {data}")
        _time_message_id = data.get("id")
        if not _time_message_id:
            raise RuntimeError(f"Time webhook response missing id: {data}")

# ============================================================
# SYNC APPLY
# ============================================================

def _wrap_day_diff(day_diff: int) -> int:
    # keep within [-180..180] to handle year wrap
    if day_diff > 180:
        day_diff -= 365
    elif day_diff < -180:
        day_diff += 365
    return day_diff

def _wrap_minute_diff(diff: int) -> int:
    while diff > 720:
        diff -= 1440
    while diff < -720:
        diff += 1440
    return diff

def apply_sync_from_parsed(parsed_day: int, parsed_hour: int, parsed_minute: int, parsed_second: int) -> Tuple[bool, str]:
    """
    Sync by moving the epoch anchor so predicted time aligns to parsed time NOW.
    """
    global state

    if not state:
        return False, "No time set yet. Use /settime first."

    details = _calculate_time_details()
    if not details:
        return False, "No calculated time available."

    cur_mod, cur_day, cur_year, sec_into_min, cur_spm = details
    target_mod = _minute_of_day(parsed_hour, parsed_minute)

    day_diff = _wrap_day_diff(parsed_day - cur_day)
    minute_diff = day_diff * 1440 + (target_mod - cur_mod)
    minute_diff = _wrap_minute_diff(minute_diff)

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {minute_diff} min < {SYNC_DRIFT_MINUTES} min threshold."

    # Instead of trying to perfectly “shift” along variable SPM,
    # we re-anchor the clock to what the server says RIGHT NOW.
    state["epoch"] = time.time()
    state["day"] = int(parsed_day)
    state["hour"] = int(parsed_hour)
    state["minute"] = int(parsed_minute)
    state["second"] = int(parsed_second or 0)

    # Keep year as-is unless day wrap suggests we've crossed the year boundary
    # If parsed day is far “behind” or “ahead”, adjust year accordingly.
    if (parsed_day < 30 and cur_day > 335):
        state["year"] = int(cur_year + 1)
    elif (parsed_day > 335 and cur_day < 30):
        state["year"] = int(max(1, cur_year - 1))
    else:
        state["year"] = int(cur_year)

    _save_state()
    return True, f"Synced from GetGameLog (drift was {minute_diff} min)."

# ============================================================
# LOOPS
# ============================================================

async def _auto_sync_loop():
    global _last_synced_ts
    await asyncio.sleep(3)

    while True:
        try:
            if not state:
                await asyncio.sleep(AUTO_SYNC_SECONDS)
                continue

            now = time.time()
            if (now - _last_synced_ts) < SYNC_COOLDOWN_SECONDS:
                await asyncio.sleep(AUTO_SYNC_SECONDS)
                continue

            text = await rcon_command("GetGameLog", timeout=RCON_TIMEOUT_SECONDS)
            parsed = parse_latest_daytime_any(text)
            if not parsed:
                # Don't spam logs; just wait and try again.
                await asyncio.sleep(AUTO_SYNC_SECONDS)
                continue

            d, h, m, s = parsed
            changed, msg = apply_sync_from_parsed(d, h, m, s)
            if changed:
                _last_synced_ts = time.time()
                print(f"Time auto-sync: {msg}")

        except Exception as e:
            print(f"Time auto-sync error: {e}")

        await asyncio.sleep(AUTO_SYNC_SECONDS)

async def _time_webhook_loop(client: discord.Client):
    global _last_announced_abs_day

    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while True:
            details = _calculate_time_details()
            if not details:
                await asyncio.sleep(5)
                continue

            minute_of_day, day, year, seconds_into_minute, cur_spm = details

            # Update only on round step
            if (minute_of_day % TIME_UPDATE_STEP_MINUTES) == 0:
                embed = _build_time_embed(minute_of_day, day, year)
                try:
                    await _upsert_time_webhook(session, embed)
                except Exception as e:
                    print(f"Time webhook update error: {e}")

                # daily announce
                abs_day = year * 365 + day
                if _last_announced_abs_day is None:
                    _last_announced_abs_day = abs_day
                elif abs_day > _last_announced_abs_day:
                    ch = client.get_channel(ANNOUNCE_CHANNEL_ID)
                    if ch:
                        try:
                            await ch.send(f"📅 **New Solunaris Day** — Day **{day}**, Year **{year}**")
                        except Exception as e:
                            print(f"Daily announce error: {e}")
                    _last_announced_abs_day = abs_day

            sleep_for = _seconds_until_next_round_step(
                minute_of_day, day, year, seconds_into_minute, TIME_UPDATE_STEP_MINUTES
            )
            await asyncio.sleep(sleep_for)

# ============================================================
# PUBLIC API (what main.py imports)
# ============================================================

def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int):
    guild_obj = discord.Object(id=int(guild_id))

    def _is_admin(interaction: discord.Interaction) -> bool:
        try:
            return any(r.id == int(admin_role_id) for r in interaction.user.roles)
        except Exception:
            return False

    @tree.command(name="settime", guild=guild_obj)
    async def settime_cmd(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
        """
        /settime YEAR DAY HOUR MINUTE
        """
        if not _is_admin(interaction):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return

        if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
            await interaction.response.send_message("❌ Invalid values.", ephemeral=True)
            return

        global state
        state = {
            "epoch": time.time(),
            "year": int(year),
            "day": int(day),
            "hour": int(hour),
            "minute": int(minute),
            "second": 0,
        }
        _save_state()
        await interaction.response.send_message("✅ Time set.", ephemeral=True)

    @tree.command(name="sync", guild=guild_obj)
    async def sync_cmd(interaction: discord.Interaction):
        """
        Force a sync from GetGameLog.
        """
        if not _is_admin(interaction):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            text = await rcon_command("GetGameLog", timeout=RCON_TIMEOUT_SECONDS)
            if not text.strip():
                await interaction.followup.send("❌ GetGameLog returned empty output.", ephemeral=True)
                return

            parsed = parse_latest_daytime_any(text)
            if not parsed:
                await interaction.followup.send("❌ No Day/Time found in GetGameLog.", ephemeral=True)
                return

            d, h, m, s = parsed
            changed, msg = apply_sync_from_parsed(d, h, m, s)
            if changed:
                await interaction.followup.send(f"✅ {msg}", ephemeral=True)
            else:
                await interaction.followup.send(f"ℹ️ {msg}", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Sync error: {e}", ephemeral=True)

def run_time_loop(client: discord.Client) -> None:
    """
    Starts background tasks for:
      - time webhook updates
      - auto sync from GetGameLog
    This returns immediately (schedules tasks).
    """
    client.loop.create_task(_time_webhook_loop(client))
    client.loop.create_task(_auto_sync_loop())