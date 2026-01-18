# travelerlogs_module.py
# Traveler logs with automatic Year/Day pulled LIVE from time_module
# + Button-only "Write Log" pinned at bottom
# + Edit button on each log (only author can edit)
# + Category lock enforcement (delete normal text)

import os
import time
import asyncio
from typing import Optional, Tuple, Dict

import discord
from discord import app_commands

import time_module  # uses time_module._calc_now() for LIVE year/day


# =====================
# CONFIG
# =====================
TRAVELERLOG_EMBED_COLOR = 0x8B5CF6  # purple
TRAVELERLOG_TITLE = "📖 Traveler Log"

# Lock enforcement: delete normal user text in this category
LOCK_CATEGORY_ID = int(os.getenv("TRAVELERLOGS_LOCK_CATEGORY_ID", "0"))  # set this env var

# Controls message (Write Log button) text
CONTROLS_TITLE = "🖋️ Write a Traveler Log"
CONTROLS_DESC = "Tap the button below to write a Traveler Log.\n\n**Tap the button • A form will open**"

# Custom IDs
CID_WRITE = "travlog:write"
CID_EDIT_PREFIX = "travlog:edit:"  # + message_id

# Store who owns which log message (so only author can edit)
# message_id -> author_id
_LOG_OWNERS: Dict[int, int] = {}


# =====================
# TIME PULL (FIXED)
# =====================
def _get_current_year_day() -> Tuple[int, int]:
    """
    Best-effort: pull Year/Day from the *live* time system.
    Priority:
      1) time_module._calc_now() (live computed time)
      2) time_module._state / time_module.load_state() (anchor)
      3) fallback (1,1)
    """
    # 1) Live computed time
    try:
        calc = getattr(time_module, "_calc_now", None)
        if callable(calc):
            res = calc()
            # expected: (minute_of_day, day, year, seconds_into_minute)
            if res and len(res) >= 3:
                day = int(res[1])
                year = int(res[2])
                if year > 0 and day > 0:
                    return year, day
    except Exception:
        pass

    # 2) Anchor state if available
    try:
        # Ensure state loaded if module supports it
        if getattr(time_module, "_state", None) is None:
            loader = getattr(time_module, "load_state", None)
            if callable(loader):
                loader()

        st = getattr(time_module, "_state", None)
        if isinstance(st, dict):
            year = int(st.get("year", 1))
            day = int(st.get("day", 1))
            if year > 0 and day > 0:
                return year, day
    except Exception:
        pass

    return 1, 1


# =====================
# EMBED BUILDERS
# =====================
def _build_log_embed(author_name: str, title: str, entry: str) -> discord.Embed:
    year, day = _get_current_year_day()

    embed = discord.Embed(
        title=TRAVELERLOG_TITLE,
        color=TRAVELERLOG_EMBED_COLOR,
    )

    embed.add_field(
        name="🗓️ Solunaris Time",
        value=f"**Year {year} • Day {day}**",
        inline=False,
    )

    # Title + body
    if title.strip():
        embed.add_field(name=title.strip(), value=entry or " ", inline=False)
    else:
        embed.add_field(name="Entry", value=entry or " ", inline=False)

    embed.set_footer(text=f"Logged by {author_name}")
    return embed


def _build_controls_embed() -> discord.Embed:
    return discord.Embed(
        title=CONTROLS_TITLE,
        description=CONTROLS_DESC,
        color=0x2F3136,
    )


# =====================
# DISCORD UI (Views/Modals)
# =====================
class WriteLogModal(discord.ui.Modal, title="Write a Traveler Log"):
    log_title = discord.ui.TextInput(
        label="Title",
        placeholder="Short title for your log entry",
        required=True,
        max_length=100,
    )
    entry = discord.ui.TextInput(
        label="Log",
        placeholder="Write your log here...",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=4000,
    )

    def __init__(self):
        super().__init__(timeout=600)

    async def on_submit(self, interaction: discord.Interaction):
        embed = _build_log_embed(
            author_name=interaction.user.display_name,
            title=str(self.log_title),
            entry=str(self.entry),
        )

        # Per-log Edit button
        view = discord.ui.View(timeout=None)
        btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Edit Log",
            emoji="✏️",
            custom_id=f"{CID_EDIT_PREFIX}PENDING",  # replaced after send
        )
        view.add_item(btn)

        msg = await interaction.channel.send(embed=embed, view=view)

        # Track ownership + update edit button custom_id to include message_id
        _LOG_OWNERS[msg.id] = interaction.user.id
        view2 = discord.ui.View(timeout=None)
        btn2 = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Edit Log",
            emoji="✏️",
            custom_id=f"{CID_EDIT_PREFIX}{msg.id}",
        )
        view2.add_item(btn2)
        try:
            await msg.edit(view=view2)
        except Exception:
            pass

        await interaction.response.send_message("✅ Traveler log recorded.", ephemeral=True)


