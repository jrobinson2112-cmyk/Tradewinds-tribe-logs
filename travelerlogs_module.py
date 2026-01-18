# travelerlogs_module.py
# Button-only Traveler Logs with:
# ✅ Persistent "Write Log" button (survives redeploys)
# ✅ Auto Year/Day pulled from time_module
# ✅ Edit Log button on each posted log (author-only)
# ✅ Lock category so normal messages are deleted (button/embed only)
# ✅ Auto-posts & pins the Write Log panel in each channel in the category (with exclusions)
#
# IMPORTANT:
# - Your main.py must call:
#     travelerlogs_module.register_persistent_views(client)
#     travelerlogs_module.ensure_controls_in_category(client, TRAVELERLOG_CATEGORY_ID)
# - If you redeploy and forget register_persistent_views(), old buttons WILL show "Interaction failed".

import os
import asyncio
from typing import Tuple, Set, Optional

import discord
from discord import app_commands

import time_module  # pulls current Solunaris time

# --- MANUAL PANEL POSTING (admin-only) --------------------------------------

def _is_admin(interaction: discord.Interaction) -> bool:
    # "Administrator" permission is the simplest reliable check
    return bool(getattr(interaction.user.guild_permissions, "administrator", False))


async def post_write_panel_to_channel(channel: discord.TextChannel) -> bool:
    """
    Posts the "Write Log" panel into a channel if it doesn't already exist.
    Returns True if posted, False if already present / couldn't post.
    """
    # Your module should already have these:
    #   - _find_existing_panel_message(channel)
    #   - _build_panel_embed()
    #   - TravelerLogsPanelView()
    try:
        existing = await _find_existing_panel_message(channel)
        if existing:
            return False

        await channel.send(embed=_build_panel_embed(), view=TravelerLogsPanelView())
        return True
    except Exception:
        return False


