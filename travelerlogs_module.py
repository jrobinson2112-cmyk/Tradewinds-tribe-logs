import os
import re
import discord
from discord import app_commands

# Where traveler logs are posted
TRAVELERLOG_CHANNEL_ID = int(os.getenv("TRAVELERLOG_CHANNEL_ID", "1462402354535075890"))

# Optional bounds (keep or remove)
MIN_YEAR = int(os.getenv("TRAVELERLOG_MIN_YEAR", "1"))
MAX_YEAR = int(os.getenv("TRAVELERLOG_MAX_YEAR", "9999"))
MIN_DAY = int(os.getenv("TRAVELERLOG_MIN_DAY", "1"))
MAX_DAY = int(os.getenv("TRAVELERLOG_MAX_DAY", "365"))

_DIGITS_ONLY = re.compile(r"^\d+$")


class TravelerLogModal(discord.ui.Modal, title="Traveler Log"):
    day = discord.ui.TextInput(
        label="Day",
        placeholder="e.g. 294",
        required=True,
        max_length=4,
    )
    year = discord.ui.TextInput(
        label="Year",
        placeholder="e.g. 1",
        required=True,
        max_length=6,
    )
    log = discord.ui.TextInput(
        label="Log",
        placeholder="Write your log entry here...",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=1900,
    )

    def __init__(self, channel_id: int):
        super().__init__()
        self.channel_id = int(channel_id)

    async def on_submit(self, interaction: discord.Interaction):
        day_str = str(self.day.value).strip()
        year_str = str(self.year.value).strip()
        log_text = str(self.log.value).strip()

        # numeric-only validation
        if not _DIGITS_ONLY.match(day_str):
            await interaction.response.send_message("❌ Day must be numbers only.", ephemeral=True)
            return
        if not _DIGITS_ONLY.match(year_str):
            await interaction.response.send_message("❌ Year must be numbers only.", ephemeral=True)
            return

        day_val = int(day_str)
        year_val = int(year_str)

        if not (MIN_DAY <= day_val <= MAX_DAY):
            await interaction.response.send_message(f"❌ Day must be between {MIN_DAY} and {MAX_DAY}.", ephemeral=True)
            return
        if not (MIN_YEAR <= year_val <= MAX_YEAR):
            await interaction.response.send_message(f"❌ Year must be between {MIN_YEAR} and {MAX_YEAR}.", ephemeral=True)
            return
        if not log_text:
            await interaction.response.send_message("❌ Log cannot be empty.", ephemeral=True)
            return

        ch = interaction.client.get_channel(self.channel_id)
        if ch is None:
            await interaction.response.send_message(
                f"❌ I can’t find the Traveler Log channel (ID: {self.channel_id}).",
                ephemeral=True,
            )
            return

        author = interaction.user.display_name

        # Clean, consistent format
        content = (
            f"📜 **Traveler Log**\n"
            f"**Year {year_val} — Day {day_val}**\n"
            f"**By:** {author}\n\n"
            f"{log_text}"
        )

        try:
            await ch.send(content)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don’t have permission to post in that channel. "
                "Give the bot **Send Messages** permission there.",
                ephemeral=True,
            )
            return
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to post log: {e}", ephemeral=True)
            return

        await interaction.response.send_message("✅ Traveler Log posted.", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        try:
            await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)
        except Exception:
            pass


def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int):
    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(name="writelog", guild=guild_obj, description="Post a Traveler Log entry (Day/Year required).")
    async def writelog_cmd(interaction: discord.Interaction):
        modal = TravelerLogModal(TRAVELERLOG_CHANNEL_ID)
        await interaction.response.send_modal(modal)

    print("[travelerlogs_module] ✅ /writelog registered")