class EditLogModal(discord.ui.Modal, title="Edit Traveler Log"):
    new_title = discord.ui.TextInput(
        label="Title",
        placeholder="Update title",
        required=True,
        max_length=100,
    )
    new_entry = discord.ui.TextInput(
        label="Log",
        placeholder="Update log text",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=4000,
    )

    def __init__(self, message: discord.Message):
        super().__init__(timeout=600)
        self._message = message

        # Pre-fill from existing embed if possible
        try:
            emb = message.embeds[0] if message.embeds else None
            if emb and emb.fields:
                # fields[0] = Solunaris Time
                # fields[1] = Title/Entry
                if len(emb.fields) >= 2:
                    self.new_title.default = emb.fields[1].name or "Entry"
                    self.new_entry.default = emb.fields[1].value or ""
        except Exception:
            pass

    async def on_submit(self, interaction: discord.Interaction):
        embed = _build_log_embed(
            author_name=interaction.user.display_name,
            title=str(self.new_title),
            entry=str(self.new_entry),
        )

        # Keep the edit button
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="Edit Log",
                emoji="✏️",
                custom_id=f"{CID_EDIT_PREFIX}{self._message.id}",
            )
        )

        await self._message.edit(embed=embed, view=view)
        await interaction.response.send_message("✅ Log updated.", ephemeral=True)


class ControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="Write Log",
                emoji="🖋️",
                custom_id=CID_WRITE,
            )
        )


# =====================
# PUBLIC API used by main.py
# =====================
def register_persistent_views(client: discord.Client):
    # So the button keeps working after restarts
    try:
        client.add_view(ControlsView())
    except Exception:
        pass


async def ensure_controls_in_category(client: discord.Client, category_id: int):
    """
    Ensures each text channel in the category has a pinned "Write Log" controls message
    as the newest message (re-posted on restart if needed).
    """
    if not category_id:
        return

    category = client.get_channel(int(category_id))
    if category is None:
        try:
            category = await client.fetch_channel(int(category_id))
        except Exception:
            return

    if not isinstance(category, discord.CategoryChannel):
        return

    for ch in category.text_channels:
        try:
            # Look for recent bot controls message
            found = False
            async for m in ch.history(limit=30):
                if m.author.bot and m.embeds:
                    if m.embeds[0].title == CONTROLS_TITLE:
                        found = True
                        # ensure pinned
                        try:
                            if not m.pinned:
                                await m.pin(reason="Traveler Log controls")
                        except Exception:
                            pass
                        break

            if not found:
                msg = await ch.send(embed=_build_controls_embed(), view=ControlsView())
                try:
                    await msg.pin(reason="Traveler Log controls")
                except Exception:
                    pass
        except Exception:
            continue


def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int):
    """
    Optional fallback command (still useful on desktop).
    Button-only flow works without this.
    """
    @tree.command(
        name="writelog",
        description="Write a traveler log (auto-stamped with current Year & Day)",
        guild=discord.Object(id=guild_id),
    )
    @app_commands.describe(title="Short title for your log entry", entry="The log text")
    async def writelog(interaction: discord.Interaction, title: str, entry: str):
        await interaction.response.send_modal(WriteLogModal())


async def handle_interaction(interaction: discord.Interaction):
    """
    Called by main.py on_interaction to handle Write/Edit button presses.
    """
    if interaction.type != discord.InteractionType.component:
        return

    cid = interaction.data.get("custom_id") if interaction.data else None
    if not cid:
        return

    if cid == CID_WRITE:
        await interaction.response.send_modal(WriteLogModal())
        return

    if cid.startswith(CID_EDIT_PREFIX):
        # Parse message_id
        try:
            mid_str = cid[len(CID_EDIT_PREFIX):]
            msg_id = int(mid_str)
        except Exception:
            await interaction.response.send_message("❌ Invalid edit reference.", ephemeral=True)
            return

        # Ownership check
        owner_id = _LOG_OWNERS.get(msg_id)
        if owner_id is not None and owner_id != interaction.user.id:
            await interaction.response.send_message("❌ Only the original author can edit this log.", ephemeral=True)
            return

        # Fetch message
        try:
            msg = await interaction.channel.fetch_message(msg_id)
        except Exception:
            await interaction.response.send_message("❌ Could not find that log message.", ephemeral=True)
            return

        # If not tracked (restart), allow only if footer matches the user display name (best-effort)
        if owner_id is None:
            try:
                emb = msg.embeds[0] if msg.embeds else None
                if emb and emb.footer and emb.footer.text:
                    if interaction.user.display_name not in emb.footer.text:
                        await interaction.response.send_message("❌ Only the original author can edit this log.", ephemeral=True)
                        return
            except Exception:
                pass
            _LOG_OWNERS[msg_id] = interaction.user.id

        await interaction.response.send_modal(EditLogModal(msg))
        return


async def enforce_travelerlog_lock(message: discord.Message):
    """
    Deletes normal text messages in the locked category.
    Users can still use the button (and slash commands).
    """
    if message.author.bot:
        return

    if LOCK_CATEGORY_ID and getattr(message.channel, "category_id", None) == LOCK_CATEGORY_ID:
        # Allow slash commands (they may appear as "/" messages in some clients)
        if message.content and message.content.startswith("/"):
            return

        # Otherwise delete
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        except Exception:
            pass