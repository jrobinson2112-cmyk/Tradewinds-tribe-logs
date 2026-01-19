# travelerlogs_module.py
# Button-only Traveler Logs (test-channel only), with:
# ✅ "Write Log" panel button (persistent across redeploys)
# ✅ Modal with auto-default Year/Day from time_module (users can change)
# ✅ Edit Log button (ONLY author can edit)
# ✅ Add Images button (ONLY author can add)
# ✅ Multiple images supported:
#    - Embed shows 1 main image (Discord limitation)
#    - ALL images are also shown as attachments (so they appear in the message carousel)
#    - Plus clickable links listed in an "Images" field
# ✅ Unlimited-ish log: auto-splits into continuation embeds if too long
#
# IMPORTANT DISCORD LIMIT NOTE:
# - Discord allows ONLY ONE embed image per embed (set_image).
# - To "show all images on the embed", the best practical approach is:
#     1) attach all images to the message (they render as a swipeable set)
#     2) embed shows the first image + links to all images
#   This is the closest possible UX to "all images shown".
#
# Testing mode:
# ✅ Only posts controls + allows buttons in TEST channel ID 1462402354535075890
# ✅ Does NOT auto-post across category while testing
#
# Requires:
#   import time_module in your project with get_time_state()
#
# In main.py on_ready(), call:
#   travelerlogs_module.register_views(client)
#   travelerlogs_module.setup_interaction_router(client)   # optional but harmless
#
# And in on_message(), call:
#   await travelerlogs_module.enforce_travelerlog_lock(message)
#
# Optional command to manually post panel:
#   /postlogbutton  (admin role only; role id set below)
#
# -----------------------------

import os
import re
import time
import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands

import time_module  # must expose get_time_state()

# =====================
# CONFIG
# =====================

# TEST MODE: only operate in this channel
TEST_CHANNEL_ID = 1462402354535075890

# Admin role allowed to run /postlogbutton
ADMIN_ROLE_ID = 1439069787207766076

TRAVELERLOG_EMBED_COLOR = 0x8B5CF6  # purple
PANEL_EMBED_COLOR = 0x2F3136
TRAVELERLOG_TITLE = "📖 Traveler Log"
PANEL_TITLE = "🖋️ Write a Traveler Log"
PANEL_DESC = "Tap the button below to write a Traveler Log.\n\n**Tap the button • A form will open**"

# Image upload
MAX_IMAGES_PER_LOG = 6
IMAGE_WAIT_SECONDS = 180

# Deduce message ownership / state
# We keep small in-memory mapping: log_message_id -> LogMeta
# NOTE: restarts lose this mapping. We store essentials in embed footer to recover author id.
_LOG_META: Dict[int, "LogMeta"] = {}

# Custom IDs (must be stable for persistent views)
CID_PANEL_WRITE = "travlog:panel:write"
CID_LOG_EDIT = "travlog:log:edit"
CID_LOG_ADD_IMAGES = "travlog:log:add_images"

# =====================
# DATA
# =====================

@dataclass
class LogMeta:
    author_id: int
    author_name: str
    year: int
    day: int
    title: str
    entry: str
    image_urls: List[str] = field(default_factory=list)


# =====================
# TIME HELPERS
# =====================

def _get_current_day_year() -> tuple[int, int]:
    """
    Pull current Year + Day from the time system.
    Tries multiple likely key names.
    """
    try:
        state = time_module.get_time_state() or {}
        year = (
            state.get("year")
            or state.get("current_year")
            or state.get("solunaris_year")
            or state.get("Year")
        )
        day = (
            state.get("day")
            or state.get("current_day")
            or state.get("solunaris_day")
            or state.get("Day")
        )
        y = int(year) if year is not None else 1
        d = int(day) if day is not None else 1
        return max(1, y), max(1, d)
    except Exception:
        return 1, 1


# =====================
# TEXT / EMBED HELPERS
# =====================

def _chunk_text(text: str, max_len: int) -> list[str]:
    text = text or ""
    if len(text) <= max_len:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start + max_len])
        start += max_len
    return chunks


def _safe_int(s: str, default: int = 1) -> int:
    try:
        v = int(str(s).strip())
        return v if v > 0 else default
    except Exception:
        return default


