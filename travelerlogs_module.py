import os
import discord
from discord import app_commands

# ================
# CONFIG
# ================
# Allow in ONE test channel for now:
TEST_CHANNEL_ID = int(os.getenv("TRAVELERLOG_TEST_CHANNEL_ID", "1462402354535075890"))

# Later: if you want to allow an entire category, set this env var (optional)
# and the command will work in ANY channel under that category.
TRAVELERLOG_CATEGORY_ID = os.getenv("TRAVELERLOG_CATEGORY_ID")
TRAVELERLOG_CATEGORY_ID = int(TRAVELERLOG_CATEGORY_ID) if TRAVELERLOG_CATEGORY_ID else None

# Modal max: Discord UI max is 4000
LOG_MAX_CHARS = int(os.getenv("TRAVELERLOG_MAX_CHARS", "4000"))

# Embed chunk size: embed description max is 4096
EMBED_CHUNK = 3800  # keep some buffer for formatting

EMBED_COLOR = 0xF1C40F  # gold-ish


def _allowed_channel(channel: discord.abc.GuildChannel) -> bool:
    """
    True if this channel is permitted for /writelog.
    - If TRAVELERLOG_CATEGORY_ID is set: any channel in that category
    - Else: only TEST_CHANNEL_ID
    """
    if TRAVELERLOG_CATEGORY_ID is not None:
        try:
            return getattr(channel, "category_id", None) == TRAVELERLOG_CATEGORY_ID
        except Exception:
            return False
    return getattr(channel, "id", None) == TEST_CHANNEL_ID


def _chunk_text(s: str, chunk_size: int):
    s = s or ""
    s = s.strip()
    if not s:
        return [""]
    out = []
    i = 0
    while i < len(s):
        out.append(s[i : i + chunk_size])
        i += chunk_size
    return out


class TravelerLogModal(discord.ui.Modal, title="Traveler Log Entry"):
    day = discord.ui.TextInput(
        label="Day",
        placeholder="e.g. 294",
        required=True,
        max_length=6,
    )
    year = discord.ui.TextInput(
        label="Year",
        placeholder="e.g. 1",
        required=True,
        max_length=6,
    )
    log = discord.ui.TextInput(
        label="Log",
        placeholder="Write your traveler log entry here...",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=LOG_MAX_CHARS,  # up to 4000
    )

    def __init__(self):
        super().__init__()
        self.result = None

    async def on_submit(self, interaction: discord.Interaction):
        day_txt = str(self.day.value).strip()
        year_txt = str(self.year.value).strip()
        log_txt = str(self.log.value).strip()

        if not day_txt.isdigit():
            await interaction.response.send_message("❌ Day must be numeric.", ephemeral=True)
            return
        if not year_txt.isdigit():
            await interaction.response.send_message("❌ Year must be numeric.", ephemeral=True)
            return

        day = int(day_txt)
        year = int(year_txt)
        if day < 1 or day > 365:
            await interaction.response.send_message("❌ Day must be between 1 and 365.", ephemeral=True)
            return
        if year < 1:
            await interaction.response.send_message("❌ Year must be 1 or higher.", ephemeral=True)
            return

        self.result = (year, day, log_txt)
        await interaction.response.defer(ephemeral=True)


def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int):
    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(name="writelog", guild=guild_obj, description="Write a Traveler Log entry (Day/Year required).")
    async def writelog_cmd(interaction: discord.Interaction):
        # Must be used in a guild text channel
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message("❌ This command can only be used in the server.", ephemeral=True)
            return

        if not _allowed_channel(interaction.channel):
            if TRAVELERLOG_CATEGORY_ID is not None:
                await interaction.response.send_message(
                    "❌ Please use /writelog inside the Traveler Logs category channels.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"❌ Please use /writelog in the test channel (ID {TEST_CHANNEL_ID}).",
                    ephemeral=True,
                )
            return

        modal = TravelerLogModal()
        await interaction.response.send_modal(modal)

        # Wait for modal submit
        timed_out = await modal.wait()
        if timed_out or not modal.result:
            return

        year, day, log_txt = modal.result

        # Send to the SAME channel the command was used in
        channel = interaction.channel

        # Embed(s)
        chunks = _chunk_text(log_txt, EMBED_CHUNK)
        embeds = []

        for idx, chunk in enumerate(chunks, start=1):
            title = f"📜 Traveler Log — Year {year}, Day {day}"
            if len(chunks) > 1:
                title += f" (Part {idx}/{len(chunks)})"

            emb = discord.Embed(
                title=title,
                description=chunk if chunk else "*No text provided.*",
                color=EMBED_COLOR,
            )
            emb.set_footer(text=f"By {interaction.user.display_name}")
            embeds.append(emb)

        # Post (multiple embeds if needed)
        try:
            for emb in embeds:
                await channel.send(embed=emb)
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to post in this channel.", ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to post log: {e}", ephemeral=True)
            return

        await interaction.followup.send("✅ Traveler Log posted.", ephemeral=True)

    print("[travelerlogs_module] ✅ /writelog registered")