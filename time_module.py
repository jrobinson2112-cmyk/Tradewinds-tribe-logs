import time
import json
import os
import asyncio
import re
import discord
from discord import app_commands

# ============================================================
# CONFIG
# ============================================================
STATE_FILE = "time_state.json"

# Your SPMs (real seconds per in-game minute)
DAY_SPM = 4.7666667
NIGHT_SPM = 4.045

# Day/night boundaries
SUNRISE_MIN = 5 * 60 + 30   # 05:30
SUNSET_MIN = 17 * 60 + 30   # 17:30

# Only update the time webhook when minute is a round step (00,10,20,...)
TIME_UPDATE_STEP = 10  # in-game minutes

# Auto-sync cadence and thresholds
AUTO_SYNC_INTERVAL = 600        # 10 minutes
SYNC_DRIFT_THRESHOLD = 2        # only correct if drift >= 2 in-game minutes
MAX_SYNC_JUMP_MINUTES = 60      # safety cap: never jump more than this many minutes per sync

# Daily message
ANNOUNCE_CHANNEL_ID = 1430388267446042666

# Permissions for /settime and /sync
ADMIN_ROLE_ID = 1439069787207766076

# ============================================================
# MODULE STATE
# ============================================================
state = None
last_announced_abs_day = None
_last_auto_sync_ts = 0.0

# ============================================================
# STATE HELPERS
# ============================================================
def _log(msg: str):
    print(f"[time_module] {msg}")

def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)

def _load_last_announced():
    """
    If present, store last announced absolute day in state file too.
    """
    global last_announced_abs_day
    if not state:
        return
    val = state.get("last_announced_abs_day")
    if isinstance(val, int):
        last_announced_abs_day = val

def _save_last_announced(abs_day: int):
    global last_announced_abs_day, state
    last_announced_abs_day = abs_day
    if state:
        state["last_announced_abs_day"] = abs_day
        save_state(state)

state = load_state()
if state:
    _load_last_announced()

# ============================================================
# TIME MATH
# ============================================================
def is_day(minute_of_day: int) -> bool:
    return SUNRISE_MIN <= minute_of_day < SUNSET_MIN

def spm(minute_of_day: int) -> float:
    return DAY_SPM if is_day(minute_of_day) else NIGHT_SPM

def advance_minute(minute_of_day: int, day: int, year: int):
    minute_of_day += 1
    if minute_of_day >= 1440:
        minute_of_day = 0
        day += 1
        if day > 365:
            day = 1
            year += 1
    return minute_of_day, day, year

def calculate_time():
    """
    Returns: (minute_of_day:int, day:int, year:int, seconds_into_current_minute:float)
    """
    global state
    if not state:
        return None

    elapsed = float(time.time() - float(state["epoch"]))

    minute_of_day = int(state["hour"]) * 60 + int(state["minute"])
    day = int(state["day"])
    year = int(state["year"])

    remaining = elapsed
    while True:
        cur_spm = spm(minute_of_day)
        if remaining >= cur_spm:
            remaining -= cur_spm
            minute_of_day, day, year = advance_minute(minute_of_day, day, year)
        else:
            return minute_of_day, day, year, remaining

def build_time_embed(minute_of_day: int, day: int, year: int):
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    emoji = "☀️" if is_day(minute_of_day) else "🌙"
    return {
        "title": f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}",
        "color": 0xF1C40F if is_day(minute_of_day) else 0x5865F2,
    }

def minute_of_day(h: int, m: int) -> int:
    return h * 60 + m

def clamp_day_diff(parsed_day: int, cur_day: int) -> int:
    """
    Handle wrap-around. Keep day diff in [-182..+182] range.
    """
    dd = parsed_day - cur_day
    if dd > 182:
        dd -= 365
    elif dd < -182:
        dd += 365
    return dd

# ============================================================
# GAMELOG PARSING
# ============================================================
# Matches: "Day 216, 18:13:36:" or "Day 216, 18:13:36"
DAYTIME_RE = re.compile(r"Day\s+(\d+),\s*(\d{1,2}):(\d{2}):(\d{2})")

def parse_latest_day_time_from_gamelog(text: str):
    """
    Returns (day, hour, minute, second) from latest line containing "Day X, HH:MM:SS"
    """
    if not text:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = DAYTIME_RE.search(ln)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return None
    # ============================================================