def setup_manual_panel_commands(tree: app_commands.CommandTree, guild_id: int):
    """
    Adds:
      /postlogbutton        -> posts panel in the current channel
      /postlogbutton_channel channel_id -> posts panel in a specific channel
    """
    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(name="postlogbutton", description="(Admin) Post the Write Log panel in this channel", guild=guild_obj)
    async def postlogbutton(interaction: discord.Interaction):
        if not _is_admin(interaction):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("❌ Must be used in a text channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        ok = await post_write_panel_to_channel(interaction.channel)
        if ok:
            await interaction.followup.send("✅ Panel posted.", ephemeral=True)
        else:
            await interaction.followup.send("ℹ️ Panel already exists here (or I couldn’t post it).", ephemeral=True)

    @tree.command(name="postlogbutton_channel", description="(Admin) Post the Write Log panel to a channel ID", guild=guild_obj)
    @app_commands.describe(channel_id="Target channel ID")
    async def postlogbutton_channel(interaction: discord.Interaction, channel_id: str):
        if not _is_admin(interaction):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            cid = int(channel_id)
        except ValueError:
            await interaction.followup.send("❌ Invalid channel ID.", ephemeral=True)
            return

        ch = interaction.client.get_channel(cid)
        if ch is None:
            try:
                ch = await interaction.client.fetch_channel(cid)
            except Exception:
                ch = None

        if not isinstance(ch, discord.TextChannel):
            await interaction.followup.send("❌ Channel not found or not a text channel.", ephemeral=True)
            return

        ok = await post_write_panel_to_channel(ch)
        if ok:
            await interaction.followup.send(f"✅ Panel posted in <#{cid}>.", ephemeral=True)
        else:
            await interaction.followup.send(f"ℹ️ Panel already exists in <#{cid}> (or I couldn’t post it).", ephemeral=True)

# ---------------------------------------------------------------------------
# =====================
# CONFIG
# =====================
TRAVELERLOG_CATEGORY_ID = int(os.getenv("TRAVELERLOG_CATEGORY_ID", "1434615650890023133"))

# Exclude these channels from having the Write Log panel
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

# If True, we won't delete messages that start with "/" (usually not visible anyway)
ALLOW_COMMAND_MESSAGES = os.getenv("TRAVELERLOG_ALLOW_COMMAND_MESSAGES", "1").lower() in ("1", "true", "yes", "on")

TRAVELERLOG_EMBED_COLOR = 0x8B5CF6  # purple
PANEL_EMBED_COLOR = 0x2F3136

TRAVELERLOG_TITLE = "📖 Traveler Log"
PANEL_TITLE = "🖋️ Write a Traveler Log"
PANEL_DESC = "Tap the button below to write a Traveler Log."

# Persistent custom IDs (do NOT change once live)
CID_WRITE = "travelerlogs:write"
CID_EDIT_PREFIX = "travelerlogs:edit:"  # + <author_id>

# Throttling / backoff to prevent API spam / 429
PANEL_ACTION_DELAY_SECONDS = float(os.getenv("TRAVELERLOG_PANEL_ACTION_DELAY_SECONDS", "1.2"))
PANEL_BACKOFF_START_SECONDS = float(os.getenv("TRAVELERLOG_PANEL_BACKOFF_START_SECONDS", "5"))
PANEL_BACKOFF_MAX_SECONDS = float(os.getenv("TRAVELERLOG_PANEL_BACKOFF_MAX_SECONDS", "120"))


# =====================
# TIME HELPERS
# =====================
def _get_time_state_safely() -> dict:
    """
    Try multiple patterns so it still works if time_module internals change.
    """
    if hasattr(time_module, "get_time_state"):
        try:
            st = time_module.get_time_state()
            if isinstance(st, dict):
                return st
        except Exception:
            pass

    if hasattr(time_module, "load_state"):
        try:
            st = time_module.load_state()
            if isinstance(st, dict):
                return st
        except Exception:
            pass

    st = getattr(time_module, "_state", None)
    if isinstance(st, dict):
        return st

    return {}


def _get_current_day_year() -> Tuple[int, int]:
    st = _get_time_state_safely()
    try:
        year = int(st.get("year", 1))
        day = int(st.get("day", 1))
        if year < 1:
            year = 1
        if day < 1:
            day = 1
        return year, day
    except Exception:
        return 1, 1


# =====================
# EMBEDS
# =====================
def _build_panel_embed() -> discord.Embed:
    return discord.Embed(
        title=PANEL_TITLE,
        description=PANEL_DESC,
        color=PANEL_EMBED_COLOR,
    )


def _build_log_embed(author_name: str, title: str, entry: str, year: int, day: int) -> discord.Embed:
    embed = discord.Embed(title=TRAVELERLOG_TITLE, color=TRAVELERLOG_EMBED_COLOR)

    embed.add_field(
        name="🗓️ Solunaris Time",
        value=f"**Year {year} • Day {day}**",
        inline=False,
    )

    safe_title = (title or "Untitled").strip()[:256]
    safe_entry = (entry or "").strip()
    if not safe_entry:
        safe_entry = "*No text provided.*"

    if len(safe_entry) <= 1024:
        embed.add_field(name=safe_title, value=safe_entry, inline=False)
    else:
        embed.title = f"{TRAVELERLOG_TITLE} — {safe_title[:120]}"
        embed.description = safe_entry[:4000] + ("\n… (truncated)" if len(safe_entry) > 4000 else "")

    embed.set_footer(text=f"Logged by {author_name}")
    return embed


# =====================
# MODALS
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
        max_length=4000,
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

        view = TravelerLogEditView(author_id=interaction.user.id)

        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("✅ Traveler log recorded.", ephemeral=True)


class TravelerLogEditModal(discord.ui.Modal, title="Edit Traveler Log"):
    log_title = discord.ui.TextInput(label="Title", max_length=256, required=True)
    log_entry = discord.ui.TextInput(
        label="Log",
        style=discord.TextStyle.paragraph,
        max_length=4000,
        required=True,
    )

    def __init__(self, message_id: int, author_id: int, original_title: str, original_entry: str):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.author_id = author_id
        self.log_title.default = (original_title or "Untitled")[:256]
        self.log_entry.default = (original_entry or "")[:4000]

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Only the original author can edit this log.", ephemeral=True)
            return

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
# VIEWS / BUTTONS
# =====================
class TravelerLogPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Write Log",
        emoji="🖋️",
        style=discord.ButtonStyle.primary,
        custom_id=CID_WRITE,
    )
    async def write_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TravelerLogWriteModal())


class TravelerLogEditView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=None)
        self.author_id = int(author_id)

        self.add_item(
            discord.ui.Button(
                label="Edit Log",
                emoji="✏️",
                style=discord.ButtonStyle.secondary,
                custom_id=f"{CID_EDIT_PREFIX}{self.author_id}",
            )
        )


# =====================
# PERSISTENT VIEW REGISTRATION (for redeploy survival)
# =====================
def register_persistent_views(client: discord.Client):
    """
    This MUST be called on startup (on_ready) or old buttons will show "Interaction failed".
    """
    client.add_view(TravelerLogPanelView())
    _install_edit_router(client)


