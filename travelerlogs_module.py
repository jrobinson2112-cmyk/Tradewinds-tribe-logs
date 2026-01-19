# travelerlogs_module.py
# Button-first Traveler Logs with:
# - Write Log panel (button) + optional /postlogbutton admin command
# - Auto-stamps default Year/Day from time_module (but user can override)
# - Edit button (ONLY the author can edit)
# - Add Images flow (uploads images as ATTACHMENTS so embed images actually render)
# - Auto-continue for long logs (splits into follow-up embed messages)
# - No "lock" enforcement (you said you'll handle perms in Discord)
#
# IMPORTANT:
# - This module is built for discord.py 2.x
# - You MUST call travelerlogs_module.register_views(client) in main.on_ready()
#   BEFORE old buttons will keep working after redeploy.
#
# MAIN.PY (drop-in):
#   travelerlogs_module.register_views(client)
#   travelerlogs_module.setup_travelerlog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
#   asyncio.create_task(travelerlogs_module.ensure_write_panels(client, guild_id=GUILD_ID))
#
# ENV (optional):
#   TRAVELERLOGS_TEST_CHANNEL_ID=1462402354535075890
#   TRAVELERLOGS_EXCLUDED_CHANNEL_IDS=1462539723112321218,1437457789164191939,...
#   TRAVELERLOGS_MAX_IMAGES=1   (set to 1 if you want single image only)
#   TRAVELERLOGS_IMAGE_TIMEOUT_SECONDS=180
#   TRAVELERLOGS_PANEL_TITLE="✒️ Write a Traveler Log"
#   TRAVELERLOGS_PANEL_DESC="Tap the button below to write a Traveler Log."
#   TRAVELERLOGS_PANEL_PIN=1

import os
import io
import re
import time
import asyncio
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict

import discord
from discord import app_commands

import time_module  # must expose get_time_state() returning dict with year/day at least


# -------------------------
# CONFIG
# -------------------------
TEST_CHANNEL_ID = int(os.getenv("TRAVELERLOGS_TEST_CHANNEL_ID", "1462402354535075890"))

_EXCL_RAW = os.getenv("TRAVELERLOGS_EXCLUDED_CHANNEL_IDS", "")
EXCLUDED_CHANNEL_IDS = set()
for part in [p.strip() for p in _EXCL_RAW.split(",") if p.strip()]:
    try:
        EXCLUDED_CHANNEL_IDS.add(int(part))
    except Exception:
        pass

MAX_IMAGES = int(os.getenv("TRAVELERLOGS_MAX_IMAGES", "1"))  # "gallery" isn't truly possible; see notes below
IMAGE_TIMEOUT_SECONDS = int(os.getenv("TRAVELERLOGS_IMAGE_TIMEOUT_SECONDS", "180"))

PANEL_TITLE = os.getenv("TRAVELERLOGS_PANEL_TITLE", "✒️ Write a Traveler Log")
PANEL_DESC = os.getenv("TRAVELERLOGS_PANEL_DESC", "Tap the button below to write a Traveler Log.")
PANEL_PIN = os.getenv("TRAVELERLOGS_PANEL_PIN", "1").lower() in ("1", "true", "yes", "on")

LOG_EMBED_COLOR = 0x8B5CF6  # purple
PANEL_EMBED_COLOR = 0x8B5CF6

# Persistent custom_id values (do NOT change once deployed or old buttons break)
CID_WRITE = "travelerlogs:write"
CID_EDIT = "travelerlogs:edit"
CID_ADD_IMAGES = "travelerlogs:addimages"

# For parsing "Logged by X" footer and author gating
FOOTER_AUTHOR_TAG = "Logged by "

# Stores pending "add images" sessions keyed by (channel_id, user_id)
_pending_image_sessions: Dict[Tuple[int, int], "ImageSession"] = {}


# -------------------------
# HELPERS
# -------------------------
def _now_ts() -> int:
    return int(time.time())

def _safe_int(s: str, default: int) -> int:
    try:
        return int(str(s).strip())
    except Exception:
        return default

def _get_current_year_day() -> Tuple[int, int]:
    """
    Pull current Year/Day from time_module.
    If time_module isn't ready, returns (1,1).
    """
    try:
        state = time_module.get_time_state()
        year = _safe_int(state.get("year", 1), 1)
        day = _safe_int(state.get("day", 1), 1)
        return year, day
    except Exception:
        return 1, 1

