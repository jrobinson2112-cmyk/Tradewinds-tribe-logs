# travelerlogs_module.py
# Traveler logs with:
# ✅ /writelog (auto-stamped with Year/Day from time_module)
# ✅ "✏️ Edit Log" button (author-only) + modal editor (title + body prefilled)
# ✅ Persistent storage (survives redeploys) via /data/travelerlogs_state.json
# ✅ Category lock: users can ONLY post via /writelog in a specific category (deletes normal text)

import os
import json
import time
import asyncio
from typing import Dict, Any, Optional, Tuple

import discord
from discord import app_commands

import time_module  # must expose get_time_state()

# =====================
# CONFIG
# =====================
TRAVELERLOG_EMBED_COLOR = int(os.getenv("TRAVELERLOG_EMBED_COLOR", "0x8B5CF6"), 16)
TRAVELERLOG_TITLE_PREFIX = os.getenv("TRAVELERLOG_TITLE_PREFIX", "📖 Traveler Log")

# Locking: delete normal messages in this category (users must use /writelog)
LOCK_CATEGORY_ID = int(os.getenv("TRAVELERLOG_LOCK_CATEGORY_ID", "1434615650890023133"))

# Persistence (Railway volume)
DATA_DIR = os.getenv("TRAVELERLOGS_DATA_DIR", "/data")
STATE_FILE = os.getenv("TRAVELERLOGS_STATE_FILE", os.path.join(DATA_DIR, "travelerlogs_state.json"))

# Modal limits
TITLE_MAX = 256
ENTRY_MAX = 4000  # Discord modal TextInput max for long text is 4000

# Button custom_id for persistence
EDIT_BUTTON_CUSTOM_ID = "travelerlog:edit"

# =====================
# INTERNAL STATE
# =====================
# message_id(str) -> record
# record: {"author_id": int, "channel_id": int, "title": str, "entry": str, "year": int, "day": int, "created_ts": float, "updated_ts": float}
_logs: Dict[str, Dict[str, Any]] = {}

# a persistent view instance (registered once)
_persistent_view: Optional[discord.ui.View] = None


# =====================
# PERSISTENCE
# =====================
def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _load_state():
    global _logs
    try:
        if not os.path.exists(STATE_FILE):
            _logs = {}
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("logs"), dict):
            _logs = data["logs"]
        else:
            _logs = {}
    except Exception:
        _logs = {}

def _save_state():
    try:
        _ensure_dir(STATE_FILE)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"logs": _logs}, f)
    except Exception:
        pass


# =====================
# TIME HELPERS
# =====================
def _get_current_day_year() -> Tuple[int, int]:
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
# EMBED HELPERS
# =====================
def _build_embed(author_display: str, title: str, entry: str, year: int, day: int) -> discord.Embed:
    # Keep it clean and readable; store body in description (up to 4096 chars)
    embed = discord.Embed(
        title=f"{TRAVELERLOG_TITLE_PREFIX} — {title[:TITLE_MAX]}",
        description=(entry[:4090] + "…") if len(entry) > 4096 else entry,
        color=TRAVELERLOG_EMBED_COLOR,
    )
    embed.add_field(name="🗓️ Solunaris Time", value=f"**Year {year} • Day {day}**", inline=False)
    embed.set_footer(text=f"Logged by {author_display}")
    return embed


# =====================
# EDIT MODAL + VIEW
# =====================
class TravelerLogEditModal(discord.ui.Modal, title="Edit Traveler Log"):
    def __init__(self, message_id: int, author_id: int, current_title: str, current_entry: str):
        super().__init__(timeout=300)
        self.message_id = int(message_id)
        self.author_id = int(author_id)

        self.title_input = discord.ui.TextInput(
            label="Title",
            style=discord.TextStyle.short,
            required=True,
            max_length=TITLE_MAX,
            default=current_title[:TITLE_MAX] if current_title else "",
        )
        self.entry_input = discord.ui.TextInput(
            label="Log",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=ENTRY_MAX,
            default=current_entry[:ENTRY_MAX] if current_entry else "",
        )

        self.add_item(self.title_input)
        self.add_item(self.entry_input)

    async def on_submit(self, interaction: discord.Interaction):
        # Author-only enforcement
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Only the original author can edit this log.", ephemeral=True)
            return

        mid = str(self.message_id)
        rec = _logs.get(mid)

        # If record missing (shouldn't happen), refuse safely
        if not rec:
            await interaction.response.send_message("❌ I can't find the stored record for this log.", ephemeral=True)
            return

        # Update stored record
        rec["title"] = str(self.title_input.value)
        rec["entry"] = str(self.entry_input.value)
        rec["updated_ts"] = time.time()
        _logs[mid] = rec
        _save_state()

        # Rebuild embed (keep original stamped Year/Day)
        year = int(rec.get("year", 1))
        day = int(rec.get("day", 1))
        new_embed = _build_embed(
            author_display=interaction.user.display_name,
            title=rec["title"],
            entry=rec["entry"],
            year=year,
            day=day,
        )

        try:
            await interaction.message.edit(embed=new_embed, view=TravelerLogView())
        except Exception:
            # If message isn't editable for any reason
            await interaction.response.send_message("❌ Could not edit that message (missing permissions?).", ephemeral=True)
            return

        await interaction.response.send_message("✅ Log updated.", ephemeral=True)