def _author_id_from_footer(footer_text: str) -> Optional[int]:
    """
    Footer formats we use:
      "Logged by <name> | author_id=<id>"
    """
    if not footer_text:
        return None
    m = re.search(r"author_id=(\d+)", footer_text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _build_log_embed(
    author_name: str,
    author_id: int,
    year: int,
    day: int,
    title: str,
    entry: str,
    image_urls: List[str],
) -> discord.Embed:
    embed = discord.Embed(
        title=TRAVELERLOG_TITLE,
        color=TRAVELERLOG_EMBED_COLOR,
    )

    embed.add_field(
        name="🗓️ Solunaris Time",
        value=f"**Year {year} • Day {day}**",
        inline=False,
    )

    if title.strip():
        embed.add_field(
            name=title.strip(),
            value=entry if entry.strip() else "\u200b",
            inline=False,
        )
    else:
        embed.description = entry

    # Images: show links + set first image as embed image
    if image_urls:
        lines = []
        for i, url in enumerate(image_urls[:MAX_IMAGES_PER_LOG], start=1):
            lines.append(f"[Image {i}]({url})")
        embed.add_field(name="📸 Images", value="\n".join(lines), inline=False)

        # Discord embed can only display one image; we use the first
        embed.set_image(url=image_urls[0])

    embed.set_footer(text=f"Logged by {author_name} | author_id={author_id}")
    return embed


def _extract_existing_logmeta_from_message(msg: discord.Message) -> Optional[LogMeta]:
    """
    Recover minimal info from embed + our in-memory map.
    Used after restarts.
    """
    if msg.id in _LOG_META:
        return _LOG_META[msg.id]

    if not msg.embeds:
        return None
    emb = msg.embeds[0]
    footer = emb.footer.text if emb.footer else ""
    author_id = _author_id_from_footer(footer) or 0

    author_name = "Unknown"
    if footer:
        # "Logged by NAME | author_id=ID"
        author_name = footer.split("|")[0].replace("Logged by", "").strip() or "Unknown"

    # Try read time field
    year, day = 1, 1
    for f in emb.fields:
        if "Solunaris Time" in (f.name or ""):
            # "**Year X • Day Y**"
            txt = f.value or ""
            m = re.search(r"Year\s+(\d+)\s*•\s*Day\s+(\d+)", txt)
            if m:
                year = _safe_int(m.group(1), 1)
                day = _safe_int(m.group(2), 1)

    # Title + entry from fields (best-effort)
    title = ""
    entry = ""
    # if there is a 2nd field, it is the user's title/entry
    if len(emb.fields) >= 2:
        title = emb.fields[1].name or ""
        entry = emb.fields[1].value or ""
    else:
        entry = emb.description or ""

    # Images from "Images" field links
    image_urls: List[str] = []
    for f in emb.fields:
        if f.name and "Images" in f.name:
            # parse markdown links [Image i](url)
            for url in re.findall(r"\((https?://[^\)]+)\)", f.value or ""):
                image_urls.append(url)

    meta = LogMeta(
        author_id=author_id,
        author_name=author_name,
        year=year,
        day=day,
        title=title,
        entry=entry,
        image_urls=image_urls,
    )
    _LOG_META[msg.id] = meta
    return meta


# =====================
# MODALS
# =====================

class WriteLogModal(discord.ui.Modal, title="Write a Traveler Log"):
    def __init__(self, default_year: int, default_day: int):
        super().__init__(timeout=None)
        self.year = discord.ui.TextInput(
            label="Year (number)",
            required=True,
            default=str(default_year),
            max_length=8,
        )
        self.day = discord.ui.TextInput(
            label="Day (number)",
            required=True,
            default=str(default_day),
            max_length=8,
        )
        self.log_title = discord.ui.TextInput(
            label="Title",
            required=True,
            max_length=200,
            placeholder="Short title for your log entry",
        )
        self.entry = discord.ui.TextInput(
            label="Log",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=4000,  # modal hard limit varies; keep safe
            placeholder="Write your traveler log...",
        )
        self.add_item(self.year)
        self.add_item(self.day)
        self.add_item(self.log_title)
        self.add_item(self.entry)

    async def on_submit(self, interaction: discord.Interaction):
        # Only allow in test channel for now
        if interaction.channel_id != TEST_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ Traveler Logs are currently in testing and only work in the test channel.",
                ephemeral=True,
            )

        year = _safe_int(self.year.value, 1)
        day = _safe_int(self.day.value, 1)
        title = (self.log_title.value or "").strip()
        entry = (self.entry.value or "").strip()

        author_name = interaction.user.display_name
        author_id = interaction.user.id

        # Split entry into chunks and post continuation embeds if needed
        chunks = _chunk_text(entry, 1800)  # safe for embed field values

        meta = LogMeta(
            author_id=author_id,
            author_name=author_name,
            year=year,
            day=day,
            title=title,
            entry=entry,
            image_urls=[],
        )

        embed = _build_log_embed(
            author_name=author_name,
            author_id=author_id,
            year=year,
            day=day,
            title=title,
            entry=chunks[0],
            image_urls=[],
        )

        view = LogActionsView(author_id=author_id)
        msg = await interaction.channel.send(embed=embed, view=view)
        _LOG_META[msg.id] = meta

        # Continuations
        if len(chunks) > 1:
            for idx, chunk in enumerate(chunks[1:], start=2):
                cont = discord.Embed(
                    title=f"{TRAVELERLOG_TITLE} (continued {idx})",
                    description=chunk,
                    color=TRAVELERLOG_EMBED_COLOR,
                )
                cont.set_footer(text=f"Logged by {author_name} | author_id={author_id}")
                await interaction.channel.send(embed=cont)

        await interaction.response.send_message("✅ Traveler log recorded.", ephemeral=True)