def _install_edit_router(client: discord.Client):
    """
    Routes edit-button clicks with custom_id prefix "travelerlogs:edit:<author_id>"
    so old posted logs remain editable after redeploy.
    """
    if getattr(client, "_travelerlogs_edit_router_installed", False):
        return
    client._travelerlogs_edit_router_installed = True

    orig_on_interaction = getattr(client, "on_interaction", None)

    async def _wrapped_on_interaction(interaction: discord.Interaction):
        try:
            if interaction.type == discord.InteractionType.component:
                data = interaction.data or {}
                cid = data.get("custom_id")

                if isinstance(cid, str) and cid.startswith(CID_EDIT_PREFIX):
                    # parse author id
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

                    msg = interaction.message
                    if not msg or not msg.embeds:
                        await interaction.response.send_message("❌ Can't edit: missing embed.", ephemeral=True)
                        return

                    emb = msg.embeds[0]

                    # Extract title/entry from embed
                    original_title = "Untitled"
                    original_entry = ""
                    if emb.fields and len(emb.fields) >= 2:
                        original_title = emb.fields[1].name
                        original_entry = emb.fields[1].value
                    else:
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
            # fall through to original
            pass

        if callable(orig_on_interaction):
            await orig_on_interaction(interaction)

    client.on_interaction = _wrapped_on_interaction


# =====================
# PANEL INSTALLER (what your main.py expects)
# =====================
async def ensure_controls_in_category(client: discord.Client, category_id: int):
    """
    Main.py expects this name.
    Ensures every text channel under category_id has a pinned Write Log panel.
    Uses throttling/backoff to avoid 429 spam.
    """
    await ensure_write_panels(client, category_id)


async def ensure_write_panels(client: discord.Client, category_id: int):
    await client.wait_until_ready()

    # Find category from cache first (avoid fetch spam)
    category = None
    for g in client.guilds:
        ch = g.get_channel(int(category_id))
        if ch is not None:
            category = ch
            break

    if category is None:
        try:
            category = await client.fetch_channel(int(category_id))
        except Exception as e:
            print("[travelerlogs] ❌ category not found:", category_id, e)
            return

    if not isinstance(category, discord.CategoryChannel):
        print("[travelerlogs] ❌ category_id is not a category:", category_id)
        return

    backoff = PANEL_BACKOFF_START_SECONDS

    for ch in category.channels:
        if not isinstance(ch, discord.TextChannel):
            continue
        if ch.id in EXCLUDE_CHANNEL_IDS:
            continue

        try:
            # Look for an existing pinned panel (avoid re-posting)
            pins = await ch.pins()
            panel_msg = None
            for p in pins:
                if p.author and p.author.bot and p.embeds:
                    if p.embeds[0].title == PANEL_TITLE:
                        panel_msg = p
                        break

            if panel_msg is None:
                embed = _build_panel_embed()
                view = TravelerLogPanelView()
                msg = await ch.send(embed=embed, view=view)
                try:
                    await msg.pin(reason="Traveler Log panel")
                except Exception:
                    pass

            # slow down between channels
            await asyncio.sleep(PANEL_ACTION_DELAY_SECONDS)

            # reset backoff after success
            backoff = PANEL_BACKOFF_START_SECONDS

        except discord.HTTPException as e:
            # If you get 429/Cloudflare style responses, back off hard
            print(f"[travelerlogs] ensure panel error in #{getattr(ch,'name','?')}: {e}")
            await asyncio.sleep(backoff)
            backoff = min(PANEL_BACKOFF_MAX_SECONDS, backoff * 2)

        except Exception as e:
            print(f"[travelerlogs] ensure panel error in #{getattr(ch,'name','?')}: {e}")
            await asyncio.sleep(backoff)
            backoff = min(PANEL_BACKOFF_MAX_SECONDS, backoff * 2)


# =====================
# OPTIONAL: SLASH COMMAND FALLBACK
# =====================
def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int):
    # ... your existing /writelog etc ...

    setup_manual_panel_commands(tree, guild_id)  # <-- ADD THIS LINE
    """
    Optional fallback command, even if you're "button-only".
    """
    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(
        name="writelog",
        description="Write a traveler log (auto-stamped with Year & Day)",
        guild=guild_obj,
    )
    @app_commands.describe(title="Short title", entry="The log text")
    async def writelog(interaction: discord.Interaction, title: str, entry: str):
        year, day = _get_current_day_year()
        embed = _build_log_embed(interaction.user.display_name, title, entry, year, day)
        view = TravelerLogEditView(author_id=interaction.user.id)

        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("✅ Traveler log recorded.", ephemeral=True)


# =====================
# LOCK ENFORCEMENT
# =====================
async def enforce_travelerlog_lock(message: discord.Message):
    """
    Deletes normal messages inside the traveler log category.
    Excluded channels are ignored.
    """
    if message.author.bot:
        return
    if not isinstance(message.channel, discord.TextChannel):
        return

    if message.channel.category_id != TRAVELERLOG_CATEGORY_ID:
        return

    if message.channel.id in EXCLUDE_CHANNEL_IDS:
        return

    if ALLOW_COMMAND_MESSAGES and message.content.startswith("/"):
        return

    try:
        await message.delete()
    except discord.Forbidden:
        pass
    except Exception:
        pass