# SYNC ENGINE
# ============================================================
async def _sync_using_gamelog(rcon_command, *, reason: str, force: bool = False):
    """
    Sync by re-anchoring epoch/day/hour/minute based on latest gamelog timestamp.
    Safety:
      - Won’t correct small drift unless force=True
      - Won’t jump more than MAX_SYNC_JUMP_MINUTES
    """
    global state

    if not state:
        return False, "No time state set yet. Use /settime first."

    log_text = await rcon_command("GetGameLog", timeout=8.0)
    parsed = parse_latest_day_time_from_gamelog(log_text)
    if not parsed:
        return False, "No parsable Day/Time found in GetGameLog."

    p_day, p_h, p_m, p_s = parsed

    cur = calculate_time()
    if not cur:
        return False, "Could not calculate current time from state."

    cur_mod, cur_day, cur_year, _ = cur
    cur_h = cur_mod // 60
    cur_m = cur_mod % 60

    # diff in in-game minutes (including day wrap logic)
    dd = clamp_day_diff(p_day, cur_day)
    diff_minutes = dd * 1440 + (minute_of_day(p_h, p_m) - minute_of_day(cur_h, cur_m))

    if not force and abs(diff_minutes) < SYNC_DRIFT_THRESHOLD:
        return False, f"Drift {diff_minutes} min (< {SYNC_DRIFT_THRESHOLD}); no sync needed."

    if abs(diff_minutes) > MAX_SYNC_JUMP_MINUTES:
        return False, f"Safety cap: drift {diff_minutes} min exceeds MAX_SYNC_JUMP_MINUTES={MAX_SYNC_JUMP_MINUTES}. No change applied."

    # Re-anchor: we treat parsed time as "now"
    state["epoch"] = time.time()
    state["day"] = int(p_day)
    state["hour"] = int(p_h)
    state["minute"] = int(p_m)

    # keep year as-is unless you want year derived from gamelog (not present)
    save_state(state)

    return True, f"Synced ({reason}) — drift corrected by {diff_minutes} min. Now Day {p_day} {p_h:02d}:{p_m:02d}:{p_s:02d}."

async def _auto_sync_tick(rcon_command):
    global _last_auto_sync_ts
    now = time.time()
    if now - _last_auto_sync_ts < AUTO_SYNC_INTERVAL:
        return
    changed, msg = await _sync_using_gamelog(rcon_command, reason="auto-sync", force=False)
    _log(msg)
    if changed:
        _last_auto_sync_ts = now

# ============================================================
# MAIN LOOP
# ============================================================
async def run_time_loop(client, rcon_command, webhook_upsert):
    """
    webhook_upsert(key:str, embed:dict) must edit-or-create the webhook message
    rcon_command(cmd:str, timeout:float) -> str
    """
    global last_announced_abs_day

    await client.wait_until_ready()
    _log("✅ time loop started")

    while True:
        if not state:
            await asyncio.sleep(5)
            continue

        calc = calculate_time()
        if not calc:
            await asyncio.sleep(5)
            continue

        mod, day, year, _sec_into = calc

        # Time webhook update ONLY on round boundaries (00,10,20,...)
        if mod % TIME_UPDATE_STEP == 0:
            embed = build_time_embed(mod, day, year)
            try:
                await webhook_upsert("time", embed)
            except Exception as e:
                _log(f"Webhook update error: {e}")

            # Daily message when day increments
            abs_day = int(year) * 365 + int(day)
            if last_announced_abs_day is None:
                _save_last_announced(abs_day)
            elif abs_day > last_announced_abs_day:
                ch = client.get_channel(ANNOUNCE_CHANNEL_ID)
                if ch:
                    try:
                        await ch.send(f"📅 **New Solunaris Day** — Day **{day}**, Year **{year}**")
                    except Exception as e:
                        _log(f"Daily announce send failed: {e}")
                _save_last_announced(abs_day)

        # Auto-sync every 10 minutes (real time), if drift is meaningful
        try:
            await _auto_sync_tick(rcon_command)
        except Exception as e:
            _log(f"Auto-sync error: {e}")

        await asyncio.sleep(30)

# ============================================================
# SLASH COMMANDS
# ============================================================
def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, rcon_command):
    guild_obj = discord.Object(id=guild_id)

    def _is_admin(interaction: discord.Interaction) -> bool:
        return any(getattr(r, "id", None) == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", []))

    @tree.command(name="settime", guild=guild_obj)
    async def settime_cmd(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
        if not _is_admin(i):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
            await i.response.send_message("❌ Invalid values.", ephemeral=True)
            return

        global state
        state = {
            "epoch": time.time(),
            "year": int(year),
            "day": int(day),
            "hour": int(hour),
            "minute": int(minute),
            "last_announced_abs_day": state.get("last_announced_abs_day") if state else None,
        }
        save_state(state)

        await i.response.send_message("✅ Time set.", ephemeral=True)

    @tree.command(name="sync", guild=guild_obj)
    async def sync_cmd(i: discord.Interaction):
        """
        Manual sync RIGHT NOW (ignores drift threshold).
        """
        if not _is_admin(i):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        await i.response.defer(ephemeral=True)

        try:
            changed, msg = await _sync_using_gamelog(rcon_command, reason="manual /sync", force=True)
            await i.followup.send(("✅ " if changed else "ℹ️ ") + msg, ephemeral=True)
        except Exception as e:
            await i.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

    _log("✅ /settime and /sync registered")