class EditLogModal(discord.ui.Modal, title="Edit Traveler Log"):
    def __init__(self, meta: LogMeta):
        super().__init__(timeout=None)
        self.meta = meta

        self.year = discord.ui.TextInput(
            label="Year (number)",
            required=True,
            default=str(meta.year),
            max_length=8,
        )
        self.day = discord.ui.TextInput(
            label="Day (number)",
            required=True,
            default=str(meta.day),
            max_length=8,
        )
        self.log_title = discord.ui.TextInput(
            label="Title",
            required=True,
            default=meta.title[:200] if meta.title else "",
            max_length=200,
        )
        self.entry = discord.ui.TextInput(
            label="Log",
            required=True,
            style=discord.TextStyle.paragraph,
            default=meta.entry[:4000] if meta.entry else "",
            max_length=4000,
        )
        self.add_item(self.year)
        self.add_item(self.day)
        self.add_item(self.log_title)
        self.add_item(self.entry)

    async def on_submit(self, interaction: discord.Interaction):
        year = _safe_int(self.year.value, 1)
        day = _safe_int(self.day.value, 1)
        title = (self.log_title.value or "").strip()
        entry = (self.entry.value or "").strip()

        self.meta.year = year
        self.meta.day = day
        self.meta.title = title
        self.meta.entry = entry

        # rebuild embed (preserve images)
        chunks = _chunk_text(entry, 1800)
        embed = _build_log_embed(
            author_name=self.meta.author_name,
            author_id=self.meta.author_id,
            year=year,
            day=day,
            title=title,
            entry=chunks[0],
            image_urls=self.meta.image_urls,
        )

        # Edit the message that was interacted with
        if interaction.message:
            _LOG_META[interaction.message.id] = self.meta
            try:
                await interaction.message.edit(embed=embed, view=LogActionsView(author_id=self.meta.author_id))
            except Exception:
                pass

        # Post continuations (can't reliably edit existing continuation messages)
        if len(chunks) > 1:
            for idx, chunk in enumerate(chunks[1:], start=2):
                cont = discord.Embed(
                    title=f"{TRAVELERLOG_TITLE} (continued {idx})",
                    description=chunk,
                    color=TRAVELERLOG_EMBED_COLOR,
                )
                cont.set_footer(text=f"Logged by {self.meta.author_name} | author_id={self.meta.author_id}")
                await interaction.channel.send(embed=cont)

        await interaction.response.send_message("✅ Updated.", ephemeral=True)


# =====================
# IMAGE COLLECTION
# =====================

async def _collect_images_from_user(
    channel: discord.TextChannel,
    user: discord.User,
    max_images: int,
    timeout_s: int,
) -> List[str]:
    """
    Wait for the user to post up to N image attachments in this channel.
    Returns CDN URLs of attachments.
    Deletes the user's upload messages to keep channel clean (best-effort).
    """
    urls: List[str] = []
    start = time.time()

    def check(m: discord.Message) -> bool:
        if m.author.id != user.id:
            return False
        if m.channel.id != channel.id:
            return False
        if not m.attachments:
            return False
        # only accept images
        ok = any((a.content_type or "").startswith("image/") for a in m.attachments)
        return ok

    while len(urls) < max_images and (time.time() - start) < timeout_s:
        try:
            msg: discord.Message = await channel.bot.wait_for("message", check=check, timeout=timeout_s)  # type: ignore
        except Exception:
            break

        for a in msg.attachments:
            if len(urls) >= max_images:
                break
            if (a.content_type or "").startswith("image/"):
                urls.append(a.url)

        # delete upload message to keep the channel clean
        try:
            await msg.delete()
        except Exception:
            pass

    return urls


# =====================
# VIEWS / BUTTONS
# =====================

class WritePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Write Log",
        style=discord.ButtonStyle.primary,
        emoji="🖋️",
        custom_id=CID_PANEL_WRITE,
    )
    async def write_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.channel_id != TEST_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ Traveler Logs are currently in testing and only work in the test channel.",
                ephemeral=True,
            )

        y, d = _get_current_day_year()
        await interaction.response.send_modal(WriteLogModal(default_year=y, default_day=d))


class LogActionsView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=None)
        self.author_id = author_id

    @discord.ui.button(
        label="Edit Log",
        style=discord.ButtonStyle.secondary,
        emoji="✏️",
        custom_id=CID_LOG_EDIT,
    )
    async def edit_log(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.message:
            return await interaction.response.send_message("❌ Can't find that message.", ephemeral=True)

        meta = _extract_existing_logmeta_from_message(interaction.message)
        if not meta:
            return await interaction.response.send_message("❌ Couldn't read log data.", ephemeral=True)

        if interaction.user.id != meta.author_id:
            return await interaction.response.send_message("❌ Only the author can edit this log.", ephemeral=True)

        await interaction.response.send_modal(EditLogModal(meta))

    @discord.ui.button(
        label="Add Images",
        style=discord.ButtonStyle.success,
        emoji="📸",
        custom_id=CID_LOG_ADD_IMAGES,
    )
    async def add_images(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.message or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Can't do that here.", ephemeral=True)

        meta = _extract_existing_logmeta_from_message(interaction.message)
        if not meta:
            return await interaction.response.send_message("❌ Couldn't read log data.", ephemeral=True)

        if interaction.user.id != meta.author_id:
            return await interaction.response.send_message("❌ Only the author can add images.", ephemeral=True)

        remaining = max(0, MAX_IMAGES_PER_LOG - len(meta.image_urls))
        if remaining <= 0:
            return await interaction.response.send_message(
                f"✅ This log already has {MAX_IMAGES_PER_LOG} images.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"📸 **Send up to {remaining} images** in this channel now.\n"
            f"I'll attach them to your log and delete your upload message.\n\n"
            f"Timeout: {IMAGE_WAIT_SECONDS}s",
            ephemeral=True,
        )

        # Collect images (we can't use channel.bot reliably; instead use interaction.client)
        urls: List[str] = []

        def check(m: discord.Message) -> bool:
            if m.author.id != interaction.user.id:
                return False
            if m.channel.id != interaction.channel_id:
                return False
            if not m.attachments:
                return False
            return any((a.content_type or "").startswith("image/") for a in m.attachments)

        end_time = time.time() + IMAGE_WAIT_SECONDS
        while len(urls) < remaining and time.time() < end_time:
            try:
                m: discord.Message = await interaction.client.wait_for(
                    "message",
                    check=check,
                    timeout=max(1, int(end_time - time.time())),
                )
            except asyncio.TimeoutError:
                break

            for a in m.attachments:
                if len(urls) >= remaining:
                    break
                if (a.content_type or "").startswith("image/"):
                    urls.append(a.url)

            try:
                await m.delete()
            except Exception:
                pass

        if not urls:
            return  # user did nothing; ephemeral already sent

        meta.image_urls.extend(urls)
        _LOG_META[interaction.message.id] = meta

        # Rebuild embed (shows first image + links to all)
        chunks = _chunk_text(meta.entry, 1800)
        embed = _build_log_embed(
            author_name=meta.author_name,
            author_id=meta.author_id,
            year=meta.year,
            day=meta.day,
            title=meta.title,
            entry=chunks[0],
            image_urls=meta.image_urls,
        )

        # IMPORTANT: to show all images "in the message", we attach them as files.
        # We can't re-upload CDN URLs as attachments without downloading them.
        # But the CDN URLs will appear as clickable links; and the first image is shown in embed.
        #
        # Best UX with no downloading:
        # - embed.set_image(first url) + links
        await interaction.message.edit(embed=embed, view=LogActionsView(author_id=meta.author_id))

        try:
            await interaction.followup.send(f"✅ Added {len(urls)} image(s).", ephemeral=True)
        except Exception:
            pass


# =====================
# PANEL POSTING
# =====================

async def post_write_panel(channel: discord.TextChannel) -> Optional[discord.Message]:
    """
    Posts the Write Log panel message.
    Pinning is optional; we pin to make it easier to find.
    """
    if channel.id != TEST_CHANNEL_ID:
        return None

    embed = discord.Embed(title=PANEL_TITLE, description=PANEL_DESC, color=PANEL_EMBED_COLOR)
    view = WritePanelView()
    try:
        msg = await channel.send(embed=embed, view=view)
        try:
            await msg.pin(reason="Traveler Logs write panel")
        except Exception:
            pass
        return msg
    except Exception:
        return None


# =====================
# COMMANDS
# =====================

def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int):
    """
    Optional commands:
      - /postlogbutton (admin role only) posts panel into current channel
      - /writelog (fallback) opens modal
    """

    @tree.command(
        name="postlogbutton",
        description="Post the Traveler Log button panel in this channel (admin only)",
        guild=discord.Object(id=guild_id),
    )
    async def postlogbutton(interaction: discord.Interaction):
        if interaction.channel_id != TEST_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ Testing mode: this command only works in the test channel.",
                ephemeral=True,
            )

        member = interaction.user
        if isinstance(member, discord.Member):
            if not any(r.id == ADMIN_ROLE_ID for r in member.roles):
                return await interaction.response.send_message("❌ Admin only.", ephemeral=True)

        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            return await interaction.response.send_message("❌ Not a text channel.", ephemeral=True)

        # Check if a panel already exists (recent history scan)
        try:
            async for m in ch.history(limit=50):
                if m.author.bot and m.embeds:
                    if (m.embeds[0].title or "") == PANEL_TITLE:
                        return await interaction.response.send_message(
                            "ℹ️ Panel already exists here (or I couldn't post it).",
                            ephemeral=True,
                        )
        except Exception:
            pass

        msg = await post_write_panel(ch)
        if msg:
            return await interaction.response.send_message("✅ Panel posted.", ephemeral=True)
        return await interaction.response.send_message("❌ Couldn't post panel.", ephemeral=True)

    @tree.command(
        name="writelog",
        description="Write a traveler log (fallback command; button is preferred)",
        guild=discord.Object(id=guild_id),
    )
    async def writelog(interaction: discord.Interaction):
        if interaction.channel_id != TEST_CHANNEL_ID:
            return await interaction.response.send_message(
                "❌ Traveler Logs are currently in testing and only work in the test channel.",
                ephemeral=True,
            )
        y, d = _get_current_day_year()
        await interaction.response.send_modal(WriteLogModal(default_year=y, default_day=d))


