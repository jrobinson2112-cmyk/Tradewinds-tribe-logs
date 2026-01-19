# travelerlogs_module.py
# Button-only Traveler Logs with:
# - Auto Year/Day default from time_module (editable in modal)
# - Multiple images per log (Add Image button; stores links)
# - Only author can edit/add images
# - Persistent buttons survive redeploy (no "interaction failed")
# - Testing mode: only works in allowed channel(s)

import os
import re
import json
import time
import asyncio
from typing import Dict, Any, Optional, List, Tuple

import discord
from discord import app_commands

import time_module  # pulls current Solunaris time

# =====================
# CONFIG
# =====================

# Only allow in these channels while testing (comma-separated). Default = your test channel.
_ALLOWED = os.getenv("TRAVELERLOG_ALLOWED_CHANNELS", "1462402354535075890")
TRAVELERLOG_ALLOWED_CHANNELS = {int(x.strip()) for x in _ALLOWED.split(",") if x.strip().isdigit()}

# Embed styling
TRAVELERLOG_EMBED_COLOR = int(os.getenv("TRAVELERLOG_EMBED_COLOR", "0x8B5CF6"), 16)
TRAVELERLOG_TITLE = os.getenv("TRAVELERLOG_TITLE", "📖 Traveler Log")

# Panel message (the "Write Log" button message)
PANEL_TITLE = os.getenv("TRAVELERLOG_PANEL_TITLE", "🖋️ Write Log")
PANEL_DESC = os.getenv(
    "TRAVELERLOG_PANEL_DESC",
    "Press the button below to write a Traveler Log.\n(Logs are posted as embeds; normal chat is removed.)",
)

# Storage
DATA_DIR = os.getenv("TRAVELERLOG_DATA_DIR", "/data")
STATE_FILE = os.getenv("TRAVELERLOG_STATE_FILE", os.path.join(DATA_DIR, "travelerlogs_state.json"))

# Limits
MAX_IMAGES_PER_LOG = int(os.getenv("TRAVELERLOG_MAX_IMAGES", "6"))
MAX_IMAGE_URLS_SHOWN = int(os.getenv("TRAVELERLOG_MAX_IMAGE_URLS_SHOWN", "10"))

# Rate-limit protection for ensure_panel (don’t spam)
ENSURE_PANEL_COOLDOWN_SECONDS = int(os.getenv("TRAVELERLOG_ENSURE_PANEL_COOLDOWN_SECONDS", "20"))

# =====================
# INTERNAL STATE
# =====================

_state: Dict[str, Any] = {
    "logs": {},  # message_id -> {author_id, year, day, title, entry, images:[url], created_ts, updated_ts}
    "panels": {},  # channel_id -> panel_message_id
    "pending_image": {},  # user_id -> {message_id, channel_id, expires_ts}
    "last_panel_ensure": {},  # channel_id -> ts
}

_state_loaded = False
_views_registered = False

# =====================
# UTIL / STORAGE
# =====================

def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _load_state():
    global _state_loaded, _state
    if _state_loaded:
        return
    _ensure_dir(STATE_FILE)
    if not os.path.exists(STATE_FILE):
        _state_loaded = True
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # merge, don't replace (forward compat)
            for k in data:
                _state[k] = data[k]
    except Exception:
        pass
    _state_loaded = True

def _save_state():
    try:
        _ensure_dir(STATE_FILE)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f)
    except Exception:
        pass

def _now_ts() -> float:
    return time.time()

# =====================
# TIME HELPERS
# =====================