def _chunk_text(text: str, limit: int) -> List[str]:
    """
    Splits text into <= limit chunks, preferring newline boundaries.
    """
    text = text or ""
    if len(text) <= limit:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        # try to break at newline
        if end < len(text):
            nl = text.rfind("\n", start, end)
            if nl > start + 200:  # don't create tiny chunks
                end = nl + 1
        chunks.append(text[start:end].rstrip())
        start = end
    return chunks

def _is_in_scope_channel(channel: discord.abc.GuildChannel) -> bool:
    """
    Testing mode: only TEST_CHANNEL_ID has the panel.
    Also respects EXCLUDED_CHANNEL_IDS.
    """
    if channel is None:
        return False
    if getattr(channel, "id", None) in EXCLUDED_CHANNEL_IDS:
        return False
    return getattr(channel, "id", None) == TEST_CHANNEL_ID

def _panel_embed() -> discord.Embed:
    e = discord.Embed(title=PANEL_TITLE, description=PANEL_DESC, color=PANEL_EMBED_COLOR)
    e.add_field(name="Tap the button", value="• A form will open", inline=False)
    return e

def _build_log_embed(
    author_name: str,
    year: int,
    day: int,
    title: str,
    body: str,
    image_count: int = 0,
) -> discord.Embed:
    e = discord.Embed(title="📖 Traveler Log", color=LOG_EMBED_COLOR)
    e.add_field(name="🗓️ Solunaris Time", value=f"**Year {year} • Day {day}**", inline=False)
    e.add_field(name=title or "Untitled", value=body or "—", inline=False)

    if image_count > 0:
        e.add_field(
            name="📸 Images",
            value="\n".join([f"[Image {i+1}](attachment://image{i+1}.png)" for i in range(image_count)]),
            inline=False,
        )

    e.set_footer(text=f"{FOOTER_AUTHOR_TAG}{author_name}")
    return e

def _extract_author_name_from_footer(embed: discord.Embed) -> Optional[str]:
    try:
        if embed.footer and embed.footer.text and embed.footer.text.startswith(FOOTER_AUTHOR_TAG):
            return embed.footer.text[len(FOOTER_AUTHOR_TAG):].strip()
    except Exception:
        pass
    return None

def _make_log_view() -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(EditLogButton())
    v.add_item(AddImagesButton())
    return v

def _make_panel_view() -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(WriteLogButton())
    return v

async def _find_existing_panel_message(channel: discord.TextChannel) -> Optional[discord.Message]:
    """
    Look for our panel message in recent history.
    """
    try:
        async for msg in channel.history(limit=50):
            if msg.author.bot and msg.embeds:
                emb = msg.embeds[0]
                if emb.title == PANEL_TITLE:
                    # must have our write button
                    if msg.components:
                        return msg
    except Exception:
        pass
    return None


# -------------------------
# IMAGE SESSION
# -------------------------
@dataclass
class ImageSession:
    log_message_id: int
    expires_at: int
    collected: List[discord.Attachment]


# -------------------------
# MODALS
# -------------------------
class WriteLogModal(discord.ui.Modal, title="Write a Traveler Log"):
    year = discord.ui.TextInput(label="Year (number)", required=True, max_length=6)
    day = discord.ui.TextInput(label="Day (number)", required=True, max_length=6)
    log_title = discord.ui.TextInput(label="Title", required=True, max_length=100)
    log_body = discord.ui.TextInput(label="Log", required=True, style=discord.TextStyle.paragraph, max_length=4000)

    def __init__(self, default_year: int, default_day: int):
        super().__init__()
        self.year.default = str(default_year)
        self.day.default = str(default_day)

    async def on_submit(self, interaction: discord.Interaction):
        year = _safe_int(self.year.value, 1)
        day = _safe_int(self.day.value, 1)
        title = str(self.log_title.value).strip()
        body = str(self.log_body.value).strip()

        author_name = interaction.user.display_name

        # Discord embed field value limit is 1024; but our body is in a field,
        # so we must split it (auto-continuation).
        body_chunks = _chunk_text(body, 1000)

        first_embed = _build_log_embed(
            author_name=author_name,
            year=year,
            day=day,
            title=title,
            body=body_chunks[0],
            image_count=0,
        )

        view = _make_log_view()

        # Post first message
        msg = await interaction.channel.send(embed=first_embed, view=view)

        # Continuation messages (no buttons)
        if len(body_chunks) > 1:
            for idx, chunk in enumerate(body_chunks[1:], start=2):
                cont = discord.Embed(
                    title=f"📖 Traveler Log (cont. {idx}/{len(body_chunks)})",
                    description=chunk,
                    color=LOG_EMBED_COLOR,
                )
                cont.set_footer(text=f"{FOOTER_AUTHOR_TAG}{author_name}")
                await interaction.channel.send(embed=cont)

        await interaction.response.send_message("✅ Traveler log recorded.", ephemeral=True)


