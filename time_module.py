import asyncio
import os
import time
import re
import discord
from discord import app_commands

# ==========================================================
# CONFIG
# ==========================================================

# Channel where the "new day" message is posted
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCE_CHANNEL_ID", "0"))

# Auto sync interval (seconds)
AUTO_SYNC_SECONDS = 600        # 10 minutes

# How often the clock display refreshes
CLOCK_REFRESH_SECONDS = 30

# ==========================================================
# ARK TIME CONSTANTS (ASA DEFAULT)
# ==========================================================
# ASA default: 1 in-game day = 60 real minutes
REAL_SECONDS_PER_INGAME_DAY = 3600
INGAME_MINUTES_PER_DAY = 1440
SECONDS_PER_INGAME_MINUTE = REAL_SECONDS_PER_INGAME_DAY / INGAME_MINUTES_PER_DAY

# ==========================================================
# GAME LOG REGEX (VERY FORGIVING)
# ==========================================================
# Matches:
# Day 221, 22:51:49:
# Day 221 22:51:49
# Day 221,22:51:49 -
DAYTIME_RE = re.compile(
    r"Day\s+(\d+)\s*,?\s*(\d{1,2})\s*:\s*(\d{2})\s*:\s*(\d{2})",
    re.IGNORECASE,
)

# ==========================================================
# STATE
# ==========================================================

_state = {
    "epoch_real": None,     # real timestamp representing Day 0, 00:00
    "last_sync": 0.0,
    "last_day_announced": None,
}

# ==========================================================
# INTERNAL HELPERS
# ==========================================================

def _parse_latest_daytime(text: str):
    """
    Extract the MOST RECENT Day / Time from GetGameLog.
    """
    if not text:
        return None

    latest = None
    for match in DAYTIME_RE.finditer(text):
        try:
            d = int(match.group(1))
            h = int(match.group(2))
            m = int(match.group(3))
            s = int(match.group(4))
            if 0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59:
                latest = (d, h, m, s)
        except Exception:
            continue

    return latest


def _apply_sync(day: int, hour: int, minute: int, second: int):
    """
    Adjust epoch so calculated time matches parsed time RIGHT NOW.
    """
    total_ingame_minutes = (day * INGAME_MINUTES_PER_DAY) + (hour * 60) + minute
    real_now = time.time()

    epoch_real = real_now - (total_ingame_minutes * SECONDS_PER_INGAME_MINUTE)

    _state["epoch_real"] = epoch_real
    _state["last_sync"] = real_now


def _calculate_current_time():
    """
    Convert real time → in-game time using epoch.
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
        "description": f"**{hour:02d}:{minute:02d} | Day {day} | Year 2**",
        "color": 0xF1C40F,
    }

# ==========================================================
# SLASH COMMANDS
# ==========================================================

def setup_time_commands(
    tree: app_commands.CommandTree,
    guild_id: int,
    admin_role_id: int | None = None,
    rcon_command=None,
):

    @tree.command(
        name="settime",
        description="Manually set the in-game time",
        guild=discord.Object(id=guild_id),
    )
    async def settime(
        interaction: discord.Interaction,
        day: int,
        hour: int,
        minute: int,
    ):
        _apply_sync(day, hour, minute, 0)
        await interaction.response.send_message("✅ Time manually set.", ephemeral=True)

    @tree.command(
        name="sync",
        description="Sync time using tribe logs (GetGameLog)",
        guild=discord.Object(id=guild_id),
    )
    async def sync(interaction: discord.Interaction):
        if not rcon_command:
            await interaction.response.send_message("❌ RCON not available.", ephemeral=True)
            return

        log = rcon_command("GetGameLog")
        parsed = _parse_latest_daytime(log)

        if not parsed:
            await interaction.response.send_message(
                "❌ No Day/Time found in GetGameLog.",
                ephemeral=True,
            )
            return

        d, h, m, s = parsed
        _apply_sync(d, h, m, s)

        await interaction.response.send_message("✅ Time synced from tribe logs.", ephemeral=True)

    print("[time_module] ✅ /settime and /sync registered")

# ==========================================================
# MAIN LOOP
# ==========================================================

async def run_time_loop(
    client: discord.Client,
    rcon_command,
    webhook_upsert,
):
    """
    - Keeps the clock running
    - Auto-syncs every 10 minutes
    - Posts ONE message at the start of each new day
    """

    await client.wait_until_ready()

    announce_channel = client.get_channel(ANNOUNCE_CHANNEL_ID)

    # Initial sync attempt on startup
    if rcon_command:
        try:
            log = rcon_command("GetGameLog")
            parsed = _parse_latest_daytime(log)
            if parsed:
                _apply_sync(*parsed)
        except Exception:
            pass

    last_clock_update = 0.0

    while True:
        now = time.time()

        # Auto-sync
        if rcon_command and (now - _state["last_sync"]) >= AUTO_SYNC_SECONDS:
            try:
                log = rcon_command("GetGameLog")
                parsed = _parse_latest_daytime(log)
                if parsed:
                    _apply_sync(*parsed)
            except Exception:
                pass

        # Clock update
        if now - last_clock_update >= CLOCK_REFRESH_SECONDS:
            current = _calculate_current_time()
            if current:
                day, hour, minute = current

                # Daily announcement (ONCE)
                if (
                    _state["last_day_announced"] is not None
                    and day > _state["last_day_announced"]
                    and announce_channel
                ):
                    await announce_channel.send(
                        f"🌅 **A new day has begun — Day {day}**"
                    )

                _state["last_day_announced"] = day

                embed = _build_time_embed(day, hour, minute)
                await webhook_upsert("time", embed)

            last_clock_update = now

        await asyncio.sleep(5)