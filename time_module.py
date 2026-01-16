import os
import asyncio
import time
import re
from typing import Optional, Tuple

import discord
from discord import app_commands

# =========================
# CONFIG
# =========================

TIME_CHANNEL_ID = int(os.getenv("TIME_CHANNEL_ID", "0"))
AUTO_SYNC_INTERVAL = int(os.getenv("TIME_AUTOSYNC_SECONDS", "600"))  # 10 minutes
DAY_LENGTH_MINUTES = int(os.getenv("ARK_DAY_LENGTH_MINUTES", "1440"))  # default ARK

# =========================
# INTERNAL STATE
# =========================

_state = {
    "year": 1,
    "day": 1,
    "minute_of_day": 0,
    "last_real_ts": None,
    "last_announced_day": None,
}

_client: Optional[discord.Client] = None
_webhook_upsert = None

# Regex that matches CLEANED tribe log lines
# Example:
# Day 297, 00:16:23 - Einar claimed Baby Deinosuchus - Lvl 225 (Deinosuchus)
TRIBELOG_TIME_RE = re.compile(
    r"Day\s+(\d+)[,\s]+(\d{2}):(\d{2})(?::(\d{2}))?"
)

# =========================
# TIME HELPERS
# =========================

def _now() -> float:
    return time.time()


def _is_daytime(minute_of_day: int) -> bool:
    # ARK default: day roughly 05:30 → 19:30
    return 330 <= minute_of_day < 1170


def _advance_time():
    if _state["last_real_ts"] is None:
        _state["last_real_ts"] = _now()
        return

    delta_real = _now() - _state["last_real_ts"]
    delta_minutes = int(delta_real / 60)

    if delta_minutes <= 0:
        return

    _state["last_real_ts"] = _now()
    _state["minute_of_day"] += delta_minutes

    while _state["minute_of_day"] >= DAY_LENGTH_MINUTES:
        _state["minute_of_day"] -= DAY_LENGTH_MINUTES
        _state["day"] += 1

        if _state["day"] > 365:
            _state["day"] = 1
            _state["year"] += 1


def _format_time_line() -> str:
    hour = _state["minute_of_day"] // 60
    minute = _state["minute_of_day"] % 60
    icon = "☀️" if _is_daytime(_state["minute_of_day"]) else "🌙"

    return (
        f"{icon} **Solunaris Time — "
        f"Year {_state['year']} | "
        f"Day {_state['day']} | "
        f"{hour:02d}:{minute:02d}**"
    )


# =========================
# TRIBE LOG PARSING
# =========================

def extract_time_from_tribelog(text: str) -> Optional[Tuple[int, int, int]]:
    """
    Returns (day, hour, minute) if found.
    """
    m = TRIBELOG_TIME_RE.search(text)
    if not m:
        return None

    day = int(m.group(1))
    hour = int(m.group(2))
    minute = int(m.group(3))

    return day, hour, minute
    # =========================
# SYNC + LOOP
# =========================

async def sync_from_tribelogs():
    """
    Pulls most recent tribelog messages already posted to Discord
    and extracts Day/Time from them.
    """
    if not _client:
        return False

    for channel in _client.get_all_channels():
        if not isinstance(channel, discord.TextChannel):
            continue

        if "tribelog" not in channel.name.lower():
            continue

        async for msg in channel.history(limit=15):
            if not msg.embeds:
                continue

            for embed in msg.embeds:
                content = embed.description or embed.title or ""
                parsed = extract_time_from_tribelog(content)
                if parsed:
                    day, hour, minute = parsed
                    _state["day"] = day
                    _state["minute_of_day"] = hour * 60 + minute
                    _state["last_real_ts"] = _now()
                    print("[time_module] Synced time from tribe logs")
                    return True

    return False


async def _post_time():
    if not _webhook_upsert:
        return

    await _webhook_upsert(
        "time",
        {
            "description": _format_time_line(),
            "color": 0xF1C40F,
        },
    )


async def run_time_loop(client: discord.Client, rcon_command=None, webhook_upsert=None):
    global _client, _webhook_upsert

    _client = client
    _webhook_upsert = webhook_upsert

    await client.wait_until_ready()

    # Initial sync
    await sync_from_tribelogs()
    await _post_time()

    while True:
        _advance_time()

        # Daily announcement
        if _state["day"] != _state["last_announced_day"]:
            _state["last_announced_day"] = _state["day"]
            await _post_time()

        # Periodic auto-sync
        if int(_now()) % AUTO_SYNC_INTERVAL < 2:
            await sync_from_tribelogs()

        await _post_time()
        await asyncio.sleep(60)


# =========================
# COMMANDS
# =========================

def setup_time_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int = None, rcon_command=None):

    @tree.command(
        name="settime",
        description="Set in-game time (Year Day Hour Minute)",
        guild=discord.Object(id=guild_id),
    )
    @app_commands.describe(
        year="In-game year",
        day="In-game day",
        hour="Hour (0–23)",
        minute="Minute (0–59)",
    )
    async def settime(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
        _state["year"] = year
        _state["day"] = day
        _state["minute_of_day"] = hour * 60 + minute
        _state["last_real_ts"] = _now()

        await interaction.response.send_message("✅ Time set.", ephemeral=True)
        await _post_time()

    @tree.command(
        name="sync",
        description="Force sync time from tribe logs",
        guild=discord.Object(id=guild_id),
    )
    async def sync(interaction: discord.Interaction):
        ok = await sync_from_tribelogs()
        if ok:
            await interaction.response.send_message("🔄 Synced from tribe logs.", ephemeral=True)
            await _post_time()
        else:
            await interaction.response.send_message("❌ No Day/Time found in tribe logs.", ephemeral=True)

    print("[time_module] ✅ /settime and /sync registered")