class EditLogModal(discord.ui.Modal, title="Edit Traveler Log"):
    year = discord.ui.TextInput(label="Year (number)", required=True, max_length=6)
    day = discord.ui.TextInput(label="Day (number)", required=True, max_length=6)
    log_title = discord.ui.TextInput(label="Title", required=True, max_length=100)
    log_body = discord.ui.TextInput(label="Log", required=True, style=discord.TextStyle.paragraph, max_length=4000)

    def __init__(self, message: discord.Message, current_year: int, current_day: int, current_title: str, current_body: str):
        super().__init__()
        self._message = message
        self.year.default = str(current_year)
        self.day.default = str(current_day)
        self.log_title.default = current_title or ""
        self.log_body.default = current_body or ""

    async def on_submit(self, interaction: discord.Interaction):
        year = _safe_int(self.year.value, 1)
        day = _safe_int(self.day.value, 1)
        title = str(self.log_title.value).strip()
        body = str(self.log_body.value).strip()

        author_name = interaction.user.display_name

        # Preserve any existing image attachments already on the message (if any)
        # We'll keep showing the FIRST attached image only (Discord embeds can't do a true gallery)
        attachments = list(self._message.attachments)
        image_files: List[discord.File] = []
        image_count = 0

        # If there are already attachments, we'll keep them by NOT re-uploading new ones.
        # We'll only reference the first one for embed.set_image().
        image_url = None
        if attachments:
            # Use the CDN URL, not attachment://, because we're not re-uploading here
            image_url = attachments[0].url
            image_count = min(len(attachments), MAX_IMAGES)

        chunks = _chunk_text(body, 1000)
        emb = _build_log_embed(author_name, year, day, title, chunks[0], image_count=image_count)
        if image_url:
            emb.set_image(url=image_url)

        await self._message.edit(embed=emb, view=_make_log_view())
        await interaction.response.send_message("✅ Log updated.", ephemeral=True)

        # NOTE: continuation messages aren't tracked/edited here; to keep it simple,
        # edits update the main embed only.


# -------------------------
# BUTTONS
# -------------------------
class WriteLogButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="✒️ Write Log",
            style=discord.ButtonStyle.primary,
            custom_id=CID_WRITE,
        )

    async def callback(self, interaction: discord.Interaction):
        # Default Year/Day from time system, but user can override in modal
        y, d = _get_current_year_day()
        await interaction.response.send_modal(WriteLogModal(default_year=y, default_day=d))


class EditLogButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="✏️ Edit Log",
            style=discord.ButtonStyle.secondary,
            custom_id=CID_EDIT,
        )

    async def callback(self, interaction: discord.Interaction):
        msg = interaction.message
        if not msg or not msg.embeds:
            return await interaction.response.send_message("❌ Can't edit this message.", ephemeral=True)

        emb = msg.embeds[0]
        author_name = _extract_author_name_from_footer(emb)
        if author_name is None:
            return await interaction.response.send_message("❌ Can't verify log author.", ephemeral=True)

        # Only author can edit (by display name match; if you want stronger, store user_id in footer too)
        if interaction.user.display_name != author_name:
            return await interaction.response.send_message("❌ Only the log author can edit this.", ephemeral=True)

        # Pull current values from embed
        year = 1
        day = 1
        title = "Untitled"
        body = ""

        try:
            # first field is Solunaris Time
            if emb.fields and len(emb.fields) >= 2:
                time_val = emb.fields[0].value or ""
                # "**Year X • Day Y**"
                m = re.search(r"Year\s+(\d+)\s+•\s+Day\s+(\d+)", time_val)
                if m:
                    year = _safe_int(m.group(1), 1)
                    day = _safe_int(m.group(2), 1)

                title = emb.fields[1].name or "Untitled"
                body = emb.fields[1].value or ""
        except Exception:
            pass

        await interaction.response.send_modal(EditLogModal(msg, year, day, title, body))


class AddImagesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="📸 Add Images",
            style=discord.ButtonStyle.success,
            custom_id=CID_ADD_IMAGES,
        )

    async def callback(self, interaction: discord.Interaction):
        msg = interaction.message
        if not msg or not msg.embeds:
            return await interaction.response.send_message("❌ Can't attach images to this.", ephemeral=True)

        emb = msg.embeds[0]
        author_name = _extract_author_name_from_footer(emb)
        if author_name is None:
            return await interaction.response.send_message("❌ Can't verify log author.", ephemeral=True)

        if interaction.user.display_name != author_name:
            return await interaction.response.send_message("❌ Only the log author can add images.", ephemeral=True)

        key = (interaction.channel_id, interaction.user.id)
        _pending_image_sessions[key] = ImageSession(
            log_message_id=msg.id,
            expires_at=_now_ts() + IMAGE_TIMEOUT_SECONDS,
            collected=[],
        )

        await interaction.response.send_message(
            f"📸 Send up to **{MAX_IMAGES}** image(s) in this channel now.\n"
            f"I’ll attach them to your log and delete your upload message.\n"
            f"Timeout: {IMAGE_TIMEOUT_SECONDS}s",
            ephemeral=True,
        )


# -------------------------
# VIEWS REGISTRATION (persistent)
# -------------------------
def register_views(client: discord.Client):
    """
    Must be called in on_ready BEFORE users click old buttons.
    This keeps buttons working after redeploys.
    """
    # Register panel view (Write Log button)
    client.add_view(_make_panel_view())
    # Register log view (Edit + Add Images)
    client.add_view(_make_log_view())


# -------------------------
# MESSAGE LISTENER (for image uploads)
# -------------------------
async def handle_message_for_images(message: discord.Message):
    """
    Call this from main.py on_message if you want image uploading to work.
    It detects pending sessions and processes attachments.
    """
    if message.author.bot:
        return
    if not message.guild:
        return
    if not isinstance(message.channel, discord.TextChannel):
        return

    key = (message.channel.id, message.author.id)
    sess = _pending_image_sessions.get(key)
    if not sess:
        return

    # Expired?
    if _now_ts() > sess.expires_at:
        _pending_image_sessions.pop(key, None)
        return

    # Only accept images
    atts = [a for a in message.attachments if (a.content_type or "").startswith("image/")]
    if not atts:
        return

    # Collect up to MAX_IMAGES total
    remaining = MAX_IMAGES - len(sess.collected)
    sess.collected.extend(atts[:max(0, remaining)])

    # Delete the user's upload message to keep channel clean
    try:
        await message.delete()
    except Exception:
        pass

    # If reached max, finalize immediately
    if len(sess.collected) >= MAX_IMAGES:
        await _finalize_images(message.channel, message.author, sess)
        _pending_image_sessions.pop(key, None)
        return

    # Otherwise keep waiting (no extra spam)


async def _finalize_images(channel: discord.TextChannel, user: discord.Member, sess: ImageSession):
    """
    Downloads images and edits the log message with attachments so embeds render.
    Discord cannot show a real gallery in a single embed;
    We will display ONLY the first image in the embed, and keep the rest as attachments.
    """
    try:
        log_msg = await channel.fetch_message(sess.log_message_id)
    except Exception:
        return

    if not log_msg.embeds:
        return

    emb = log_msg.embeds[0]

    # Build files list (download bytes)
    files: List[discord.File] = []
    for idx, att in enumerate(sess.collected[:MAX_IMAGES], start=1):
        try:
            data = await att.read()
        except Exception:
            continue
        # Use consistent filename so attachment:// works
        filename = f"image{idx}.png"
        files.append(discord.File(fp=io.BytesIO(data), filename=filename))

    if not files:
        return

    # Update embed: add images field + set first image preview
    # IMPORTANT: When using attachment://, you must upload files on the SAME edit.
    # Also, discord.py uses `files=` for uploads on edit().
    image_count = len(files)
    # Rebuild embed (keep year/day/title/body from existing)
    author_name = _extract_author_name_from_footer(emb) or user.display_name

    # Extract year/day/title/body from current embed
    year = 1
    day = 1
    title = "Untitled"
    body = "—"
    try:
        if emb.fields and len(emb.fields) >= 2:
            m = re.search(r"Year\s+(\d+)\s+•\s+Day\s+(\d+)", emb.fields[0].value or "")
            if m:
                year = _safe_int(m.group(1), 1)
                day = _safe_int(m.group(2), 1)
            title = emb.fields[1].name or "Untitled"
            body = emb.fields[1].value or "—"
    except Exception:
        pass

    new_emb = _build_log_embed(author_name, year, day, title, body, image_count=image_count)

    # Show only the FIRST image in the embed (Discord limitation)
    new_emb.set_image(url="attachment://image1.png")

    try:
        await log_msg.edit(embed=new_emb, view=_make_log_view(), files=files)
    except Exception as e:
        # If edit fails, swallow to avoid crashing loops
        print(f"[travelerlogs] attach images edit error: {e}")