def _get_current_time_from_time_module() -> Tuple[int, int]:
    """
    Returns (year, day). Falls back to (1, 1) if not available.
    """
    # Try a dedicated helper if it exists
    getter = getattr(time_module, "get_time_state", None)
    if callable(getter):
        try:
            st = getter()
            y = int(st.get("year", 1))
            d = int(st.get("day", 1))
            return y, d
        except Exception:
            return 1, 1

    # Fallback: try reading the time state file if time_module uses it
    try:
        time_state_file = os.getenv("TIME_STATE_FILE", "/data/time_state.json")
        if os.path.exists(time_state_file):
            with open(time_state_file, "r", encoding="utf-8") as f:
                st = json.load(f)
            y = int(st.get("year", 1))
            d = int(st.get("day", 1))
            return y, d
    except Exception:
        pass

    return 1, 1

# =====================
# EMBED BUILDING
# =====================

def _image_links_block(urls: List[str]) -> str:
    if not urls:
        return ""
    shown = urls[:MAX_IMAGE_URLS_SHOWN]
    lines = []
    for i, u in enumerate(shown, start=1):
        lines.append(f"[Image {i}]({u})")
    if len(urls) > len(shown):
        lines.append(f"… and {len(urls) - len(shown)} more")
    return "\n".join(lines)

def _build_log_embed(log: Dict[str, Any]) -> discord.Embed:
    year = int(log.get("year", 1))
    day = int(log.get("day", 1))
    title = str(log.get("title", "Traveler Log")).strip() or "Traveler Log"
    entry = str(log.get("entry", "")).strip() or "*No text provided.*"
    images = list(log.get("images", [])) if isinstance(log.get("images", []), list) else []

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

    if images:
        # show first image visually, list links below
        embed.set_image(url=images[0])
        links = _image_links_block(images)
        if links:
            embed.add_field(name="📸 Images", value=links, inline=False)

    author_id = log.get("author_id")
    if author_id:
        embed.set_footer(text=f"Logged by <@{author_id}>")

    return embed

# =====================
# UI MODALS
# =====================

class WriteLogModal(discord.ui.Modal):
    def __init__(self, *, year_default: int, day_default: int):
        super().__init__(title="Write Traveler Log")

        self.year = discord.ui.TextInput(
            label="Year",
            placeholder="e.g. 2",
            default=str(year_default),
            required=True,
            max_length=6,
        )
        self.day = discord.ui.TextInput(
            label="Day",
            placeholder="e.g. 326",
            default=str(day_default),
            required=True,
            max_length=6,
        )
        self.log_title = discord.ui.TextInput(
            label="Title",
            placeholder="Short title",
            required=True,
            max_length=80,
        )
        self.entry = discord.ui.TextInput(
            label="Log",
            placeholder="Write your log entry...",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1900,  # Discord hard limit for modal inputs
        )

        self.add_item(self.year)
        self.add_item(self.day)
        self.add_item(self.log_title)
        self.add_item(self.entry)

        self.result: Optional[Dict[str, Any]] = None

    async def on_submit(self, interaction: discord.Interaction):
        # Validate numeric year/day
        if not re.fullmatch(r"\d+", str(self.year.value).strip() or ""):
            await interaction.response.send_message("❌ Year must be numeric.", ephemeral=True)
            return
        if not re.fullmatch(r"\d+", str(self.day.value).strip() or ""):
            await interaction.response.send_message("❌ Day must be numeric.", ephemeral=True)
            return

        self.result = {
            "year": int(self.year.value),
            "day": int(self.day.value),
            "title": str(self.log_title.value).strip(),
            "entry": str(self.entry.value).strip(),
        }
        await interaction.response.defer(ephemeral=True)


