# travelerlogs_module.py
# Button-only Traveler Logs (TEST CHANNEL ONLY) with:
# - Auto default Year/Day from time_module (via TextInput defaults + numeric validation)
# - Users can override Year/Day
# - Edit button (author only)
# - Add Images button: user uploads up to 6 images; bot re-posts as attachments and updates embed links
# - Persistent views (no "interaction failed" after redeploy)

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands

import time_module  # provides get_time_state()


# =====================
# CONFIG
# =====================
TRAVELERLOG_EMBED_COLOR = 0x8B5CF6
TRAVELERLOG_TITLE = "📖 Traveler Log"

# TEST ONLY: only post the write panel here
TRAVELERLOG_TEST_CHANNEL_ID = 1462402354535075890

# (kept for later; when test-only is removed these become relevant again)
EXCLUDED_CHANNEL_IDS = {
    1462539723112321218,
    1437457789164191939,
    1455315150859927663,
    1456386974167466106,
}

# how many images can be attached per log (Discord max attachments per message is higher, but keep it sane)
MAX_IMAGES_PER_LOG = 6
IMAGE_WAIT_TIMEOUT_SEC = 180

# =====================
# INTERNAL STATE
# =====================
# message_id -> author_id
_LOG_OWNERS: Dict[int, int] = {}

# message_id -> list[attachment_url] (the hosted discord CDN urls)
_LOG_IMAGE_URLS: Dict[int, List[str]] = {}

# message_id -> (year, day, title, entry)
_LOG_CONTENT: Dict[int, Tuple[int, int, str, str]] = {}

# used to prevent duplicate panels
_PANEL_MARKER = "TRAVELERLOG_PANEL_V1"

# =====================
# HELPERS
# =====================

def _get_current_day_year() -> Tuple[int, int]:
    try:
        state = time_module.get_time_state()
        y = int(state.get("year", 1))
        d = int(state.get("day", 1))
        return max(1, y), max(1, d)
    except Exception:
        return 1, 1


def _is_int_string(s: str) -> bool:
    return bool(re.fullmatch(r"\d+", (s or "").strip()))


def _safe_int(s: str, default: int = 1) -> int:
    try:
        return int((s or "").strip())
    except Exception:
        return default