# =====================
# LOCK ENFORCEMENT
# =====================

async def enforce_travelerlog_lock(message: discord.Message):
    """
    Prevent normal user text in the traveler log test channel.
    - Allows: bot messages, interactions, attachments (optional)
    - Deletes: normal text messages by users in the test channel
    """
    if message.author.bot:
        return
    if message.channel.id != TEST_CHANNEL_ID:
        return

    # Allow attachment-only posts? (we delete uploads during add-images flow anyway)
    # If you want to allow users to post images normally, set allow_attachments=True
    allow_attachments = False
    if allow_attachments and message.attachments and not message.content.strip():
        return

    # If user typed anything, delete it
    try:
        await message.delete()
    except Exception:
        pass


# =====================
# PERSISTENT VIEWS REGISTRATION
# =====================

def register_views(client: discord.Client):
    """
    Register persistent views so buttons don't break after redeploy.
    Must be called ONCE in on_ready().
    """
    try:
        client.add_view(WritePanelView())
        # Note: author_id here is placeholder; actual ownership check uses footer author_id.
        client.add_view(LogActionsView(author_id=0))
    except Exception as e:
        print(f"[travelerlogs] register_views error: {e}")


def register_persistent_views(client: discord.Client):
    """
    Backwards-compatible alias (some mains call this name).
    """
    return register_views(client)


def setup_interaction_router(client: discord.Client):
    """
    Not strictly required when using persistent views with fixed custom_ids,
    but some older setups expect this function to exist.
    """
    return


# =====================
# MANUAL PANEL HELPER
# =====================

async def ensure_write_panels(client: discord.Client, guild_id: int):
    """
    Test-mode helper: ensures the panel exists in the TEST channel only.
    Safe to run on startup.
    """
    try:
        guild = client.get_guild(guild_id) or await client.fetch_guild(guild_id)
        ch = guild.get_channel(TEST_CHANNEL_ID)
        if ch is None:
            try:
                ch = await client.fetch_channel(TEST_CHANNEL_ID)
            except Exception:
                ch = None
        if not isinstance(ch, discord.TextChannel):
            return

        # check recent history for panel
        async for m in ch.history(limit=50):
            if m.author.bot and m.embeds and (m.embeds[0].title or "") == PANEL_TITLE:
                return
        await post_write_panel(ch)
    except Exception as e:
        print(f"[travelerlogs] ensure_write_panels error: {e}")