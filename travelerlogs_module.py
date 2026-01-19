# travelerlogs_module.py
# Button-only Traveler Logs (test channel only) with:
# - Live Year/Day defaults pulled at button click time from time_module.get_time_state()
# - Edit Log button (author only)
# - Add Images button (author only) -> user uploads images in channel, bot attaches to embed
# - Auto-continuation for long logs (splits into multiple embeds/messages)
# - Persistent views to prevent "interaction failed" after redeploy

import os
import re
import time
import asyncio
from typing import List, Dict, Optional, Tuple

import discord
from discord import app_commands

import time_module  # must provide get_time_state()


# =====================
# CONFIG
# =====================
TEST_CHANNEL_ID = int(os.getenv("TRAVELERLOGS_TEST_CHANNEL_ID", "1462402354535075890"))

TRAVELERLOG_EMBED_COLOR = 0x8B5CF6  # purple
WRITE_PANEL_TITLE = "🖋️ Write a Traveler Log"
WRITE_PANEL_DESC = "Tap the button below to write a Traveler Log.\n\n**Tap the button • A form will open**"

MAX_IMAGES_PER_LOG = int(os.getenv("TRAVELERLOGS_MAX_IMAGES", "6"))
IMAGE_COLLECT_TIMEOUT = int(os.getenv("TRAVELERLOGS_IMAGE_TIMEOUT", "180"))

# Discord embed limits:
# - description: 4096
# - total embed: 6000-ish. We'll keep each chunk <= 3500 for safety.
LOG_CHUNK_SIZE = int(os.getenv("TRAVELERLOGS_LOG_CHUNK_SIZE", "3500"))

# Custom IDs must be stable across redeploys for persistent views
CID_WRITE = "travlog:write"
CID_EDIT = "travlog:edit"
CID_ADDIMG = "travlog:addimg"


# =====================
# RUNTIME STATE
# =====================
# log_message_id -> author_id
_LOG_AUTHOR: Dict[int, int] = {}

# log_message_id -> list of image URLs
_LOG_IMAGES: Dict[int, List[str]] = {}

# user_id -> active "collect images" session info
# { "log_message_id": int, "channel_id": int, "until": float, "count": int }
_IMAGE_SESSIONS: Dict[int, Dict] = {}

# store the panel message id per channel (so we can re-create if deleted)
_PANEL_MESSAGE_ID: Dict[int, int] = {}


# =====================
# TIME HELPERS
# =====================
def _get_current_year_day() -> Tuple[int, int]:
    """
    Pull current Year/Day from time_module.get_time_state().
    This MUST be called at interaction time (button click), not at import.
    """
    try:
        state = time_module.get_time_state()
        year = int(state.get("year", 1))
        day = int(state.get("day", 1))
        return year, day
    except Exception:
        return 1, 1


# =====================
# EMBED BUILDING
# =====================
def _build_log_embeds(
    year: int,
    day: int,
    title: str,
    body: str,
    author_name: str,
    images: Optional[List[str]] = None,
) -> List[discord.Embed]:
    """
    Build one or more embeds (auto-continuation) to fit long logs.
    Shows all images as links + sets first image as preview (Discord only allows 1 embed image).
    """
    images = images or []

    # Chunk body
    chunks = []
    text = body or ""
    while text:
        chunks.append(text[:LOG_CHUNK_SIZE])
        text = text[LOG_CHUNK_SIZE:]

    if not chunks:
        chunks = [""]

    embeds: List[discord.Embed] = []
    for i, chunk in enumerate(chunks):
        is_first = (i == 0)
        emb = discord.Embed(
            title="📖 Traveler Log" if is_first else f"📖 Traveler Log (cont. {i+1})",
            color=TRAVELERLOG_EMBED_COLOR,
        )

        if is_first:
            emb.add_field(
                name="🗓️ Solunaris Time",
                value=f"**Year {year} • Day {day}**",
                inline=False,
            )
            emb.add_field(name=title, value=chunk or "\u200b", inline=False)
        else:
            emb.add_field(name=f"{title} (cont.)", value=chunk or "\u200b", inline=False)

        if is_first and images:
            # show links list
            links = "\n".join([f"[Image {idx+1}]({url})" for idx, url in enumerate(images)])
            emb.add_field(name="📸 Images", value=links, inline=False)

            # show first image as preview
            emb.set_image(url=images[0])

        emb.set_footer(text=f"Logged by {author_name}")
        embeds.append(emb)

    return embeds


