# travelerlogs_module.py
# Button-only Traveler Logs with:
# - Persistent "Write Log" panel (survives redeploy)
# - Author-only Edit + Add Images buttons
# - Image upload via attachments (NOT pasted URLs) to avoid grey placeholder embeds
# - Default Day/Year pulled from time_module (but editable in modal)
# - Optional lock: delete normal messages in the channel (except image-upload replies when prompted)

import os
import json
import time
import asyncio
from typing import Optional, Dict, Any, List, Tuple

import discord
from discord import app_commands

import time_module


# =====================
# CONFIG
# =====================
DATA_DIR = os.getenv("TRAVELERLOGS_DATA_DIR", "/data")
STATE_FILE = os.path.join(DATA_DIR, "travelerlogs_state.json")

# Testing-only: only ensure the panel exists in this channel
TEST_CHANNEL_ID = int(os.getenv("TRAVELERLOG_TEST_CHANNEL_ID", "1462402354535075890"))

# If you later want category mode again, set this env and adjust ensure_write_panels
LOCK_CATEGORY_ID = int(os.getenv("TRAVELERLOGS_LOCK_CATEGORY_ID", "0"))

# Excluded channels for panel posting
EXCLUDED_CHANNEL_IDS = {
    1462539723112321218,
    1437457789164191939,
    1455315150859927663,
    1456386974167466106,
}

# Admin role to allow manual panel posting command
ADMIN_ROLE_ID = int(os.getenv("TRAVELERLOGS_ADMIN_ROLE_ID", "1439069787207766076"))

# UI
EMBED_COLOR = 0x8B5CF6  # purple
LOG_TITLE = "📖 Traveler Log"
PANEL_TITLE = "✒️ Write a Traveler Log"
PANEL_DESC = "Tap the button below to write a Traveler Log.\n\n**Tap the button • A form will open**"

# Limits
MAX_IMAGES_PER_LOG = int(os.getenv("TRAVELERLOGS_MAX_IMAGES", "6"))
IMAGE_UPLOAD_TIMEOUT = int(os.getenv("TRAVELERLOGS_IMAGE_TIMEOUT_SEC", "180"))  # 3 mins
PANEL_SEARCH_LIMIT = 50  # how many recent messages to scan for an existing panel


# =====================
# STATE
# =====================
# message_id -> log dict (author_id, author_name, title, body, year, day, image_urls[])
_LOGS: Dict[str, Dict[str, Any]] = {}

# (channel_id, user_id) -> {"target_mid": int, "expires_at": float}
_PENDING_IMAGE_UPLOAD: Dict[Tuple[int, int], Dict[str, Any]] = {}

# Persistent views need stable custom_ids
CID_WRITE = "travelerlogs:write"
CID_EDIT = "travelerlogs:edit"
CID_ADD_IMAGES = "travelerlogs:add_images"


# =====================
# FILE HELPERS
# =====================
def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

def _load_state():
    global _LOGS
    _ensure_dir()
    try:
        if not os.path.exists(STATE_FILE):
            _LOGS = {}
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _LOGS = data.get("logs", {}) if isinstance(data.get("logs", {}), dict) else {}
        else:
            _LOGS = {}
    except Exception:
        _LOGS = {}

def _save_state():
    _ensure_dir()
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"logs": _LOGS}, f)
    except Exception:
        pass


# =====================
# TIME HELPERS
# =====================
def _get_current_day_year() -> Tuple[int, int]:
    """
    Pull current Year + Day from the time system.
    """
    try:
        state = time_module.get_time_state()
        year = int(state.get("year", 1))
        day = int(state.get("day", 1))
        return year, day
    except Exception:
        return 1, 1


# =====================
# EMBED BUILDERS
# =====================
def _build_log_embed(
    *,
    author_name: str,
    title: str,
    body: str,
    year: int,
    day: int,
    image_urls: Optional[List[str]] = None,
) -> discord.Embed:
    image_urls = image_urls or []

    embed = discord.Embed(title=LOG_TITLE, color=EMBED_COLOR)

    embed.add_field(
        name="🗓️ Solunaris Time",
        value=f"**Year {year} • Day {day}**",
        inline=False,
    )

    embed.add_field(name=title, value=body if body else "—", inline=False)

    if image_urls:
        # show the first image as the embed image (most reliable rendering)
        embed.set_image(url=image_urls[0])

        # and list the rest as links (including first, so it’s all visible)
        links = []
        for i, url in enumerate(image_urls[:MAX_IMAGES_PER_LOG], start=1):
            links.append(f"[Image {i}]({url})")
        embed.add_field(name="📸 Images", value="\n".join(links), inline=False)

    embed.set_footer(text=f"Logged by {author_name}")
    return embed


