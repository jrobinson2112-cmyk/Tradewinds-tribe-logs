# travelerlogs_module.py
# Button-only Traveler Logs with:
# ✅ Persistent "Write Log" button (survives redeploys)
# ✅ Auto Year/Day pulled from time_module (your Solunaris time system)
# ✅ Edit Log button on each posted log (author-only)
# ✅ Lock: deletes normal messages in a specific category (so only embeds/buttons remain)
# ✅ Auto-posts & pins the Write Log panel in every channel in the category, excluding listed channel IDs
#
# HOW TO USE (main.py):
#   import travelerlogs_module
#
#   # in on_ready():
#   travelerlogs_module.register_views(client)  # IMPORTANT (persistent buttons!)
#   travelerlogs_module.setup_travelerlog_commands(tree, GUILD_ID)  # optional (fallback /writelog)
#
#   # to auto-ensure the pinned panel exists in category:
#   asyncio.create_task(travelerlogs_module.ensure_write_panels(client, guild_id=GUILD_ID))
#
#   # in on_message():
#   await travelerlogs_module.enforce_travelerlog_lock(message)
#
# ENV VARS (optional):
#   TRAVELERLOG_CATEGORY_ID=1434615650890023133
#   TRAVELERLOG_EXCLUDE_CHANNEL_IDS=comma,separated,ids
#   TRAVELERLOG_ALLOW_COMMAND_MESSAGES=0   (default 1; allow slash command messages if any are visible)
#
# NOTES:
# - Persistent views require: timeout=None AND fixed custom_id AND client.add_view(...) on startup.
# - If you redeploy and forget register_views(), old buttons WILL show "interaction failed".
#
import os
import re
import asyncio
from typing import Optional, Tuple, Set, List

import discord
from discord import app_commands

import time_module  # must expose get_time_state() or equivalent; we handle safe fallback


# =====================
# CONFIG
# =====================
TRAVELERLOG_CATEGORY_ID = int(os.getenv("TRAVELERLOG_CATEGORY_ID", "1434615650890023133"))

# Exclusions (from your request) + optional env-based extras
_DEFAULT_EXCLUDES = {
    1462539723112321218,
    1437457789164191939,
    1455315150859927663,
    1456386974167466106,
}

_env_excludes = os.getenv("TRAVELERLOG_EXCLUDE_CHANNEL_IDS", "").strip()
if _env_excludes:
    for part in _env_excludes.split(","):
        part = part.strip()
        if part.isdigit():
            _DEFAULT_EXCLUDES.add(int(part))

EXCLUDE_CHANNEL_IDS: Set[int] = set(_DEFAULT_EXCLUDES)

ALLOW_COMMAND_MESSAGES = os.getenv("TRAVELERLOG_ALLOW_COMMAND_MESSAGES", "1").lower() in ("1", "true", "yes", "on")

TRAVELERLOG_EMBED_COLOR = 0x8B5CF6  # purple
PANEL_EMBED_COLOR = 0x2F3136

TRAVELERLOG_TITLE = "📖 Traveler Log"
PANEL_TITLE = "🖋️ Write a Traveler Log"
PANEL_DESC = "Tap the button below to write a Traveler Log.\n\n**Tap the button • A form will open**"

# Persistent custom IDs (do NOT change these once live)
CID_WRITE = "travelerlogs:write"
CID_EDIT_PREFIX = "travelerlogs:edit:"  # + <author_id>

# =====================
# TIME HELPERS
# =====================
def _get_time_state_safely() -> dict:
    """
    Try multiple known patterns so this works even if time_module changed internally.
    """
    # Preferred: time_module.get_time_state() -> dict with year/day/hour/minute
    if hasattr(time_module, "get_time_state"):
        try:
            st = time_module.get_time_state()
            if isinstance(st, dict):
                return st
        except Exception:
            pass

    # Fallback: time_module.load_state() (some builds use this)
    if hasattr(time_module, "load_state"):
        try:
            st = time_module.load_state()
            if isinstance(st, dict):
                return st
        except Exception:
            pass

    # Last fallback: if module has _state global
    st = getattr(time_module, "_state", None)
    if isinstance(st, dict):
        return st

    return {}


