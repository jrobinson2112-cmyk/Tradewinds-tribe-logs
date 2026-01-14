# time_module.py (PART 1/3)
# RCON-only Solunaris time tracking + autosync from GetGameLog + daily announce.
# Works with your existing discord.py Client + CommandTree pattern.

import os
import re
import json
import time
import asyncio
from typing import Optional, Tuple

import aiohttp
import discord
from discord import app_commands

# =========================
# CONFIG (edit if needed)
# =========================

STATE_FILE = os.getenv("TIME_STATE_FILE", "time_state.json")

# Where to announce "new day" messages (text channel)
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "1430388267446042666"))

# Guild + Admin role for /settime
GUILD_ID = int(os.getenv("GUILD_ID", "1430388266393276509"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "1439069787207766076"))

# Time webhook (the embed message that shows current time)
# IMPORTANT: must be a valid Discord webhook URL (not forum-only unless you include thread_id properly)
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()

# Server cap just for display (optional)
PLAYER_CAP = int(os.getenv("PLAYER_CAP", "42"))

# ==============
# RCON settings
# ==============
RCON_HOST = os.getenv("RCON_HOST", "").strip()
RCON_PORT = int(os.getenv("RCON_PORT", "0") or 0)
RCON_PASSWORD = os.getenv("RCON_PASSWORD", "").strip()

# =========================
# Nitrado time multipliers
# =========================
# From your screenshot:
# DayCycleSpeedScale = 5.92
# DayTimeSpeedScale  = 1.85
# NightTimeSpeedScale= 2.18
DAY_CYCLE_SPEED_SCALE = float(os.getenv("DAY_CYCLE_SPEED_SCALE", "5.92"))
DAY_TIME_SPEED_SCALE = float(os.getenv("DAY_TIME_SPEED_SCALE", "1.85"))
NIGHT_TIME_SPEED_SCALE = float(os.getenv("NIGHT_TIME_SPEED_SCALE", "2.18"))

# Nitrado guide default: full cycle = 60 real minutes, day is 05:30->17:30 (12h game),
# night is 17:30->05:30 (12h game).
# We convert that to seconds-per-game-minute (SPM), then apply multipliers.
DEFAULT_FULL_CYCLE_REAL_SECONDS = 60 * 60  # 1 hour real-time by default
DEFAULT_FULL_CYCLE_GAME_MINUTES = 24 * 60  # 1440 game minutes

# Polling + sync
TIME_UPDATE_STEP_MINUTES = int(os.getenv("TIME_UPDATE_STEP_MINUTES", "10"))  # updates at :00/:10/...
GAMELOG_SYNC_SECONDS = int(os.getenv("GAMELOG_SYNC_SECONDS", "120"))
SYNC_DRIFT_MINUTES = int(os.getenv("SYNC_DRIFT_MINUTES", "2"))              # only correct if drift >= this
SYNC_COOLDOWN_SECONDS = int(os.getenv("SYNC_COOLDOWN_SECONDS", "600"))      # at most once per 10 mins

# Sunrise/sunset boundaries (in minutes since midnight)
SUNRISE = 5 * 60 + 30
SUNSET = 17 * 60 + 30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

# =========================
# VALIDATION
# =========================
def _require_env():
    missing = []
    for k in ["RCON_HOST", "RCON_PORT", "RCON_PASSWORD"]:
        if not os.getenv(k):
            missing.append(k)
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

_require_env()

# WEBHOOK_URL is required for the time embed. If you want "time module" without webhook,
# set WEBHOOK_URL="" and it will just not post the time embed (but will still announce new days).
# (We won't hard-crash for that.)

# =========================
# STATE
# =========================
# State anchors predicted time to real time.
# epoch = real timestamp (time.time()) when the stored in-game time was correct.
# year/day/hour/minute = in-game clock at that epoch
_state = None
_time_message_id = None
_last_announced_absolute_day = None
_last_sync_ts = 0.0

def load_state():
    global _state
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

def save_state(s: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f)

load_state()

# =========================
# TIME MODEL (SPM)
# =========================
def is_day(minute_of_day: int) -> bool:
    return SUNRISE <= minute_of_day < SUNSET