class EditLogModal(discord.ui.Modal):
    def __init__(self, *, log: Dict[str, Any]):
        super().__init__(title="Edit Traveler Log")

        self.year = discord.ui.TextInput(
            label="Year",
            default=str(int(log.get("year", 1))),
            required=True,
            max_length=6,
        )
        self.day = discord.ui.TextInput(
            label="Day",
            default=str(int(log.get("day", 1))),
            required=True,
            max_length=6,
        )
        self.log_title = discord.ui.TextInput(
            label="Title",
            default=str(log.get("title", "")).strip()[:80],
            required=True,
            max_length=80,
        )
        self.entry = discord.ui.TextInput(
            label="Log",
            default=str(log.get("entry", "")).strip()[:1900],
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1900,
        )

        self.add_item(self.year)
        self.add_item(self.day)
        self.add_item(self.log_title)
        self.add_item(self.entry)

        self.result: Optional[Dict[str, Any]] = None

    async def on_submit(self, interaction: discord.Interaction):
        if not re.fullmatch(r"\d+", str(self.year.value).strip() or ""):
            await interaction.response.send_message("❌ Year must be numeric.", ephemeral=True)
            return
        if not re.fullmatch(r"\d+", str(self.day.value).strip() or ""):
            await interaction.response.send_message("❌ Day must be numeric.", ephemeral=True)
            return

        self.result = {
            "year": int(self.year.value),
            "day": int(self.day.value),
            "title": str(self.log_title.value).strip(),
            "entry": str(self.entry.value).strip(),
        }
        await interaction.response.defer(ephemeral=True)
        
# =====================
# VIEWS (PERSISTENT BUTTONS)
# =====================

class WritePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🖋️ Write Log",
        style=discord.ButtonStyle.primary,
        custom_id="travlog:write",
    )
    async def write_log_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        _load_state()

        if interaction.channel_id not in TRAVELERLOG_ALLOWED_CHANNELS:
            await interaction.response.send_message("🚧 Traveler logs are currently in testing.", ephemeral=True)
            return

        year_default, day_default = _get_current_time_from_time_module()
        modal = WriteLogModal(year_default=year_default, day_default=day_default)
        await interaction.response.send_modal(modal)

        # wait for modal submit
        timeout_at = _now_ts() + 180
        while modal.result is None and _now_ts() < timeout_at:
            await asyncio.sleep(0.25)

        if modal.result is None:
            return  # user closed modal

        data = modal.result
        author_id = interaction.user.id

        # Post log message
        log_record = {
            "author_id": author_id,
            "year": data["year"],
            "day": data["day"],
            "title": data["title"],
            "entry": data["entry"],
            "images": [],
            "created_ts": _now_ts(),
            "updated_ts": _now_ts(),
        }

        embed = _build_log_embed(log_record)
        msg = await interaction.channel.send(embed=embed, view=LogEntryView())

        # Persist by message_id
        _state["logs"][str(msg.id)] = log_record
        _save_state()

        # Keep panel at the bottom (delete old panel and repost)
        await _refresh_panel_in_channel(interaction.channel)

        await interaction.followup.send("✅ Traveler log recorded.", ephemeral=True)


class LogEntryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✏️ Edit Log",
        style=discord.ButtonStyle.secondary,
        custom_id="travlog:edit",
    )
    async def edit_log_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        _load_state()

        if interaction.channel_id not in TRAVELERLOG_ALLOWED_CHANNELS:
            await interaction.response.send_message("🚧 Traveler logs are currently in testing.", ephemeral=True)
            return

        mid = str(interaction.message.id)
        log = _state["logs"].get(mid)
        if not log:
            await interaction.response.send_message("❌ I can’t find the stored data for this log.", ephemeral=True)
            return

        if int(log.get("author_id", 0)) != interaction.user.id:
            await interaction.response.send_message("❌ Only the author can edit this log.", ephemeral=True)
            return

        modal = EditLogModal(log=log)
        await interaction.response.send_modal(modal)

        timeout_at = _now_ts() + 180
        while modal.result is None and _now_ts() < timeout_at:
            await asyncio.sleep(0.25)

        if modal.result is None:
            return

        data = modal.result
        log["year"] = data["year"]
        log["day"] = data["day"]
        log["title"] = data["title"]
        log["entry"] = data["entry"]
        log["updated_ts"] = _now_ts()

        _state["logs"][mid] = log
        _save_state()

        # Update message embed
        try:
            await interaction.message.edit(embed=_build_log_embed(log), view=LogEntryView())
        except Exception:
            pass

        await interaction.followup.send("✅ Log updated.", ephemeral=True)

    @discord.ui.button(
        label="📸 Add Images",
        style=discord.ButtonStyle.success,
        custom_id="travlog:addimg",
    )
    async def add_image_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        _load_state()

        if interaction.channel_id not in TRAVELERLOG_ALLOWED_CHANNELS:
            await interaction.response.send_message("🚧 Traveler logs are currently in testing.", ephemeral=True)
            return

        mid = str(interaction.message.id)
        log = _state["logs"].get(mid)
        if not log:
            await interaction.response.send_message("❌ I can’t find the stored data for this log.", ephemeral=True)
            return

        if int(log.get("author_id", 0)) != interaction.user.id:
            await interaction.response.send_message("❌ Only the author can add images to this log.", ephemeral=True)
            return

        # Set pending image upload mode for this user
        _state["pending_image"][str(interaction.user.id)] = {
            "message_id": int(interaction.message.id),
            "channel_id": int(interaction.channel_id),
            "expires_ts": _now_ts() + 180,  # 3 minutes
        }
        _save_state()

        await interaction.response.send_message(
            "📸 Upload your image(s) in this channel now (up to 3 minutes). "
            "I’ll attach them to this log and remove your upload message to keep the channel clean.",
            ephemeral=True,
        )

# =====================
# PANEL MANAGEMENT
# =====================

async def _find_existing_panel_message(channel: discord.TextChannel) -> Optional[discord.Message]:
    """
    Look back a small window to find an existing panel message.
    """
    try:
        async for m in channel.history(limit=50):
            if m.author.bot and m.components:
                # Detect our panel by custom_id
                for row in m.components:
                    for comp in getattr(row, "children", []):
                        if getattr(comp, "custom_id", None) == "travlog:write":
                            return m
    except Exception:
        return None
    return None

async def _post_panel(channel: discord.TextChannel) -> Optional[discord.Message]:
    embed = discord.Embed(title=PANEL_TITLE, description=PANEL_DESC, color=TRAVELERLOG_EMBED_COLOR)
    try:
        msg = await channel.send(embed=embed, view=WritePanelView())
        return msg
    except Exception:
        return None

async def _refresh_panel_in_channel(channel: discord.TextChannel):
    """
    Ensures a single panel exists and is the LAST message in channel.
    Deletes older panel if found, posts a new one at bottom.
    """
    _load_state()

    # cooldown
    last = float(_state["last_panel_ensure"].get(str(channel.id), 0.0))
    if _now_ts() - last < ENSURE_PANEL_COOLDOWN_SECONDS:
        return
    _state["last_panel_ensure"][str(channel.id)] = _now_ts()

    try:
        existing = await _find_existing_panel_message(channel)
        if existing:
            try:
                await existing.delete()
            except Exception:
                pass

        new_panel = await _post_panel(channel)
        if new_panel:
            _state["panels"][str(channel.id)] = int(new_panel.id)
            _save_state()
    except Exception:
        pass

async def ensure_write_panels(client: discord.Client, guild_id: int):
    """
    Ensures the write panel exists in every allowed channel (testing mode).
    Safe to call on startup.
    """
    _load_state()

    guild = client.get_guild(int(guild_id))
    if guild is None:
        try:
            guild = await client.fetch_guild(int(guild_id))
        except Exception:
            return

    for ch_id in list(TRAVELERLOG_ALLOWED_CHANNELS):
        ch = guild.get_channel(int(ch_id))
        if ch is None:
            try:
                ch = await guild.fetch_channel(int(ch_id))
            except Exception:
                continue
        if not isinstance(ch, discord.TextChannel):
            continue

        # Ensure panel exists (don’t force-bottom here; just ensure it exists)
        try:
            existing = await _find_existing_panel_message(ch)
            if existing is None:
                msg = await _post_panel(ch)
                if msg:
                    _state["panels"][str(ch.id)] = int(msg.id)
                    _save_state()
        except Exception:
            continue