def _get_current_day_year() -> Tuple[int, int]:
    st = _get_time_state_safely()
    try:
        year = int(st.get("year", 1))
        day = int(st.get("day", 1))
        # sanity clamp
        if year < 1:
            year = 1
        if day < 1:
            day = 1
        return year, day
    except Exception:
        return 1, 1


# =====================
# EMBED BUILDERS
# =====================
def _build_log_embed(author_name: str, title: str, entry: str, year: int, day: int) -> discord.Embed:
    embed = discord.Embed(
        title=TRAVELERLOG_TITLE,
        color=TRAVELERLOG_EMBED_COLOR,
    )

    embed.add_field(
        name="🗓️ Solunaris Time",
        value=f"**Year {year} • Day {day}**",
        inline=False,
    )

    # Title/entry
    safe_title = title.strip()[:256] if title else "Untitled"
    safe_entry = entry.strip() if entry else ""
    if not safe_entry:
        safe_entry = "*No text provided.*"

    # Discord embed field value max is 1024 chars. Use description for longer text.
    # We'll put title as a field name and entry in the field value if it fits;
    # otherwise use embed description.
    if len(safe_entry) <= 1024:
        embed.add_field(
            name=safe_title,
            value=safe_entry,
            inline=False,
        )
    else:
        # Put title in the embed title line
        embed.title = f"{TRAVELERLOG_TITLE} — {safe_title[:120]}"
        embed.description = safe_entry[:4000] + ("\n… (truncated)" if len(safe_entry) > 4000 else "")

    embed.set_footer(text=f"Logged by {author_name}")
    return embed


def _build_panel_embed() -> discord.Embed:
    return discord.Embed(
        title=PANEL_TITLE,
        description=PANEL_DESC,
        color=PANEL_EMBED_COLOR,
    )


# =====================
# UI: MODALS
# =====================
class TravelerLogWriteModal(discord.ui.Modal, title="Write Traveler Log"):
    log_title = discord.ui.TextInput(
        label="Title",
        placeholder="Short title for your log entry",
        max_length=256,
        required=True,
    )
    log_entry = discord.ui.TextInput(
        label="Log",
        placeholder="Write your traveler log here…",
        style=discord.TextStyle.paragraph,
        max_length=4000,  # modal input limit
        required=True,
    )

    def __init__(self):
        super().__init__(timeout=None)

    async def on_submit(self, interaction: discord.Interaction):
        year, day = _get_current_day_year()

        embed = _build_log_embed(
            author_name=interaction.user.display_name,
            title=str(self.log_title.value),
            entry=str(self.log_entry.value),
            year=year,
            day=day,
        )

        # Add an Edit button that only the author can use (custom_id includes author id)
        view = TravelerLogEditView(author_id=interaction.user.id)

        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("✅ Traveler log recorded.", ephemeral=True)


class TravelerLogEditModal(discord.ui.Modal, title="Edit Traveler Log"):
    log_title = discord.ui.TextInput(
        label="Title",
        placeholder="Update the title",
        max_length=256,
        required=True,
    )
    log_entry = discord.ui.TextInput(
        label="Log",
        placeholder="Update the log text…",
        style=discord.TextStyle.paragraph,
        max_length=4000,
        required=True,
    )

    def __init__(self, message_id: int, author_id: int, original_title: str, original_entry: str):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.author_id = author_id

        # Prefill
        self.log_title.default = (original_title or "Untitled")[:256]
        self.log_entry.default = (original_entry or "")[:4000]

    async def on_submit(self, interaction: discord.Interaction):
        # Author-only
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Only the original author can edit this log.", ephemeral=True)
            return

        # Fetch message to edit (in the same channel)
        try:
            msg = await interaction.channel.fetch_message(self.message_id)
        except Exception:
            await interaction.response.send_message("❌ Could not find that log message to edit.", ephemeral=True)
            return

        year, day = _get_current_day_year()

        embed = _build_log_embed(
            author_name=interaction.user.display_name,
            title=str(self.log_title.value),
            entry=str(self.log_entry.value),
            year=year,
            day=day,
        )

        view = TravelerLogEditView(author_id=self.author_id)

        try:
            await msg.edit(embed=embed, view=view)
            await interaction.response.send_message("✅ Log updated.", ephemeral=True)
        except Exception:
            await interaction.response.send_message("❌ Failed to edit the log (missing permissions?).", ephemeral=True)


