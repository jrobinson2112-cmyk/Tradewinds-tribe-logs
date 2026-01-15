import asyncio
import os
import time
import re
import inspect
import discord
from discord import app_commands

# ==========================================================
# CONFIG (env)
# ==========================================================
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))  # where daily message goes (optional)

# Auto sync interval (seconds)
AUTO_SYNC_SECONDS = 600        # 10 minutes

# How often the webhook clock refreshes (seconds)
CLOCK_REFRESH_SECONDS = 30

# ==========================================================
# ASA DAY/NIGHT MODEL
# ==========================================================
# ASA default: 1 in-game day = 60 real minutes
REAL_SECONDS_PER_INGAME_DAY = 3600
INGAME_MINUTES_PER_DAY = 1440
SECONDS_PER_INGAME_MINUTE = REAL_SECONDS_PER_INGAME_DAY / INGAME_MINUTES_PER_DAY

# ==========================================================
# GAME LOG REGEX (for GetGameLog)
# Matches: "Day 221, 22:51:49" anywhere in the line
# ==========================================================
DAYTIME_RE = re.compile(
    r"Day\s+(\d+)\s*,?\s*(\d{1,2})\s*:\s*(\d{2})\s*:\s*(\d{2})",
    re.IGNORECASE,
)

# ==========================================================
# MODULE STATE
# ==========================================================
_state = {
    "epoch_real": None,              # real timestamp representing Day 0, 00:00
    "last_sync_ts": 0.0,             # last time we applied a sync
    "last_day_announced": None,      # last day number announced in channel
}

# RCON callable bound from main.py
_BOUND_RCON = None

# ==========================================================
# COMPAT: allow main.py to bind RCON for slash commands
# ==========================================================
def bind_rcon_for_commands(rcon_cmd):
    """
    main.py calls this so /sync can work even if setup_time_commands wasn't given rcon.
    rcon_cmd can be sync or async.
    """
    global _BOUND_RCON
    _BOUND_RCON = rcon_cmd


# ==========================================================
# INTERNAL HELPERS
# ==========================================================
async def _maybe_call_rcon(rcon_cmd, command: str):
    """
    Supports both sync and async RCON functions.
    """
    if rcon_cmd is None:
        return ""
    try:
        res = rcon_cmd(command)
        if inspect.isawaitable(res):
            res = await res
        return res or ""
    except Exception:
        return ""


def _parse_latest_daytime(text: str):
    """
    Extract the MOST RECENT Day / Time match from GetGameLog output.
    """
    if not text:
        return None

    latest = None
    for m in DAYTIME_RE.finditer(text):
        try:
            d = int(m.group(1))
            h = int(m.group(2))
            mn = int(m.group(3))
            s = int(m.group(4))
            if 0 <= h <= 23 and 0 <= mn <= 59 and 0 <= s <= 59:
                latest = (d, h, mn, s)
        except Exception:
            continue

    return latest


def _apply_sync(day: int, hour: int, minute: int, second: int):
    """
    Adjust epoch so calculated in-game time matches the parsed time *now*.
    """
    total_ingame_minutes = (day * INGAME_MINUTES_PER_DAY) + (hour * 60) + minute
    real_now = time.time()

    epoch_real = real_now - (total_ingame_minutes * SECONDS_PER_INGAME_MINUTE)

    _state["epoch_real"] = epoch_real
    _state["last_sync_ts"] = real_now


def _calculate_current_time():
    """
    Convert real time -> in-game time using epoch.
    Returns (day, hour, minute) or None.
    """
    if _state["epoch_real"] is None:
        return None

    elapsed_real = time.time() - _state["epoch_real"]
    ingame_minutes = int(elapsed_real / SECONDS_PER_INGAME_MINUTE)

    day = ingame_minutes // INGAME_MINUTES_PER_DAY
    minute_of_day = ingame_minutes % INGAME_MINUTES_PER_DAY
    hour = minute_of_day // 60
    minute = minute_of_day % 60

    return day, hour, minute


def _build_time_embed(day: int, hour: int, minute: int):
    emoji = "🌙" if hour < 6 or hour >= 20 else "☀️"
    return {
        "title": f"{emoji} | Solunaris Time",
        "description": f"**{hour:02d}:{minute:02d} | Day {day}**",
        "color": 0xF1C40F,
    }