def _log_view_for(message_id: int, author_id: int) -> discord.ui.View:
    return TravelerLogView(message_id=message_id, author_id=author_id)


def _panel_view() -> discord.ui.View:
    return TravelerWritePanelView()


# =====================
# MODALS
# =====================
class WriteLogModal(discord.ui.Modal, title="Write a Traveler Log"):
    def __init__(self, *, default_year: int, default_day: int):
        super().__init__(timeout=300)

        self.year = discord.ui.TextInput(
            label="Year (number)",
            placeholder=str(default_year),
            default=str(default_year),
            required=True,
            max_length=6,
        )
        self.day = discord.ui.TextInput(
            label="Day (number)",
            placeholder=str(default_day),
            default=str(default_day),
            required=True,
            max_length=6,
        )
        self.log_title = discord.ui.TextInput(
            label="Title",
            placeholder="Short title for your log entry",
            required=True,
            max_length=120,
        )
        self.body = discord.ui.TextInput(
            label="Log",
            placeholder="Write your traveler log...",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1900,
        )

        self.add_item(self.year)
        self.add_item(self.day)
        self.add_item(self.log_title)
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction):
        # Parse numeric fields
        try:
            year = int(str(self.year.value).strip())
            day = int(str(self.day.value).strip())
        except Exception:
            await interaction.response.send_message("❌ Year and Day must be numbers.", ephemeral=True)
            return

        author_name = interaction.user.display_name
        author_id = interaction.user.id

        embed = _build_log_embed(
            author_name=author_name,
            title=str(self.log_title.value).strip(),
            body=str(self.body.value).strip(),
            year=year,
            day=day,
            image_urls=[],
        )

        # Post log
        msg = await interaction.channel.send(
            embed=embed,
            view=_log_view_for(message_id=0, author_id=author_id),  # temp; replaced below
        )

        # Persist
        _LOGS[str(msg.id)] = {
            "author_id": author_id,
            "author_name": author_name,
            "title": str(self.log_title.value).strip(),
            "body": str(self.body.value).strip(),
            "year": year,
            "day": day,
            "image_urls": [],
        }
        _save_state()

        # Re-attach the correct view with the real message_id
        await msg.edit(view=_log_view_for(message_id=msg.id, author_id=author_id))

        await interaction.response.send_message("✅ Traveler log recorded.", ephemeral=True)


class EditLogModal(discord.ui.Modal, title="Edit Traveler Log"):
    def __init__(self, *, message_id: int, current: Dict[str, Any]):
        super().__init__(timeout=300)
        self.message_id = message_id

        self.year = discord.ui.TextInput(
            label="Year (number)",
            default=str(current.get("year", 1)),
            required=True,
            max_length=6,
        )
        self.day = discord.ui.TextInput(
            label="Day (number)",
            default=str(current.get("day", 1)),
            required=True,
            max_length=6,
        )
        self.log_title = discord.ui.TextInput(
            label="Title",
            default=str(current.get("title", ""))[:120],
            required=True,
            max_length=120,
        )
        self.body = discord.ui.TextInput(
            label="Log",
            default=str(current.get("body", ""))[:1900],
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1900,
        )

        self.add_item(self.year)
        self.add_item(self.day)
        self.add_item(self.log_title)
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction):
        log = _LOGS.get(str(self.message_id))
        if not log:
            await interaction.response.send_message("❌ I can’t find that log anymore.", ephemeral=True)
            return

        if interaction.user.id != int(log.get("author_id", 0)):
            await interaction.response.send_message("❌ Only the author can edit this log.", ephemeral=True)
            return

        try:
            year = int(str(self.year.value).strip())
            day = int(str(self.day.value).strip())
        except Exception:
            await interaction.response.send_message("❌ Year and Day must be numbers.", ephemeral=True)
            return

        log["year"] = year
        log["day"] = day
        log["title"] = str(self.log_title.value).strip()
        log["body"] = str(self.body.value).strip()
        # keep images
        image_urls = list(log.get("image_urls", []) or [])
        author_name = str(log.get("author_name") or interaction.user.display_name)

        _save_state()

        embed = _build_log_embed(
            author_name=author_name,
            title=log["title"],
            body=log["body"],
            year=log["year"],
            day=log["day"],
            image_urls=image_urls,
        )

        try:
            await interaction.message.edit(embed=embed, view=_log_view_for(message_id=self.message_id, author_id=interaction.user.id))
        except Exception:
            pass

        await interaction.response.send_message("✅ Updated.", ephemeral=True)