class TravelerLogView(discord.ui.View):
    """
    Persistent edit button.
    IMPORTANT: uses a fixed custom_id so it can work across restarts.
    """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✏️ Edit Log", style=discord.ButtonStyle.secondary, custom_id=EDIT_BUTTON_CUSTOM_ID)
    async def edit_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg = interaction.message
        if msg is None:
            await interaction.response.send_message("❌ Cannot access the message.", ephemeral=True)
            return

        mid = str(msg.id)
        rec = _logs.get(mid)

        # If record missing, try to reconstruct minimal fields from the embed
        if not rec:
            # Attempt best-effort parse
            cur_title = ""
            cur_entry = ""
            author_id = interaction.user.id  # fallback (not ideal)
            if msg.embeds:
                e = msg.embeds[0]
                if e.title:
                    # strip prefix if present
                    cur_title = e.title.replace(f"{TRAVELERLOG_TITLE_PREFIX} — ", "", 1)
                if e.description:
                    cur_entry = e.description
            await interaction.response.send_message(
                "❌ This log wasn't recorded in my database (likely posted before this version). "
                "Re-post it using /writelog to enable editing.",
                ephemeral=True,
            )
            return

        author_id = int(rec.get("author_id", 0))
        if interaction.user.id != author_id:
            await interaction.response.send_message("❌ Only the original author can edit this log.", ephemeral=True)
            return

        modal = TravelerLogEditModal(
            message_id=msg.id,
            author_id=author_id,
            current_title=str(rec.get("title", "")),
            current_entry=str(rec.get("entry", "")),
        )
        await interaction.response.send_modal(modal)


def ensure_persistent_view_registered(client: discord.Client):
    """
    Call this ONCE in on_ready to ensure the persistent button works across restarts.
    """
    global _persistent_view
    if _persistent_view is None:
        _persistent_view = TravelerLogView()
        try:
            client.add_view(_persistent_view)
        except Exception:
            # If discord.py version doesn't support add_view on Client for any reason,
            # buttons will still work for messages sent during this runtime.
            pass


# =====================
# COMMAND SETUP
# =====================
def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int, client: Optional[discord.Client] = None):
    """
    Registers:
      /writelog title entry

    NOTE: client is optional. If provided, registers persistent view.
    """
    _load_state()
    if client is not None:
        ensure_persistent_view_registered(client)

    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(
        name="writelog",
        description="Write a traveler log (auto-stamped with current Year & Day)",
        guild=guild_obj,
    )
    @app_commands.describe(
        title="Short title for your log entry",
        entry="The log text (up to 4000 chars)"
    )
    async def writelog(interaction: discord.Interaction, title: str, entry: str):
        # Get current day/year from time system
        year, day = _get_current_day_year()

        # Build embed
        embed = _build_embed(
            author_display=interaction.user.display_name,
            title=title,
            entry=entry,
            year=year,
            day=day,
        )

        # Post in the channel where the command was used
        view = TravelerLogView()
        msg = await interaction.channel.send(embed=embed, view=view)

        # Store record for editing later
        _logs[str(msg.id)] = {
            "author_id": interaction.user.id,
            "channel_id": interaction.channel.id,
            "title": title,
            "entry": entry,
            "year": year,
            "day": day,
            "created_ts": time.time(),
            "updated_ts": time.time(),
        }
        _save_state()

        await interaction.response.send_message("✅ Traveler log recorded.", ephemeral=True)


# =====================
# CATEGORY LOCK ENFORCEMENT
# =====================
async def enforce_travelerlog_lock(message: discord.Message):
    """
    Deletes normal messages posted in channels under LOCK_CATEGORY_ID,
    so people can ONLY post via /writelog (which posts an embed as the bot).
    """
    if message.author.bot:
        return
    if message.guild is None:
        return

    # Must be a guild text channel with a category
    ch = message.channel
    if not isinstance(ch, discord.abc.GuildChannel):
        return

    cat = getattr(ch, "category", None)
    if cat is None or getattr(cat, "id", None) != LOCK_CATEGORY_ID:
        return

    # Allow staff with Manage Messages to talk if you want (handy for moderation)
    try:
        if message.author.guild_permissions.manage_messages:
            return
    except Exception:
        pass

    # Allow system messages
    if message.type != discord.MessageType.default:
        return

    # If someone tries to talk normally, delete it
    try:
        await message.delete()
    except discord.Forbidden:
        # If bot lacks perms, fail silently
        pass
    except Exception:
        pass