# ==========================================================
# SLASH COMMANDS
# ==========================================================
def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int | None = None, rcon_command=None):
    """
    Registers /settime and /sync.
    rcon_command optional; if not provided, /sync will use bind_rcon_for_commands().
    """

    @tree.command(
        name="settime",
        description="Manually set the in-game day/hour/minute",
        guild=discord.Object(id=guild_id),
    )
    async def settime(interaction: discord.Interaction, day: int, hour: int, minute: int):
        # Optional admin gate
        if admin_role_id is not None:
            try:
                roles = getattr(interaction.user, "roles", [])
                if not any(getattr(r, "id", None) == admin_role_id for r in roles):
                    await interaction.response.send_message("❌ No permission.", ephemeral=True)
                    return
            except Exception:
                pass

        if day < 0 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
            await interaction.response.send_message("❌ Invalid values.", ephemeral=True)
            return

        _apply_sync(day, hour, minute, 0)
        await interaction.response.send_message("✅ Time manually set.", ephemeral=True)

    @tree.command(
        name="sync",
        description="Sync time using Day/Time from GetGameLog via RCON",
        guild=discord.Object(id=guild_id),
    )
    async def sync(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        rc = rcon_command or _BOUND_RCON
        if rc is None:
            await interaction.followup.send("❌ RCON not available (not bound).", ephemeral=True)
            return

        log = await _maybe_call_rcon(rc, "GetGameLog")
        parsed = _parse_latest_daytime(log)
        if not parsed:
            await interaction.followup.send("❌ No Day/Time found in GetGameLog.", ephemeral=True)
            return

        d, h, mn, s = parsed
        _apply_sync(d, h, mn, s)
        await interaction.followup.send(f"✅ Synced from GetGameLog: Day {d}, {h:02d}:{mn:02d}:{s:02d}", ephemeral=True)

    print("[time_module] ✅ /settime and /sync registered")


# ==========================================================
# MAIN LOOP
# ==========================================================
async def run_time_loop(client: discord.Client, rcon_command, webhook_upsert):
    """
    - Updates the time embed regularly
    - Auto-syncs from GetGameLog every 10 mins (AUTO_SYNC_SECONDS)
    - Posts daily message once per new day (if ANNOUNCE_CHANNEL_ID is set)
    """
    await client.wait_until_ready()

    announce_channel = client.get_channel(ANNOUNCE_CHANNEL_ID) if ANNOUNCE_CHANNEL_ID else None

    # Initial sync attempt on startup
    try:
        log = await _maybe_call_rcon(rcon_command, "GetGameLog")
        parsed = _parse_latest_daytime(log)
        if parsed:
            _apply_sync(*parsed)
        else:
            print("[time_module] Auto-sync: No parsable Day/Time found in GetGameLog.")
    except Exception:
        print("[time_module] Auto-sync startup failed.")

    last_clock_update = 0.0

    while True:
        now = time.time()

        # Auto-sync every 10 minutes
        if rcon_command and (now - _state["last_sync_ts"]) >= AUTO_SYNC_SECONDS:
            try:
                log = await _maybe_call_rcon(rcon_command, "GetGameLog")
                parsed = _parse_latest_daytime(log)
                if parsed:
                    _apply_sync(*parsed)
                else:
                    print("[time_module] Auto-sync: No parsable Day/Time found in GetGameLog.")
            except Exception:
                print("[time_module] Auto-sync error.")

        # Clock update
        if (now - last_clock_update) >= CLOCK_REFRESH_SECONDS:
            cur = _calculate_current_time()
            if cur:
                day, hour, minute = cur

                # Daily message once per day boundary
                if announce_channel:
                    if _state["last_day_announced"] is None:
                        _state["last_day_announced"] = day
                    elif day > _state["last_day_announced"]:
                        try:
                            await announce_channel.send(f"🌅 **A new day has begun — Day {day}**")
                        except Exception:
                            pass
                        _state["last_day_announced"] = day

                embed = _build_time_embed(day, hour, minute)
                try:
                    await webhook_upsert("time", embed)
                except Exception:
                    pass

            last_clock_update = now

        await asyncio.sleep(5)