# =====================
# UI: VIEWS / BUTTONS
# =====================
class TravelerLogPanelView(discord.ui.View):
    """
    Persistent panel view: pinned in each log channel.
    """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Write Log",
        emoji="🖋️",
        style=discord.ButtonStyle.primary,
        custom_id=CID_WRITE,
    )
    async def write_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Open write modal
        await interaction.response.send_modal(TravelerLogWriteModal())


class TravelerLogEditView(discord.ui.View):
    """
    Per-log view with author-locked Edit button.
    Must be persistent too (timeout=None) so old logs remain editable after redeploy.
    """
    def __init__(self, author_id: int):
        super().__init__(timeout=None)
        self.author_id = int(author_id)

        # Add the button dynamically with an author-specific custom_id
        self.add_item(
            discord.ui.Button(
                label="Edit Log",
                emoji="✏️",
                style=discord.ButtonStyle.secondary,
                custom_id=f"{CID_EDIT_PREFIX}{self.author_id}",
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Let the callback handle exact checks; returning True allows interaction to proceed.
        return True


class TravelerLogPersistentRouter(discord.ui.View):
    """
    A persistent router view that receives ALL edit button clicks (by custom_id prefix)
    and opens the modal to edit the embed.
    This is how we support persistent edit buttons for logs posted in the past.
    """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="(router)",
        style=discord.ButtonStyle.secondary,
        custom_id="travelerlogs:router",  # hidden/unused; required to keep a View instance
        disabled=True,
    )
    async def _noop(self, interaction: discord.Interaction, button: discord.ui.Button):
        # should never be clickable
        await interaction.response.defer(ephemeral=True)

    async def on_timeout(self):
        return

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        try:
            await interaction.response.send_message("❌ Interaction failed.", ephemeral=True)
        except Exception:
            pass

    # We use a global interaction listener in the module instead (below) to route prefix IDs.


# =====================
# PUBLIC: REGISTER VIEWS
# =====================
def register_views(client: discord.Client):
    """
    Must be called ON STARTUP (on_ready) so old pinned buttons still work after redeploy.
    """
    # The panel view with fixed custom_id
    client.add_view(TravelerLogPanelView())

    # We do NOT need to add TravelerLogEditView instances here (they're dynamic),
    # but we DO need to ensure edit interactions are handled. We do that with the
    # interaction listener below (setup_interaction_router).
    # Still, adding a router view doesn't hurt, but isn't sufficient for prefix IDs.


def setup_interaction_router(client: discord.Client):
    """
    Hooks into on_interaction to handle edit button custom_id prefix.
    Call once on startup.
    """
    if getattr(client, "_travelerlogs_router_installed", False):
        return
    client._travelerlogs_router_installed = True

    original_on_interaction = getattr(client, "on_interaction", None)

    async def _wrapped_on_interaction(interaction: discord.Interaction):
        # First, let our router try
        try:
            if interaction.type == discord.InteractionType.component:
                data = interaction.data or {}
                cid = data.get("custom_id")
                if isinstance(cid, str) and cid.startswith(CID_EDIT_PREFIX):
                    # custom_id = travelerlogs:edit:<author_id>
                    try:
                        author_id = int(cid.split(":")[-1])
                    except Exception:
                        author_id = 0

                    if author_id <= 0:
                        await interaction.response.send_message("❌ Invalid edit button.", ephemeral=True)
                        return

                    if interaction.user.id != author_id:
                        await interaction.response.send_message("❌ Only the original author can edit this log.", ephemeral=True)
                        return

                    # Get message being edited
                    msg = interaction.message
                    if not msg or not msg.embeds:
                        await interaction.response.send_message("❌ Can't edit: missing log embed.", ephemeral=True)
                        return

                    emb = msg.embeds[0]

                    # Extract current title & entry from embed
                    # We stored:
                    #  Field 0: "🗓️ Solunaris Time"
                    #  Field 1: <title> -> <entry>  OR embed.description used for long entry
                    original_title = "Untitled"
                    original_entry = ""

                    if emb.fields and len(emb.fields) >= 2:
                        original_title = emb.fields[1].name
                        original_entry = emb.fields[1].value
                    else:
                        # fallback if description used
                        # Title may be "📖 Traveler Log — <title>"
                        if emb.title and "—" in emb.title:
                            original_title = emb.title.split("—", 1)[1].strip()
                        original_entry = emb.description or ""

                    await interaction.response.send_modal(
                        TravelerLogEditModal(
                            message_id=msg.id,
                            author_id=author_id,
                            original_title=original_title,
                            original_entry=original_entry,
                        )
                    )
                    return
        except Exception:
            # Fall through to original handler
            pass

        # If there was an existing on_interaction, call it
        if callable(original_on_interaction):
            await original_on_interaction(interaction)

    client.on_interaction = _wrapped_on_interaction


# =====================
# OPTIONAL: SLASH COMMAND FALLBACK
# =====================
def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int):
    """
    Optional fallback: /writelog title entry
    (You asked for button-only, but leaving this as a safety net.)
    """
    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(
        name="writelog",
        description="Write a traveler log (auto-stamped with Year & Day)",
        guild=guild_obj,
    )
    @app_commands.describe(
        title="Short title for your log entry",
        entry="The log text"
    )
    async def writelog(interaction: discord.Interaction, title: str, entry: str):
        year, day = _get_current_day_year()

        embed = _build_log_embed(
            author_name=interaction.user.display_name,
            title=title,
            entry=entry,
            year=year,
            day=day,
        )

        view = TravelerLogEditView(author_id=interaction.user.id)

        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("✅ Traveler log recorded.", ephemeral=True)