# =====================
# COMMAND SETUP (OPTIONAL FALLBACK)
# =====================

def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int):
    """
    Optional fallback slash command (kept for convenience).
    Button-only still works even if you remove this.
    """

    @tree.command(
        name="writelog",
        description="Write a traveler log (auto-stamped with current Year & Day; editable)",
        guild=discord.Object(id=int(guild_id)),
    )
    async def writelog_cmd(interaction: discord.Interaction):
        if interaction.channel_id not in TRAVELERLOG_ALLOWED_CHANNELS:
            await interaction.response.send_message("🚧 Traveler logs are currently in testing.", ephemeral=True)
            return

        year_default, day_default = _get_current_time_from_time_module()
        modal = WriteLogModal(year_default=year_default, day_default=day_default)
        await interaction.response.send_modal(modal)

# =====================
# PERSISTENT VIEW REGISTRATION
# =====================

def register_views(client: discord.Client):
    """
    Must be called once at startup (on_ready is fine).
    This prevents old buttons from failing after redeploy.
    """
    global _views_registered
    if _views_registered:
        return
    client.add_view(WritePanelView())
    client.add_view(LogEntryView())
    _views_registered = True

def setup_interaction_router(client: discord.Client):
    """
    Not strictly required when using persistent views, but harmless to keep compatibility.
    """
    # No-op; persistent views handle interactions.
    return

# =====================
# LOCK ENFORCEMENT + IMAGE CAPTURE
# =====================

async def enforce_travelerlog_lock(message: discord.Message):
    """
    Deletes normal messages in allowed channels to enforce embed-only logging.
    Also captures image uploads when user has pressed "Add Images".
    """
    _load_state()

    if message.author.bot:
        return

    if message.channel.id not in TRAVELERLOG_ALLOWED_CHANNELS:
        return

    # If user is in pending image mode, accept attachments and attach to the target log
    pend = _state.get("pending_image", {}).get(str(message.author.id))
    if pend and int(pend.get("channel_id", 0)) == message.channel.id and _now_ts() <= float(pend.get("expires_ts", 0)):
        if not message.attachments:
            # block text even during pending image mode
            try:
                await message.delete()
            except Exception:
                pass
            return

        target_mid = str(int(pend.get("message_id")))
        log = _state["logs"].get(target_mid)
        if not log:
            # nothing to attach to; clear pending
            _state["pending_image"].pop(str(message.author.id), None)
            _save_state()
            try:
                await message.delete()
            except Exception:
                pass
            return

        # Add attachment URLs (up to MAX_IMAGES_PER_LOG)
        imgs = list(log.get("images", [])) if isinstance(log.get("images", []), list) else []
        added = 0
        for a in message.attachments:
            if len(imgs) >= MAX_IMAGES_PER_LOG:
                break
            if a.url:
                imgs.append(a.url)
                added += 1

        log["images"] = imgs
        log["updated_ts"] = _now_ts()
        _state["logs"][target_mid] = log

        # Clear pending once we got at least one image (or keep? We'll clear to avoid accidental captures)
        _state["pending_image"].pop(str(message.author.id), None)
        _save_state()

        # Update the original log message embed
        try:
            # Fetch the message to edit
            target_msg = await message.channel.fetch_message(int(target_mid))
            await target_msg.edit(embed=_build_log_embed(log), view=LogEntryView())
        except Exception:
            pass

        # Delete the upload message to keep channel clean
        try:
            await message.delete()
        except Exception:
            pass

        return

    # Not in image mode -> delete normal messages (including images)
    try:
        await message.delete()
    except discord.Forbidden:
        pass
    except Exception:
        pass