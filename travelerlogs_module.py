# travelerlogs_module.py
# Traveler logs with automatic Year/Day pulled from time_module

import discord
from discord import app_commands
from typing import Optional

import time_module  # ✅ pulls current Solunaris time


# =====================
# CONFIG
# =====================
TRAVELERLOG_EMBED_COLOR = 0x8B5CF6  # purple
TRAVELERLOG_TITLE = "📖 Traveler Log"


# =====================
# HELPERS
# =====================
def _get_current_day_year() -> tuple[int, int]:
    """
    Pull current Year + Day from the time system.
    Falls back safely if time isn't initialised yet.
    """
    try:
        state = time_module.get_time_state()
        year = int(state.get("year", 1))
        day = int(state.get("day", 1))
        return year, day
    except Exception:
        return 1, 1


# =====================
# COMMAND SETUP
# =====================
def setup_travelerlog_commands(
    tree: app_commands.CommandTree,
    guild_id: int,
):
    """
    Registers /writelog command
    """

    @tree.command(
        name="writelog",
        description="Write a traveler log (auto-stamped with current Year & Day)",
        guild=discord.Object(id=guild_id),
    )
    @app_commands.describe(
        title="Short title for your log entry",
        entry="The log text"
    )
    async def writelog(
        interaction: discord.Interaction,
        title: str,
        entry: str,
    ):
        year, day = _get_current_day_year()

        embed = discord.Embed(
            title=TRAVELERLOG_TITLE,
            color=TRAVELERLOG_EMBED_COLOR,
        )

        embed.add_field(
            name=f"🗓️ Solunaris Time",
            value=f"**Year {year} • Day {day}**",
            inline=False,
        )

        embed.add_field(
            name=title,
            value=entry,
            inline=False,
        )

        embed.set_footer(
            text=f"Logged by {interaction.user.display_name}"
        )

        await interaction.channel.send(embed=embed)
        await interaction.response.send_message(
            "✅ Traveler log recorded.",
            ephemeral=True,
        )


# =====================
# OPTIONAL: CHANNEL LOCK ENFORCEMENT
# =====================
async def enforce_travelerlog_lock(message: discord.Message):
    """
    Optional safety: prevent normal messages in traveler-log channels.
    If you don't want this, you can remove calls to this in main.py.
    """
    if message.author.bot:
        return

    # Example rule: only enforce in channels with 'traveler-log' in name
    if "traveler-log" not in message.channel.name.lower():
        return

    # Allow slash commands
    if message.content.startswith("/"):
        return

    try:
        await message.delete()
    except discord.Forbidden:
        pass