def _build_log_embed(
    *,
    author_name: str,
    year: int,
    day: int,
    title: str,
    entry: str,
    image_urls: Optional[List[str]] = None,
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

    embed.add_field(
        name=title,
        value=entry,
        inline=False,
    )

    image_urls = image_urls or []
    if image_urls:
        # show first image inline in embed
        embed.set_image(url=image_urls[0])

        # list links for the rest
        lines = []
        for i, url in enumerate(image_urls, start=1):
            lines.append(f"[Image {i}]({url})")
        embed.add_field(
            name="📸 Images",
            value="\n".join(lines),
            inline=False,
        )

    embed.set_footer(text=f"Logged by {author_name}")
    return embed


async def _fetch_member_display_name(guild: discord.Guild, user_id: int) -> str:
    m = guild.get_member(user_id)
    if m:
        return m.display_name
    try:
        m = await guild.fetch_member(user_id)
        return m.display_name
    except Exception:
        return f"User {user_id}"


def _channel_allowed(channel_id: int) -> bool:
    if channel_id in EXCLUDED_CHANNEL_IDS:
        return False
    return True


# =====================
# MODALS
# =====================

class WriteLogModal(discord.ui.Modal, title="Write a Traveler Log"):
    def __init__(self, *, default_year: int, default_day: int):
        super().__init__(timeout=300)

        # IMPORTANT: TextInput supports default; NumberInput does not.
        self.year = discord.ui.TextInput(
            label="Year (number)",
            required=True,
            max_length=6,
            default=str(default_year),
            placeholder="e.g. 2",
        )
        self.day = discord.ui.TextInput(
            label="Day (number)",
            required=True,
            max_length=6,
            default=str(default_day),
            placeholder="e.g. 329",
        )
        self.title_in = discord.ui.TextInput(
            label="Title",
            required=True,
            max_length=80,
            placeholder="Short title for your log entry",
        )
        self.entry_in = discord.ui.TextInput(
            label="Log",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=1900,
            placeholder="Write your traveler log...",
        )

        self.add_item(self.year)
        self.add_item(self.day)
        self.add_item(self.title_in)
        self.add_item(self.entry_in)

        self.result: Optional[Tuple[int, int, str, str]] = None

    async def on_submit(self, interaction: discord.Interaction):
        y_s = self.year.value
        d_s = self.day.value

        if not _is_int_string(y_s) or not _is_int_string(d_s):
            await interaction.response.send_message(
                "❌ Year and Day must be numbers only.",
                ephemeral=True,
            )
            return

        y = max(1, _safe_int(y_s, 1))
        d = max(1, _safe_int(d_s, 1))
        t = self.title_in.value.strip()
        e = self.entry_in.value.strip()

        self.result = (y, d, t, e)
        await interaction.response.defer(ephemeral=True)


class EditLogModal(discord.ui.Modal, title="Edit Traveler Log"):
    def __init__(self, *, current_year: int, current_day: int, current_title: str, current_entry: str):
        super().__init__(timeout=300)

        self.year = discord.ui.TextInput(
            label="Year (number)",
            required=True,
            max_length=6,
            default=str(current_year),
        )
        self.day = discord.ui.TextInput(
            label="Day (number)",
            required=True,
            max_length=6,
            default=str(current_day),
        )
        self.title_in = discord.ui.TextInput(
            label="Title",
            required=True,
            max_length=80,
            default=current_title[:80],
        )
        self.entry_in = discord.ui.TextInput(
            label="Log",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=1900,
            default=current_entry[:1900],
        )

        self.add_item(self.year)
        self.add_item(self.day)
        self.add_item(self.title_in)
        self.add_item(self.entry_in)

        self.result: Optional[Tuple[int, int, str, str]] = None

    async def on_submit(self, interaction: discord.Interaction):
        if not _is_int_string(self.year.value) or not _is_int_string(self.day.value):
            await interaction.response.send_message("❌ Year and Day must be numbers only.", ephemeral=True)
            return

        y = max(1, _safe_int(self.year.value, 1))
        d = max(1, _safe_int(self.day.value, 1))
        t = self.title_in.value.strip()
        e = self.entry_in.value.strip()

        self.result = (y, d, t, e)
        await interaction.response.defer(ephemeral=True)


# =====================
# VIEWS / BUTTONS
# =====================

class WritePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Write Log",
        emoji="🖋️",
        style=discord.ButtonStyle.primary,
        custom_id="travelerlogs:write",
    )
    async def write(self, interaction: discord.Interaction, button: discord.ui.Button):
        # only allow in test channel for now
        if interaction.channel_id != TRAVELERLOG_TEST_CHANNEL_ID:
            await interaction.response.send_message("❌ Traveler Logs are currently in testing in the test channel only.", ephemeral=True)
            return

        y, d = _get_current_day_year()
        modal = WriteLogModal(default_year=y, default_day=d)
        await interaction.response.send_modal(modal)

        # wait for modal submit (discord.py handles this; just poll result)
        for _ in range(300):
            await asyncio.sleep(0.2)
            if modal.result is not None:
                break

        if modal.result is None:
            return

        year, day, title, entry = modal.result

        author_name = interaction.user.display_name
        embed = _build_log_embed(
            author_name=author_name,
            year=year,
            day=day,
            title=title,
            entry=entry,
            image_urls=[],
        )

        view = LogActionsView(author_id=interaction.user.id)
        msg = await interaction.channel.send(embed=embed, view=view)

        _LOG_OWNERS[msg.id] = interaction.user.id
        _LOG_IMAGE_URLS[msg.id] = []
        _LOG_CONTENT[msg.id] = (year, day, title, entry)

        await interaction.followup.send("✅ Traveler log recorded.", ephemeral=True)