def base_spm() -> float:
    # default seconds per game minute across the full day-night cycle
    return DEFAULT_FULL_CYCLE_REAL_SECONDS / DEFAULT_FULL_CYCLE_GAME_MINUTES  # 3600/1440 = 2.5s

def day_spm() -> float:
    # Apply Nitrado scales:
    # DayCycleSpeedScale affects overall cycle pace, DayTimeSpeedScale affects daytime pace.
    # Bigger scale => time moves faster => fewer real seconds per game minute.
    # So we DIVIDE SPM by (DayCycleSpeedScale * DayTimeSpeedScale).
    return base_spm() / (DAY_CYCLE_SPEED_SCALE * DAY_TIME_SPEED_SCALE)

def night_spm() -> float:
    return base_spm() / (DAY_CYCLE_SPEED_SCALE * NIGHT_TIME_SPEED_SCALE)

def spm_for(minute_of_day: int) -> float:
    return day_spm() if is_day(minute_of_day) else night_spm()

def _advance_one_minute(minute_of_day: int, day: int, year: int):
    minute_of_day += 1
    if minute_of_day >= 1440:
        minute_of_day = 0
        day += 1
        if day > 365:
            day = 1
            year += 1
    return minute_of_day, day, year

def calculate_time_details():
    """
    Returns:
      minute_of_day (0..1439),
      day (1..365),
      year (>=1),
      seconds_into_current_minute (float),
      current_minute_spm (float)
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
        cur_spm = spm_for(minute_of_day)
        if remaining >= cur_spm:
            remaining -= cur_spm
            minute_of_day, day, year = _advance_one_minute(minute_of_day, day, year)
            continue
        return minute_of_day, day, year, remaining, cur_spm

def build_time_embed(minute_of_day: int, day: int, year: int) -> dict:
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    emoji = "☀️" if is_day(minute_of_day) else "🌙"
    color = DAY_COLOR if is_day(minute_of_day) else NIGHT_COLOR
    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    return {"title": title, "color": color}

def seconds_until_next_round_step(minute_of_day: int, seconds_into_minute: float, step: int):
    """
    Real seconds until next in-game minute where minute%step==0.
    If currently on boundary, schedule the NEXT boundary (step mins ahead).
    """
    mod = minute_of_day % step
    minutes_to_boundary = (step - mod) if mod != 0 else step

    cur_spm = spm_for(minute_of_day)
    remaining_in_current_minute = max(0.0, cur_spm - seconds_into_minute)
    total = remaining_in_current_minute

    m = minute_of_day
    d = 1
    y = 1
    # We only need SPM for subsequent minutes; minute->minute across day/night boundary matters
    for _ in range(minutes_to_boundary - 1):
        m = (m + 1) % 1440
        total += spm_for(m)

    return max(0.5, total)

# =========================
# WEBHOOK UPSERT (time embed)
# =========================
async def upsert_time_webhook(session: aiohttp.ClientSession, embed: dict):
    """
    Creates a single message (first time) then edits it forever.
    """
    global _time_message_id

    if not WEBHOOK_URL:
        return  # no webhook configured

    # If we have a message_id, PATCH it
    if _time_message_id:
        async with session.patch(
            f"{WEBHOOK_URL}/messages/{_time_message_id}",
            json={"embeds": [embed]},
        ) as r:
            if r.status == 404:
                _time_message_id = None
            else:
                # ignore other statuses; keep going
                return

    # Create new message
    async with session.post(
        WEBHOOK_URL + "?wait=true",
        json={"embeds": [embed]},
    ) as r:
        data = await r.json(content_type=None)
        if r.status >= 300:
            raise RuntimeError(f"Time webhook post failed: {r.status} {data}")
        if "id" not in data:
            raise RuntimeError(f"Time webhook returned unexpected payload: {data}")
        _time_message_id = data["id"]

# =========================
# RCON (minimal)
# =========================
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

async def rcon_command(command: str, timeout: float = 6.0) -> str:
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
            txt = body.decode("utf-8", errors="replace")
            if txt:
                out.append(txt)

        return "".join(out).strip()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
            # time_module.py (PART 2/3)
# GetGameLog parsing + auto-sync logic + /settime slash command

# This part depends on PART 1 being present above it.

# =========================
# GAMELOG PARSING
# =========================
# Accept ANY timestamp format we’ve seen in ASA logs:
# Examples:
#   Day 123, 14:37:52:
#   Day 5, 07:04:01:
#   [2026.01.13-20.11.44] Day 44, 18:02:09:
# We deliberately do NOT depend on tribe name here.

_DAYTIME_RE = re.compile(
    r"Day\s+(\d+)\s*,\s*(\d{1,2})\s*:\s*(\d{2})\s*:\s*(\d{2})",
    re.IGNORECASE,
)

def parse_latest_daytime_from_gamelog(text: str) -> Optional[Tuple[int, int, int, int]]:
    """
    Returns (day, hour, minute, second) from the LAST parsable line in the log.
    """
    if not text:
        return None

    last = None
    for line in text.splitlines():
        m = _DAYTIME_RE.search(line)
        if m:
            last = (
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
                int(m.group(4)),
            )
    return last

def wrap_minute_diff(diff: int) -> int:
    # Keep diff within +- half a day so we don’t jump a full cycle
    while diff > 720:
        diff -= 1440
    while diff < -720:
        diff += 1440
    return diff

def apply_sync(parsed_day: int, parsed_hour: int, parsed_minute: int):
    """
    Adjusts epoch so predicted time aligns with parsed gamelog time.
    Does NOT jump if drift is small.
    """
    global _state, _last_sync_ts

    if not _state:
        return False, "no state"

    now = time.time()
    if now - _last_sync_ts < SYNC_COOLDOWN_SECONDS:
        return False, "cooldown"

    details = calculate_time_details()
    if not details:
        return False, "no calculated time"

    cur_minute_of_day, cur_day, cur_year, _, _ = details
    target_minute_of_day = parsed_hour * 60 + parsed_minute

    # Day diff within same year
    day_diff = parsed_day - cur_day
    if day_diff > 180:
        day_diff -= 365
    elif day_diff < -180:
        day_diff += 365

    minute_diff = day_diff * 1440 + (target_minute_of_day - cur_minute_of_day)
    minute_diff = wrap_minute_diff(minute_diff)

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        return False, f"drift {minute_diff}m (ignored)"

    # Shift epoch so predicted clock jumps to parsed time
    # Positive minute_diff means game time is ahead → move epoch backward
    shift_seconds = 0.0
    step = 1 if minute_diff > 0 else -1
    m = cur_minute_of_day

    for _ in range(abs(minute_diff)):
        if step > 0:
            shift_seconds += spm_for(m)
            m = (m + 1) % 1440
        else:
            m = (m - 1) % 1440
            shift_seconds -= spm_for(m)

    _state["epoch"] -= shift_seconds
    save_state(_state)
    _last_sync_ts = now

    return True, f"synced {minute_diff:+} minutes"

# =========================
# GAMELOG SYNC LOOP
# =========================
async def gamelog_sync_loop():
    """
    Polls GetGameLog via RCON and uses timestamps to auto-correct drift.
    """
    await asyncio.sleep(5)

    while True:
        try:
            log = await rcon_command("GetGameLog")
            parsed = parse_latest_daytime_from_gamelog(log)
            if parsed:
                day, hour, minute, _sec = parsed
                ok, msg = apply_sync(day, hour, minute)
                if ok:
                    print(f"GameLog sync applied: {msg}")
            else:
                print("GameLog sync: no parsable Day/time found")
        except Exception as e:
            print(f"GameLog sync error: {e}")

        await asyncio.sleep(GAMELOG_SYNC_SECONDS)

# =========================
# /settime SLASH COMMAND
# =========================
def setup_time_commands(tree: app_commands.CommandTree):
    @tree.command(
        name="settime",
        description="Manually set Solunaris in-game time (admin only)",
        guild=discord.Object(id=GUILD_ID),
    )
    @app_commands.describe(
        day="In-game day number",
        hour="Hour (0-23)",
        minute="Minute (0-59)",
        year="Optional year (default: current)",
    )
    async def settime(
        interaction: discord.Interaction,
        day: int,
        hour: int,
        minute: int,
        year: Optional[int] = None,
    ):
        # Permission check
        if not any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        global _state
        if year is None:
            year = _state["year"] if _state else 1

        _state = {
            "epoch": time.time(),
            "year": int(year),
            "day": int(day),
            "hour": int(hour),
            "minute": int(minute),
        }
        save_state(_state)

        await interaction.response.send_message(
            f"✅ Time set to **Day {day} {hour:02d}:{minute:02d} (Year {year})**",
            ephemeral=False,
        )
        # time_module.py (PART 3/3)
# Time webhook updater + daily announcement + public start() helper

# This part depends on PART 1 + PART 2 being present above it.

# =========================
# WEBHOOK HELPERS (TIME)
# =========================
async def upsert_webhook_embed(session: aiohttp.ClientSession, url: str, key: str, embed: dict):
    """
    Creates the message once, then edits it forever.
    Requires ?wait=true on the first POST so we get message id back.
    """
    mid = _message_ids.get(key)

    if mid:
        async with session.patch(f"{url}/messages/{mid}", json={"embeds": [embed]}) as r:
            if r.status == 404:
                # message deleted or webhook reset - recreate
                _message_ids[key] = None
                return await upsert_webhook_embed(session, url, key, embed)
            # swallow other non-200s for stability
        return

    # Create new
    post_url = url
    joiner = "&" if "?" in post_url else "?"
    post_url = f"{post_url}{joiner}wait=true"

    async with session.post(post_url, json={"embeds": [embed]}) as r:
        try:
            data = await r.json()
        except Exception:
            txt = await r.text()
            raise RuntimeError(f"Webhook post failed: {r.status} {txt}")

        if r.status >= 400:
            raise RuntimeError(f"Webhook post failed: {r.status} {data}")

        if "id" not in data:
            raise RuntimeError(f"Webhook post returned no id: {data}")

        _message_ids[key] = data["id"]

def build_time_embed(minute_of_day: int, day: int, year: int) -> dict:
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    day_now = is_day(minute_of_day)

    emoji = "☀️" if day_now else "🌙"
    color = DAY_COLOR if day_now else NIGHT_COLOR

    return {
        "description": f"{emoji} | **Solunaris Time** | **{hour:02d}:{minute:02d}** | **Day {day}** | **Year {year}**",
        "color": color,
    }

# =========================
# TIME LOOP (webhook updates)
# =========================
async def time_loop(client: discord.Client):
    """
    - Updates the time webhook every TIME_UPDATE_STEP_MINUTES of in-game time.
    - Posts a daily announcement message when the day increments.
    """
    global _last_announced_absolute_day

    # Wait until Discord is ready
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            details = calculate_time_details()
            if not details:
                await asyncio.sleep(2)
                continue

            minute_of_day, day, year, seconds_into_minute, _cur_spm = details

            # Day announcement (only once per day)
            absolute_day = year * 365 + day
            if _last_announced_absolute_day is None:
                _last_announced_absolute_day = absolute_day
            elif absolute_day > _last_announced_absolute_day:
                ch = client.get_channel(ANNOUNCE_CHANNEL_ID)
                if ch:
                    try:
                        await ch.send(f"📅 **New Solunaris Day** — Day **{day}**, Year **{year}**")
                    except Exception:
                        pass
                _last_announced_absolute_day = absolute_day

            # If on a round update boundary, update webhook
            if (minute_of_day % TIME_UPDATE_STEP_MINUTES) == 0:
                embed = build_time_embed(minute_of_day, day, year)
                try:
                    await upsert_webhook_embed(session, TIME_WEBHOOK_URL, "time", embed)
                except Exception as e:
                    # don't crash the whole loop
                    print(f"Time webhook error: {e}")

            # Sleep until next round boundary (computed in PART 1)
            sleep_for = seconds_until_next_round_step(
                minute_of_day,
                day,
                year,
                seconds_into_minute,
                TIME_UPDATE_STEP_MINUTES,
            )
            await asyncio.sleep(sleep_for)

# =========================
# PUBLIC STARTER
# =========================
def start_time_module(client: discord.Client, tree: app_commands.CommandTree):
    """
    Call this from main.py on_ready():
      setup_time_commands(tree)
      asyncio.create_task(time_loop(client))
      asyncio.create_task(gamelog_sync_loop())
    """
    setup_time_commands(tree)
    asyncio.create_task(time_loop(client))
    asyncio.create_task(gamelog_sync_loop())
    print("✅ time_module started (webhook + /settime + gamelog auto-sync)")
        