# =====================
# VIEWS (PERSISTENT)
# =====================
class TravelerWritePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✒️ Write Log", style=discord.ButtonStyle.primary, custom_id=CID_WRITE)
    async def write_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        year, day = _get_current_day_year()
        await interaction.response.send_modal(WriteLogModal(default_year=year, default_day=day))


class TravelerLogView(discord.ui.View):
    def __init__(self, *, message_id: int, author_id: int):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.author_id = author_id

    @discord.ui.button(label="✏️ Edit Log", style=discord.ButtonStyle.secondary, custom_id=CID_EDIT)
    async def edit_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        log = _LOGS.get(str(self.message_id))
        if not log:
            await interaction.response.send_message("❌ I can’t find that log anymore.", ephemeral=True)
            return
        if interaction.user.id != int(log.get("author_id", 0)):
            await interaction.response.send_message("❌ Only the author can edit this log.", ephemeral=True)
            return

        await interaction.response.send_modal(EditLogModal(message_id=self.message_id, current=log))

    @discord.ui.button(label="📸 Add Images", style=discord.ButtonStyle.success, custom_id=CID_ADD_IMAGES)
    async def add_images(self, interaction: discord.Interaction, button: discord.ui.Button):
        log = _LOGS.get(str(self.message_id))
        if not log:
            await interaction.response.send_message("❌ I can’t find that log anymore.", ephemeral=True)
            return
        if interaction.user.id != int(log.get("author_id", 0)):
            await interaction.response.send_message("❌ Only the author can add images to this log.", ephemeral=True)
            return

        # Create a pending upload slot
        key = (interaction.channel_id, interaction.user.id)
        _PENDING_IMAGE_UPLOAD[key] = {
            "target_mid": self.message_id,
            "expires_at": time.time() + IMAGE_UPLOAD_TIMEOUT,
        }

        await interaction.response.send_message(
            f"📸 **Send up to {MAX_IMAGES_PER_LOG} images** in this channel now.\n"
            f"I’ll attach them to your log and delete your upload message.\n"
            f"Timeout: {IMAGE_UPLOAD_TIMEOUT}s",
            ephemeral=True,
        )

        # Kick off waiter task
        asyncio.create_task(_await_image_upload(interaction.client, interaction.channel_id, interaction.user.id))