# -------------------------
# COMMANDS
# -------------------------
def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int):
    """
    Optional commands:
    - /writelog (fallback)
    - /postlogbutton (admin role only) posts panel in current channel
    """

    @tree.command(
        name="writelog",
        description="Write a traveler log (fallback; button is preferred)",
        guild=discord.Object(id=guild_id),
    )
    @app_commands.describe(
        year="Year number",
        day="Day number",
        title="Short title",
        entry="Your log text",
    )
    async def writelog(interaction: discord.Interaction, year: int, day: int, title: str, entry: str):
        author_name = interaction.user.display_name
        chunks = _chunk_text(entry, 1000)

        emb = _build_log_embed(author_name, year, day, title, chunks[0], image_count=0)
        await interaction.channel.send(embed=emb, view=_make_log_view())

        if len(chunks) > 1:
            for idx, chunk in enumerate(chunks[1:], start=2):
                cont = discord.Embed(
                    title=f"📖 Traveler Log (cont. {idx}/{len(chunks)})",
                    description=chunk,
                    color=LOG_EMBED_COLOR,
                )
                cont.set_footer(text=f"{FOOTER_AUTHOR_TAG}{author_name}")
                await interaction.channel.send(embed=cont)

        await interaction.response.send_message("✅ Traveler log recorded.", ephemeral=True)

    def _is_admin(interaction: discord.Interaction) -> bool:
        try:
            if interaction.user.guild_permissions.administrator:
                return True
            if isinstance(interaction.user, discord.Member):
                return any(r.id == admin_role_id for r in interaction.user.roles)
        except Exception:
            pass
        return False

    @tree.command(
        name="postlogbutton",
        description="(Admin) Post the Write Log panel in this channel",
        guild=discord.Object(id=guild_id),
    )
    async def postlogbutton(interaction: discord.Interaction):
        if not _is_admin(interaction):
            return await interaction.response.send_message("❌ Admin only.", ephemeral=True)

        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Use this in a text channel.", ephemeral=True)

        if interaction.channel.id in EXCLUDED_CHANNEL_IDS:
            return await interaction.response.send_message("ℹ️ This channel is excluded.", ephemeral=True)

        existing = await _find_existing_panel_message(interaction.channel)
        if existing:
            return await interaction.response.send_message("ℹ️ Panel already exists here (or I couldn't post it).", ephemeral=True)

        panel = await interaction.channel.send(embed=_panel_embed(), view=_make_panel_view())
        if PANEL_PIN:
            try:
                await panel.pin(reason="Traveler Log panel")
            except Exception:
                pass

        await interaction.response.send_message("✅ Panel posted.", ephemeral=True)


# -------------------------
# PANEL ENSURE (for test channel)
# -------------------------
async def ensure_write_panels(client: discord.Client, guild_id: int):
    """
    Ensures the Write Log panel exists in the TEST_CHANNEL_ID only.
    (You asked to keep it only in test while tweaking.)
    """
    try:
        guild = client.get_guild(guild_id) or await client.fetch_guild(guild_id)
    except Exception as e:
        print(f"[travelerlogs] ensure_write_panels: can't get guild: {e}")
        return

    try:
        ch = guild.get_channel(TEST_CHANNEL_ID) or await client.fetch_channel(TEST_CHANNEL_ID)
    except Exception as e:
        print(f"[travelerlogs] ensure_write_panels: can't get test channel: {e}")
        return

    if not isinstance(ch, discord.TextChannel):
        return
    if ch.id in EXCLUDED_CHANNEL_IDS:
        return

    existing = await _find_existing_panel_message(ch)
    if existing:
        return

    try:
        panel = await ch.send(embed=_panel_embed(), view=_make_panel_view())
        if PANEL_PIN:
            try:
                await panel.pin(reason="Traveler Log panel")
            except Exception:
                pass
        print(f"[travelerlogs] ✅ posted panel in test channel #{ch.name}")
    except Exception as e:
        print(f"[travelerlogs] ensure_write_panels: post error: {e}")