# =====================
# PANEL INSTALLER (AUTO PIN)
# =====================
async def ensure_write_panels(client: discord.Client, guild_id: int):
    """
    Ensures every text channel under TRAVELERLOG_CATEGORY_ID has:
      - a pinned panel message with the Write button
    Skips channels in EXCLUDE_CHANNEL_IDS.
    """
    await client.wait_until_ready()

    guild = client.get_guild(int(guild_id))
    if guild is None:
        try:
            guild = await client.fetch_guild(int(guild_id))
        except Exception:
            print("[travelerlogs] ❌ could not fetch guild")
            return

    category = guild.get_channel(TRAVELERLOG_CATEGORY_ID)
    if category is None:
        try:
            category = await client.fetch_channel(TRAVELERLOG_CATEGORY_ID)
        except Exception:
            print("[travelerlogs] ❌ category not found:", TRAVELERLOG_CATEGORY_ID)
            return

    if not isinstance(category, discord.CategoryChannel):
        print("[travelerlogs] ❌ TRAVELERLOG_CATEGORY_ID is not a category.")
        return

    for ch in category.channels:
        if not isinstance(ch, discord.TextChannel):
            continue
        if ch.id in EXCLUDE_CHANNEL_IDS:
            continue

        try:
            # Check pins for our panel
            pins = await ch.pins()
            panel_msg = None
            for p in pins:
                if p.author and p.author.bot:
                    # Identify by embed title + our custom button present
                    if p.embeds and p.embeds[0].title == PANEL_TITLE:
                        panel_msg = p
                        break

            # If not found, post new panel and pin it
            if panel_msg is None:
                embed = _build_panel_embed()
                view = TravelerLogPanelView()
                msg = await ch.send(embed=embed, view=view)
                try:
                    await msg.pin(reason="Traveler Log panel")
                except Exception:
                    pass
                # (Optional) add a small delay to avoid rate limits on big categories
                await asyncio.sleep(0.3)

        except Exception as e:
            print(f"[travelerlogs] ensure panel error in #{getattr(ch,'name','?')}: {e}")


# =====================
# LOCK ENFORCEMENT
# =====================
async def enforce_travelerlog_lock(message: discord.Message):
    """
    Deletes normal messages inside the traveler log category so people can't post plain text.
    - Allows bot messages
    - Allows interactions (buttons/modals produce messages from the bot)
    - Optionally allows slash command messages (usually not visible anyway)
    """
    if message.author.bot:
        return

    # Must be a guild text channel
    if not isinstance(message.channel, discord.TextChannel):
        return

    # Must be inside the target category
    if message.channel.category_id != TRAVELERLOG_CATEGORY_ID:
        return

    # Excluded channels are not locked
    if message.channel.id in EXCLUDE_CHANNEL_IDS:
        return

    # Allow slash commands if any are visible (usually they aren't)
    if ALLOW_COMMAND_MESSAGES and message.content.startswith("/"):
        return

    # Anything else gets removed
    try:
        await message.delete()
    except discord.Forbidden:
        # Bot lacks Manage Messages in that channel
        pass
    except Exception:
        pass