# =====================
# IMAGE UPLOAD FLOW
# =====================
async def _await_image_upload(client: discord.Client, channel_id: int, user_id: int):
    key = (channel_id, user_id)
    info = _PENDING_IMAGE_UPLOAD.get(key)
    if not info:
        return

    # Wait for the next message from that user in that channel containing attachments
    def check(m: discord.Message) -> bool:
        if m.channel.id != channel_id:
            return False
        if m.author.id != user_id:
            return False
        if not m.attachments:
            return False
        # Still pending & not expired
        meta = _PENDING_IMAGE_UPLOAD.get(key)
        return bool(meta) and time.time() < float(meta.get("expires_at", 0))

    try:
        msg: discord.Message = await client.wait_for("message", check=check, timeout=IMAGE_UPLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        # Expired
        _PENDING_IMAGE_UPLOAD.pop(key, None)
        return
    except Exception:
        _PENDING_IMAGE_UPLOAD.pop(key, None)
        return

    # Resolve target log
    info = _PENDING_IMAGE_UPLOAD.pop(key, None)
    if not info:
        return

    target_mid = int(info.get("target_mid", 0))
    log = _LOGS.get(str(target_mid))
    if not log:
        # delete upload message anyway to keep clean
        try:
            await msg.delete()
        except Exception:
            pass
        return

    # Extract attachment URLs (this is the reliable way)
    urls = []
    for a in msg.attachments[:MAX_IMAGES_PER_LOG]:
        if a.url:
            urls.append(a.url)

    if not urls:
        try:
            await msg.delete()
        except Exception:
            pass
        return

    # Append to existing
    existing = list(log.get("image_urls", []) or [])
    combined = (existing + urls)[:MAX_IMAGES_PER_LOG]
    log["image_urls"] = combined
    _save_state()

    # Update embed
    author_name = str(log.get("author_name") or msg.author.display_name)
    embed = _build_log_embed(
        author_name=author_name,
        title=str(log.get("title", "Untitled")),
        body=str(log.get("body", "")),
        year=int(log.get("year", 1)),
        day=int(log.get("day", 1)),
        image_urls=combined,
    )

    # Edit the original log message
    try:
        channel = msg.channel
        target_message = await channel.fetch_message(target_mid)
        await target_message.edit(embed=embed, view=_log_view_for(message_id=target_mid, author_id=int(log.get("author_id", 0))))
    except Exception:
        pass

    # Delete the upload message so channels stay clean
    try:
        await msg.delete()
    except Exception:
        pass


# =====================
# PUBLIC API: MAIN.PY HOOKS
# =====================
def register_views(client: discord.Client):
    """
    Must be called on_ready BEFORE syncing / before users click buttons.
    This prevents "interaction failed" after redeploy.
    """
    client.add_view(TravelerWritePanelView())
    # Log view is created per-message, but buttons share same custom_ids,
    # so persistent registration of the class isn't required here.


def setup_interaction_router(client: discord.Client):
    """
    No-op for this implementation (we use normal View callbacks).
    Kept because your main.py calls it.
    """
    return


def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int):
    """
    Optional fallback slash command, useful for admins / debugging:
      /postlogbutton -> posts panel in current channel (admin role only)
    """

    @tree.command(
        name="postlogbutton",
        description="Post the Traveler Log panel in this channel (admin role only).",
        guild=discord.Object(id=guild_id),
    )
    async def postlogbutton(interaction: discord.Interaction):
        # role check
        role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
        if ADMIN_ROLE_ID not in role_ids:
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        ok, reason = await ensure_panel_in_channel(interaction.channel)
        if ok:
            await interaction.response.send_message("✅ Panel posted.", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ {reason}", ephemeral=True)


async def ensure_write_panels(client: discord.Client, guild_id: int):
    """
    Ensures the panel exists.
    **Testing mode:** only in TEST_CHANNEL_ID.
    """
    # Only test channel for now (your request)
    try:
        ch = client.get_channel(TEST_CHANNEL_ID) or await client.fetch_channel(TEST_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel) and ch.id not in EXCLUDED_CHANNEL_IDS:
            await ensure_panel_in_channel(ch)
    except Exception as e:
        print(f"[travelerlogs] ensure_write_panels error: {e}")


async def ensure_panel_in_channel(channel: discord.abc.Messageable) -> Tuple[bool, str]:
    """
    Posts & pins a "Write Log" panel if one doesn't exist recently.
    Returns (ok, reason).
    """
    # Only real text channels
    if not isinstance(channel, discord.TextChannel):
        return False, "Not a text channel."

    if channel.id in EXCLUDED_CHANNEL_IDS:
        return False, "Channel is excluded."

    # Scan recent messages for an existing panel
    try:
        async for m in channel.history(limit=PANEL_SEARCH_LIMIT):
            if m.author.bot and m.embeds:
                e = m.embeds[0]
                if (e.title or "").strip() == PANEL_TITLE:
                    return False, "Panel already exists here (or I couldn't post it)."
    except Exception:
        pass

    embed = discord.Embed(title=PANEL_TITLE, description=PANEL_DESC, color=EMBED_COLOR)
    view = _panel_view()

    try:
        msg = await channel.send(embed=embed, view=view)
        # Pin it
        try:
            await msg.pin(reason="Traveler Log panel")
        except Exception:
            pass
        return True, "Posted."
    except discord.HTTPException as e:
        return False, f"HTTP error posting panel: {e}"
    except Exception as e:
        return False, f"Error posting panel: {e}"


async def enforce_travelerlog_lock(message: discord.Message):
    """
    Deletes normal messages in the TEST channel (or later category),
    BUT allows:
      - bot messages
      - slash commands
      - the special image-upload reply (when Add Images is waiting)
    """
    if message.author.bot:
        return

    # Only enforce in testing channel for now
    if message.channel.id != TEST_CHANNEL_ID:
        return

    # Allow slash commands typed (if user can type at all)
    if message.content and message.content.startswith("/"):
        return

    # Allow image-upload replies ONLY if we are waiting for them
    key = (message.channel.id, message.author.id)
    pending = _PENDING_IMAGE_UPLOAD.get(key)
    if pending and time.time() < float(pending.get("expires_at", 0)) and message.attachments:
        return

    # Otherwise remove normal messages
    try:
        await message.delete()
    except Exception:
        pass


# =====================
# INIT
# =====================
_load_state()