class LogActionsView(discord.ui.View):
    """
    Buttons that live under the log embed.
    """
    def __init__(self, log_message_id: int):
        super().__init__(timeout=None)
        self.log_message_id = log_message_id

    @discord.ui.button(label="Edit Log", style=discord.ButtonStyle.secondary, emoji="✏️", custom_id=CID_EDIT)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        author_id = _LOG_AUTHOR.get(self.log_message_id)
        if author_id is None or interaction.user.id != author_id:
            await interaction.response.send_message("❌ Only the author can edit this log.", ephemeral=True)
            return

        # Pull current embed info to prefill title/body where possible
        msg = interaction.message
        if not msg or not msg.embeds:
            await interaction.response.send_message("❌ Could not load the log embeds.", ephemeral=True)
            return

        # Best-effort parse title/body from first embed
        emb = msg.embeds[0]
        parsed_title = "Log"
        parsed_body = ""
        if emb.fields:
            # field 0 = time, field 1 = title/body
            if len(emb.fields) >= 2:
                parsed_title = emb.fields[1].name or "Log"
                parsed_body = emb.fields[1].value or ""

        # Prefer stored year/day? We'll parse from time field
        year, day = _extract_year_day_from_embed(emb) or _get_current_year_day()

        modal = WriteTravelerLogModal(
            year=year,
            day=day,
            default_title=parsed_title,
            default_body=parsed_body,
            editing_log_message_id=self.log_message_id
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Add Images", style=discord.ButtonStyle.success, emoji="📸", custom_id=CID_ADDIMG)
    async def addimg_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        author_id = _LOG_AUTHOR.get(self.log_message_id)
        if author_id is None or interaction.user.id != author_id:
            await interaction.response.send_message("❌ Only the author can add images to this log.", ephemeral=True)
            return

        # Start an image collection session for this user
        _IMAGE_SESSIONS[interaction.user.id] = {
            "log_message_id": self.log_message_id,
            "channel_id": interaction.channel_id,
            "until": time.time() + IMAGE_COLLECT_TIMEOUT,
            "count": 0,
        }

        await interaction.response.send_message(
            f"📸 Send up to **{MAX_IMAGES_PER_LOG}** images in this channel now.\n"
            f"I'll attach them to your log and (optionally) delete your upload message.\n"
            f"Timeout: {IMAGE_COLLECT_TIMEOUT}s",
            ephemeral=True,
        )


def _extract_year_day_from_embed(embed: discord.Embed) -> Optional[Tuple[int, int]]:
    """
    Parse 'Year X • Day Y' from the time field.
    """
    if not embed.fields:
        return None
    # find the time field
    for f in embed.fields:
        if "Solunaris Time" in (f.name or ""):
            m = re.search(r"Year\s+(\d+)\s*•\s*Day\s+(\d+)", f.value or "")
            if m:
                return int(m.group(1)), int(m.group(2))
    return None


class WriteTravelerLogModal(discord.ui.Modal):
    """
    IMPORTANT: defaults must be set in __init__ (dynamic), not as class attrs.
    """
    def __init__(
        self,
        year: int,
        day: int,
        default_title: str = "",
        default_body: str = "",
        editing_log_message_id: Optional[int] = None,
    ):
        super().__init__(title="Write a Traveler Log")

        self.editing_log_message_id = editing_log_message_id

        self.year_input = discord.ui.TextInput(
            label="Year (number)",
            default=str(year),
            required=True,
        )
        self.day_input = discord.ui.TextInput(
            label="Day (number)",
            default=str(day),
            required=True,
        )
        self.title_input = discord.ui.TextInput(
            label="Title",
            default=default_title[:100] if default_title else "",
            placeholder="Short title for your log entry",
            required=True,
            max_length=100,
        )
        self.log_input = discord.ui.TextInput(
            label="Log",
            style=discord.TextStyle.paragraph,
            default=default_body[:4000] if default_body else "",
            placeholder="Write your traveler log…",
            required=True,
            max_length=4000,  # modal text inputs have a max; continuation is done when posting
        )

        self.add_item(self.year_input)
        self.add_item(self.day_input)
        self.add_item(self.title_input)
        self.add_item(self.log_input)

    async def on_submit(self, interaction: discord.Interaction):
        # validate numbers
        try:
            year = int(str(self.year_input.value).strip())
            day = int(str(self.day_input.value).strip())
        except Exception:
            await interaction.response.send_message("❌ Year and Day must be numbers.", ephemeral=True)
            return

        title = str(self.title_input.value).strip() or "Log"
        body = str(self.log_input.value).strip()

        author_name = interaction.user.display_name

        # Editing existing log
        if self.editing_log_message_id:
            # Pull any stored images
            images = _LOG_IMAGES.get(self.editing_log_message_id, [])

            embeds = _build_log_embeds(year, day, title, body, author_name, images=images)

            # Edit the original message (only first embed + view lives there; continuations are not edited)
            try:
                channel = interaction.channel
                if channel is None:
                    channel = await interaction.client.fetch_channel(interaction.channel_id)
                msg = await channel.fetch_message(self.editing_log_message_id)
            except Exception:
                await interaction.response.send_message("❌ Could not find the original log message to edit.", ephemeral=True)
                return

            view = LogActionsView(log_message_id=msg.id)

            # update the message with first embed
            await msg.edit(embed=embeds[0], view=view)

            # If there are continuation embeds, post them as new messages (cannot “attach” neatly to original)
            if len(embeds) > 1:
                for emb in embeds[1:]:
                    await interaction.channel.send(embed=emb)

            await interaction.response.send_message("✅ Log updated.", ephemeral=True)
            return

        # New log post
        images = []  # start with none; user can add afterwards
        embeds = _build_log_embeds(year, day, title, body, author_name, images=images)

        # Send first message with view
        view = LogActionsView(log_message_id=0)  # temp, we’ll set after send
        sent = await interaction.channel.send(embed=embeds[0], view=view)

        # Now that we know message id, store author + fix view binding
        _LOG_AUTHOR[sent.id] = interaction.user.id
        _LOG_IMAGES[sent.id] = []
        view = LogActionsView(log_message_id=sent.id)
        await sent.edit(view=view)

        # Send continuations
        if len(embeds) > 1:
            for emb in embeds[1:]:
                await interaction.channel.send(embed=emb)

        await interaction.response.send_message("✅ Traveler log recorded.", ephemeral=True)


class WritePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Write Log", style=discord.ButtonStyle.primary, emoji="🖋️", custom_id=CID_WRITE)
    async def write_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Only allow in test channel
        if interaction.channel_id != TEST_CHANNEL_ID:
            await interaction.response.send_message("❌ Traveler Logs are currently in testing in the test channel only.", ephemeral=True)
            return

        year, day = _get_current_year_day()

        modal = WriteTravelerLogModal(year=year, day=day)
        await interaction.response.send_modal(modal)


# =====================
# PERSISTENT VIEWS
# =====================
def register_views(client: discord.Client):
    """
    Must be called in on_ready BEFORE panels exist, so old buttons never break.
    """
    client.add_view(WritePanelView())
    # LogActionsView is message-specific, but custom_ids must still be registered:
    # We register a dummy view so Discord routes interactions to this process.
    dummy = discord.ui.View(timeout=None)
    dummy.add_item(discord.ui.Button(label="Edit Log", style=discord.ButtonStyle.secondary, custom_id=CID_EDIT))
    dummy.add_item(discord.ui.Button(label="Add Images", style=discord.ButtonStyle.success, custom_id=CID_ADDIMG))
    client.add_view(dummy)


def setup_interaction_router(client: discord.Client):
    """
    Routes CID_EDIT / CID_ADDIMG to the correct LogActionsView by reading message.id.
    This prevents "interaction failed" on persistent buttons.
    """
    @client.event
    async def on_interaction(interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get("custom_id") if interaction.data else None
        if cid not in (CID_EDIT, CID_ADDIMG):
            return

        # The message being interacted with is the log message
        msg = interaction.message
        if not msg:
            return

        # Ensure author mapping exists (if bot restarted, we can reconstruct minimally)
        if msg.id not in _LOG_AUTHOR:
            # try to infer author from footer "Logged by X" is not reversible to id,
            # so we don't guess; deny edit/addimg until re-posted.
            # (If you want persistence across restarts, we can store to disk.)
            await interaction.response.send_message(
                "❌ This log was created before the latest restart and can't be edited right now.",
                ephemeral=True,
            )
            return

        # Create a live view bound to this message id and dispatch manually
        view = LogActionsView(log_message_id=msg.id)

        if cid == CID_EDIT:
            await view.edit_btn.callback(interaction)  # type: ignore
        elif cid == CID_ADDIMG:
            await view.addimg_btn.callback(interaction)  # type: ignore


# =====================
# WRITE PANEL ENSURE (TEST CHANNEL ONLY)
# =====================
async def ensure_write_panels(client: discord.Client, guild_id: int):
    """
    Ensures the pinned Write Log panel exists in the TEST_CHANNEL only.
    """
    try:
        ch = client.get_channel(TEST_CHANNEL_ID) or await client.fetch_channel(TEST_CHANNEL_ID)
    except Exception as e:
        print(f"[travelerlogs] ❌ cannot fetch test channel: {e}")
        return

    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        print("[travelerlogs] ❌ test channel is not text channel")
        return

    await _ensure_panel_in_channel(ch)


async def _ensure_panel_in_channel(channel: discord.TextChannel):
    """
    Create panel message if missing, pin it, and keep its ID.
    """
    try:
        # If we have a stored ID, check it exists
        mid = _PANEL_MESSAGE_ID.get(channel.id)
        if mid:
            try:
                await channel.fetch_message(mid)
                return
            except Exception:
                _PANEL_MESSAGE_ID.pop(channel.id, None)

        # Search recent pinned messages for our panel
        pins = await channel.pins()
        for p in pins:
            if p.author.bot and p.embeds:
                if p.embeds[0].title == WRITE_PANEL_TITLE:
                    _PANEL_MESSAGE_ID[channel.id] = p.id
                    return

        # Post a new panel
        embed = discord.Embed(title=WRITE_PANEL_TITLE, description=WRITE_PANEL_DESC, color=TRAVELERLOG_EMBED_COLOR)
        view = WritePanelView()
        msg = await channel.send(embed=embed, view=view)
        try:
            await msg.pin()
        except Exception:
            pass
        _PANEL_MESSAGE_ID[channel.id] = msg.id
        print(f"[travelerlogs] ✅ panel ensured in #{channel.name}")

    except discord.HTTPException as e:
        print(f"[travelerlogs] ensure panel error in #{channel.name}: {e}")


# =====================
# OPTIONAL COMMAND (FALLBACK)
# =====================
def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int):
    """
    Optional: /writelog fallback in test channel only.
    """
    @tree.command(
        name="writelog",
        description="Write a traveler log (test channel only)",
        guild=discord.Object(id=guild_id),
    )
    async def writelog(interaction: discord.Interaction):
        if interaction.channel_id != TEST_CHANNEL_ID:
            await interaction.response.send_message("❌ Use the Write Log button in the test channel.", ephemeral=True)
            return
        year, day = _get_current_year_day()
        await interaction.response.send_modal(WriteTravelerLogModal(year=year, day=day))


# =====================
# IMAGE COLLECTION (HOOK INTO on_message)
# =====================
async def handle_possible_image_upload(message: discord.Message):
    """
    Call this from main.py on_message.
    If the author is in an image session and posts attachments, attach them to the log embed.
    """
    if message.author.bot:
        return

    sess = _IMAGE_SESSIONS.get(message.author.id)
    if not sess:
        return

    # Must be same channel and within time
    if message.channel.id != sess["channel_id"]:
        return
    if time.time() > sess["until"]:
        _IMAGE_SESSIONS.pop(message.author.id, None)
        return

    # Collect image URLs
    urls: List[str] = []
    for a in message.attachments:
        # accept images only
        if a.content_type and a.content_type.startswith("image/"):
            urls.append(a.url)

    if not urls:
        return

    log_message_id = sess["log_message_id"]
    existing = _LOG_IMAGES.get(log_message_id, [])
    remaining = MAX_IMAGES_PER_LOG - len(existing)
    if remaining <= 0:
        await message.channel.send(f"❌ Max images reached for this log ({MAX_IMAGES_PER_LOG}).", delete_after=6)
        _IMAGE_SESSIONS.pop(message.author.id, None)
        return

    urls = urls[:remaining]
    existing.extend(urls)
    _LOG_IMAGES[log_message_id] = existing

    # Update the embed (first message only)
    try:
        channel = message.channel
        log_msg = await channel.fetch_message(log_message_id)
        if not log_msg.embeds:
            return

        # Rebuild embeds from current first embed and stored images
        emb0 = log_msg.embeds[0]
        parsed = _extract_year_day_from_embed(emb0) or _get_current_year_day()
        year, day = parsed

        title = "Log"
        body = ""
        if emb0.fields and len(emb0.fields) >= 2:
            title = emb0.fields[1].name or "Log"
            body = emb0.fields[1].value or ""

        author_id = _LOG_AUTHOR.get(log_message_id)
        author_name = message.author.display_name if author_id == message.author.id else "Unknown"

        rebuilt = _build_log_embeds(year, day, title, body, author_name, images=existing)

        view = LogActionsView(log_message_id=log_message_id)
        await log_msg.edit(embed=rebuilt[0], view=view)

    except Exception as e:
        print(f"[travelerlogs] attach images error: {e}")

    # Optionally delete the user's upload message to keep channels clean
    try:
        await message.delete()
    except Exception:
        pass

    # End session if max reached
    if len(_LOG_IMAGES[log_message_id]) >= MAX_IMAGES_PER_LOG:
        _IMAGE_SESSIONS.pop(message.author.id, None)
        await message.channel.send("✅ Images attached (max reached).", delete_after=6)
    else:
        sess["count"] += len(urls)
        _IMAGE_SESSIONS[message.author.id] = sess
        await message.channel.send(f"✅ Attached {len(urls)} image(s) to your log.", delete_after=6)