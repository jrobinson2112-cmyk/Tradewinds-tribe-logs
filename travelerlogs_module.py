# travelerlogs_module.py
# Traveler logs with:
# ✅ /writelog (auto-stamped with Year/Day from time_module)
# ✅ "✏️ Edit Log" button (author-only) + modal editor (title + body prefilled)
# ✅ Persistent storage (survives redeploys) via /data/travelerlogs_state.json
# ✅ Category lock: users can ONLY post via /writelog in a specific category (deletes normal text)

import os
import json
import time
from typing import Dict, Any, Optional, Tuple

import discord
from discord import app_commands

import time_module  # pulls current Solunaris time

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

# ALSO read time state's file directly as fallback
# (time_module usually uses TIME_STATE_FILE=/data/time_state.json)
TIME_STATE_FILE_FALLBACK = os.getenv("TIME_STATE_FILE", "/data/time_state.json")

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
def _read_json_file(path: str) -> Optional[dict]:
    try:
        if not path or not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None

def _extract_year_day(state: Optional[dict]) -> Optional[Tuple[int, int]]:
    if not isinstance(state, dict):
        return None
    # Try common key variants
    year = state.get("year", state.get("Year"))
    day = state.get("day", state.get("Day"))
    if year is None or day is None:
        return None
    try:
        year_i = int(year)
        day_i = int(day)
        if year_i < 1 or day_i < 1:
            return None
        return year_i, day_i
    except Exception:
        return None

def _get_current_day_year() -> Tuple[int, int]:
    """
    Pull current Year + Day from the time system.
    Falls back to reading the time state file directly.
    """
    # 1) If time_module exposes get_time_state()
    try:
        fn = getattr(time_module, "get_time_state", None)
        if callable(fn):
            yd = _extract_year_day(fn())
            if yd:
                return yd
    except Exception:
        pass

    # 2) If time_module exposes load_state()
    try:
        fn = getattr(time_module, "load_state", None)
        if callable(fn):
            yd = _extract_year_day(fn())
            if yd:
                return yd
    except Exception:
        pass

    # 3) If time_module keeps _state in memory
    try:
        st = getattr(time_module, "_state", None)
        yd = _extract_year_day(st)
        if yd:
            return yd
    except Exception:
        pass

    # 4) Read from file path used by time_module if available, otherwise fallback env
    try:
        time_state_path = getattr(time_module, "STATE_FILE", None) or TIME_STATE_FILE_FALLBACK
        yd = _extract_year_day(_read_json_file(str(time_state_path)))
        if yd:
            return yd
    except Exception:
        pass

    # 5) Last resort fallback
    return 1, 1


# =====================
# EMBED HELPERS
# =====================
def _build_embed(author_display: str, title: str, entry: str, year: int, day: int) -> discord.Embed:
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
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Only the original author can edit this log.", ephemeral=True)
            return

        mid = str(self.message_id)
        rec = _logs.get(mid)
        if not rec:
            await interaction.response.send_message("❌ I can't find the stored record for this log.", ephemeral=True)
            return

        rec["title"] = str(self.title_input.value)
        rec["entry"] = str(self.entry_input.value)
        rec["updated_ts"] = time.time()
        _logs[mid] = rec
        _save_state()

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
            await interaction.response.send_message("❌ Could not edit that message (missing permissions?).", ephemeral=True)
            return

        await interaction.response.send_message("✅ Log updated.", ephemeral=True)


class TravelerLogView(discord.ui.View):
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
        if not rec:
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
    global _persistent_view
    if _persistent_view is None:
        _persistent_view = TravelerLogView()
        try:
            client.add_view(_persistent_view)
        except Exception:
            pass


# =====================
# COMMAND SETUP
# =====================
def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int, client: Optional[discord.Client] = None):
    """
    Registers /writelog command.
    Pass client from main.py so persistent edit button survives redeploy.
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
        year, day = _get_current_day_year()

        embed = _build_embed(
            author_display=interaction.user.display_name,
            title=title,
            entry=entry,
            year=year,
            day=day,
        )

        view = TravelerLogView()
        msg = await interaction.channel.send(embed=embed, view=view)

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
    so people can ONLY post via /writelog.
    """
    if message.author.bot:
        return
    if message.guild is None:
        return

    ch = message.channel
    if not isinstance(ch, discord.abc.GuildChannel):
        return

    cat = getattr(ch, "category", None)
    if cat is None or getattr(cat, "id", None) != LOCK_CATEGORY_ID:
        return

    # Allow moderators with Manage Messages
    try:
        if message.author.guild_permissions.manage_messages:
            return
    except Exception:
        pass

    if message.type != discord.MessageType.default:
        return

    try:
        await message.delete()
    except discord.Forbidden:
        pass
    except Exception:
        pass