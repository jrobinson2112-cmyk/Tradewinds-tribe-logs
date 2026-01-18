# travelerlogs_module.py
# Traveler logs with automatic Year/Day pulled from time_module
# Locks an entire category so ONLY /writelog can post

import discord
from discord import app_commands

import time_module  # pulls current Solunaris time

# =====================
# CONFIG
# =====================
TRAVELERLOG_CATEGORY_ID = 1434615650890023133  # 🔒 LOCKED CATEGORY ID

TRAVELERLOG_EMBED_COLOR = 0x8B5CF6  # purple
TRAVELERLOG_TITLE = "📖 Traveler Log"


# =====================
# TIME HELPER
# =====================
def _get_current_day_year() -> tuple[int, int]:
    """
    Pull current Year + Day from the time system.
    Safe fallback if time isn't ready yet.
    """
    try:
        state = time_module.get_time_state()
        year = int(state.get("year", 1))
        day = int(state.get("day", 1))
        return year, day
    except Exception:
        return 1, 1


# =====================
# COMMAND REGISTRATION
# =====================
def setup_travelerlog_commands(
    tree: app_commands.CommandTree,
    guild_id: int,
):
    """
    Registers /writelog
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
            name="🗓️ Solunaris Time",
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

        # Post in the channel where command was used
        await interaction.channel.send(embed=embed)

        await interaction.response.send_message(
            "✅ Traveler log recorded.",
            ephemeral=True,
        )


# =====================
# CATEGORY LOCK ENFORCEMENT
# =====================
async def enforce_travelerlog_lock(message: discord.Message):
    """
    Deletes ALL normal messages in the Traveler Log category.
    Only bot / slash-command messages remain.
    """
    # Ignore bots (including our own embeds)
    if message.author.bot:
        return

    # Ignore DMs
    if not message.guild:
        return

    # Only enforce inside the locked category
    category_id = getattr(message.channel, "category_id", None)
    if category_id is None:
        return

    if int(category_id) != int(TRAVELERLOG_CATEGORY_ID):
        return

    # Allow slash-command invocations (they don't leave real text anyway)
    if message.content.startswith("/"):
        return

    # Delete any normal user text
    try:
        await message.delete()
    except discord.Forbidden:
        pass