class LogActionsView(discord.ui.View):
    def __init__(self, *, author_id: int):
        super().__init__(timeout=None)
        self.author_id = author_id

    @discord.ui.button(
        label="Edit Log",
        emoji="✏️",
        style=discord.ButtonStyle.secondary,
        custom_id="travelerlogs:edit",
    )
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg = interaction.message
        if not msg:
            await interaction.response.send_message("❌ Can't find the log message.", ephemeral=True)
            return

        owner = _LOG_OWNERS.get(msg.id)
        if owner != interaction.user.id:
            await interaction.response.send_message("❌ Only the author can edit this log.", ephemeral=True)
            return

        year, day, title, entry = _LOG_CONTENT.get(msg.id, (1, 1, "Untitled", ""))
        modal = EditLogModal(current_year=year, current_day=day, current_title=title, current_entry=entry)
        await interaction.response.send_modal(modal)

        for _ in range(300):
            await asyncio.sleep(0.2)
            if modal.result is not None:
                break

        if modal.result is None:
            return

        new_year, new_day, new_title, new_entry = modal.result

        # preserve images
        imgs = _LOG_IMAGE_URLS.get(msg.id, [])
        author_name = interaction.user.display_name

        new_embed = _build_log_embed(
            author_name=author_name,
            year=new_year,
            day=new_day,
            title=new_title,
            entry=new_entry,
            image_urls=imgs,
        )

        await msg.edit(embed=new_embed, view=self)

        _LOG_CONTENT[msg.id] = (new_year, new_day, new_title, new_entry)
        await interaction.followup.send("✅ Log updated.", ephemeral=True)

    @discord.ui.button(
        label="Add Images",
        emoji="📸",
        style=discord.ButtonStyle.success,
        custom_id="travelerlogs:add_images",
    )
    async def add_images(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg = interaction.message
        if not msg:
            await interaction.response.send_message("❌ Can't find the log message.", ephemeral=True)
            return

        owner = _LOG_OWNERS.get(msg.id)
        if owner != interaction.user.id:
            await interaction.response.send_message("❌ Only the author can add images to this log.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"📸 Send up to {MAX_IMAGES_PER_LOG} images in this channel now.\n"
            f"I'll attach them to your log and delete your upload message.\n\n"
            f"Timeout: {IMAGE_WAIT_TIMEOUT_SEC}s",
            ephemeral=True,
        )

        channel = interaction.channel

        collected: List[discord.Attachment] = []

        def check(m: discord.Message) -> bool:
            if m.author.id != interaction.user.id:
                return False
            if m.channel.id != channel.id:
                return False
            return any(a.content_type and a.content_type.startswith("image/") for a in m.attachments)

        start = time.time()
        while time.time() - start < IMAGE_WAIT_TIMEOUT_SEC and len(collected) < MAX_IMAGES_PER_LOG:
            try:
                m: discord.Message = await interaction.client.wait_for("message", check=check, timeout=5.0)
            except asyncio.TimeoutError:
                continue

            imgs = [a for a in m.attachments if (a.content_type or "").startswith("image/")]
            if imgs:
                collected.extend(imgs[: MAX_IMAGES_PER_LOG - len(collected)])

            # delete user upload to keep channel clean (ignore perms issues)
            try:
                await m.delete()
            except Exception:
                pass

        if not collected:
            await interaction.followup.send("❌ No images received in time.", ephemeral=True)
            return

        # IMPORTANT FIX:
        # You cannot "attach images to an existing message" after the fact.
        # So we re-send a new message WITH attachments, then edit the embed on that NEW message,
        # and delete the old one. (Keeps the log looking like one post with the images.)
        #
        # We'll keep the same embed content + view.

        year, day, title, entry = _LOG_CONTENT.get(msg.id, (1, 1, "Untitled", ""))
        author_name = interaction.user.display_name

        # build new image urls (Discord CDN urls)
        # we need to actually post them as attachments first to get stable urls.
        files = [await a.to_file() for a in collected]

        # keep old urls too (append)
        existing_urls = _LOG_IMAGE_URLS.get(msg.id, [])
        # placeholder embed; after send we can read attachments urls from the new message
        temp_embed = _build_log_embed(
            author_name=author_name,
            year=year,
            day=day,
            title=title,
            entry=entry,
            image_urls=existing_urls,
        )

        # send new message with attachments and same buttons
        new_msg = await channel.send(embed=temp_embed, files=files, view=self)

        # now we can read the uploaded attachment URLs from new_msg.attachments
        new_urls = [a.url for a in new_msg.attachments if (a.content_type or "").startswith("image/")]
        all_urls = existing_urls + new_urls
        all_urls = all_urls[:MAX_IMAGES_PER_LOG]

        final_embed = _build_log_embed(
            author_name=author_name,
            year=year,
            day=day,
            title=title,
            entry=entry,
            image_urls=all_urls,
        )
        await new_msg.edit(embed=final_embed, view=self)

        # migrate ownership/content to new message id
        _LOG_OWNERS[new_msg.id] = _LOG_OWNERS.get(msg.id, interaction.user.id)
        _LOG_CONTENT[new_msg.id] = _LOG_CONTENT.get(msg.id, (year, day, title, entry))
        _LOG_IMAGE_URLS[new_msg.id] = all_urls

        # cleanup old message + old state
        try:
            await msg.delete()
        except Exception:
            pass

        _LOG_OWNERS.pop(msg.id, None)
        _LOG_CONTENT.pop(msg.id, None)
        _LOG_IMAGE_URLS.pop(msg.id, None)

        await interaction.followup.send(f"✅ Added {len(new_urls)} image(s).", ephemeral=True)


# =====================
# PANEL CREATION
# =====================

async def _find_existing_panel(channel: discord.TextChannel) -> Optional[discord.Message]:
    try:
        pins = await channel.pins()
        for m in pins:
            if m.author.bot and m.embeds:
                if any((_PANEL_MARKER in (e.description or "")) for e in m.embeds):
                    return m
    except Exception:
        pass
    return None


async def _post_panel(channel: discord.TextChannel) -> Optional[discord.Message]:
    if not _channel_allowed(channel.id):
        return None

    # test-only mode
    if channel.id != TRAVELERLOG_TEST_CHANNEL_ID:
        return None

    embed = discord.Embed(
        title="🖋️ Write a Traveler Log",
        description=(
            "Tap the button below to write a Traveler Log.\n\n"
            "**Tap the button • A form will open**\n\n"
            f"`{_PANEL_MARKER}`"
        ),
        color=TRAVELERLOG_EMBED_COLOR,
    )
    view = WritePanelView()

    try:
        msg = await channel.send(embed=embed, view=view)
        try:
            await msg.pin(reason="Traveler Log write panel")
        except Exception:
            pass
        return msg
    except Exception:
        return None


async def ensure_write_panels(client: discord.Client, guild_id: int):
    """
    Ensures the Write Log panel exists in the TEST CHANNEL only.
    Safe + low rate usage.
    """
    await client.wait_until_ready()
    guild = client.get_guild(guild_id)
    if guild is None:
        return

    ch = guild.get_channel(TRAVELERLOG_TEST_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        try:
            ch = await client.fetch_channel(TRAVELERLOG_TEST_CHANNEL_ID)
        except Exception:
            return
    if not isinstance(ch, discord.TextChannel):
        return

    existing = await _find_existing_panel(ch)
    if existing is None:
        await _post_panel(ch)


# =====================
# PERSISTENT VIEWS
# =====================

def register_views(client: discord.Client):
    """
    Must be called on startup BEFORE you expect old buttons to work after redeploy.
    """
    client.add_view(WritePanelView())
    # LogActionsView is per-author, but custom_id is static so we can register a generic one too.
    # The author check is done using _LOG_OWNERS at runtime.
    client.add_view(LogActionsView(author_id=0))


# =====================
# OPTIONAL SLASH COMMAND (fallback)
# =====================

def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int):
    @tree.command(
        name="postlogbutton",
        description="(Admin) Post the Write Log button panel in this channel",
        guild=discord.Object(id=guild_id),
    )
    async def postlogbutton(interaction: discord.Interaction):
        # allow anyone during test? keep as-is; you can add role check in main if you want
        if interaction.channel_id != TRAVELERLOG_TEST_CHANNEL_ID:
            await interaction.response.send_message("❌ Test-only: can only post in the test channel for now.", ephemeral=True)
            return

        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("❌ Not a text channel.", ephemeral=True)
            return

        existing = await _find_existing_panel(ch)
        if existing:
            await interaction.response.send_message("ℹ️ Panel already exists here (or I couldn't post it).", ephemeral=True)
            return

        msg = await _post_panel(ch)
        if msg:
            await interaction.response.send_message("✅ Panel posted.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Couldn't post panel (permissions?).", ephemeral=True)


# =====================
# LOCK ENFORCEMENT (TEST CHANNEL ONLY)
# =====================

async def enforce_travelerlog_lock(message: discord.Message):
    """
    Deletes normal messages in the TEST channel (so people can't write normal text),
    while still allowing:
    - bot embeds
    - the panel message
    - user image uploads during the add-images flow (we delete those ourselves)
    """
    if message.author.bot:
        return

    if message.channel.id != TRAVELERLOG_TEST_CHANNEL_ID:
        return

    # allow anything with attachments (images) so add-images flow works
    if message.attachments:
        return

    # allow commands (in case you still use /postlogbutton etc)
    if message.content.startswith("/"):
        return

    try:
        await message.delete()
    except Exception:
        pass