# travelerlogs_module.py
# Button-only Traveler Logs with:
# - Write Log button panel (pinned)
# - Edit Log button (only author)
# - Add Image button (only author) ✅ reliable: re-uploads image to the log message and uses attachment://
# - /postlogbutton admin command to manually post the panel
#
# Notes:
# - “Gallery style” multiple images in ONE embed isn’t really possible on Discord (one embed image).
#   So this version enforces MAX_IMAGES_PER_LOG = 1 to keep it reliable.
# - No “normal text” enforcement here (you said you’ll do via Discord perms).
# - Year/Day defaults from time_module.get_time_state() but user can change in modal.

import os
import asyncio
import discord
from discord import app_commands
from typing import Optional, Dict, Any, Tuple

import time_module

# =====================
# CONFIG
# =====================
TRAVELERLOG_EMBED_COLOR = 0x8B5CF6  # purple
TRAVELERLOG_TITLE = "📖 Traveler Log"

# Testing: if set, ONLY this channel gets the panel; else uses category mode.
TEST_ONLY_CHANNEL_ID = int(os.getenv("TRAVELERLOGS_TEST_CHANNEL_ID", "1462402354535075890"))

# Old excluded channels (won’t get panel even if you scan a category)
EXCLUDED_CHANNEL_IDS = {
    1462539723112321218,
    1437457789164191939,
    1455315150859927663,
    1456386974167466106,
}

# Admin role allowed to use /postlogbutton
ADMIN_ROLE_ID = int(os.getenv("TRAVELERLOGS_ADMIN_ROLE_ID", "1439069787207766076"))

# Max images per log (reliable: 1)
MAX_IMAGES_PER_LOG = 1

# Used to uniquely mark the pinned “Write Log” panel message
PANEL_MARKER = "TRAVELERLOG_WRITE_PANEL_V1"

# =====================
# IN-MEMORY STATE
# =====================
# log message id -> {"author_id": int, "image_filename": str|None}
_LOG_META: Dict[int, Dict[str, Any]] = {}

# user_id -> last log message id in that channel (for “Add Images” flow)
_LAST_LOG_BY_USER_CHANNEL: Dict[Tuple[int, int], int] = {}

# =====================
# TIME HELPERS
# =====================
def _get_current_day_year() -> Tuple[int, int]:
    """
    Pull current Year + Day from time_module.
    Falls back to 1,1 if time isn't initialised yet.
    """
    try:
        state = time_module.get_time_state()
        year = int(state.get("year", 1))
        day = int(state.get("day", 1))
        return year, day
    except Exception:
        return 1, 1

# =====================
# TEXT HELPERS
# =====================
def _chunk_text(text: str, limit: int = 3900) -> list[str]:
    """
    Discord embed description hard limit is 4096; keep margin for safety.
    If log is longer, we split into multiple embeds.
    """
    text = text or ""
    if len(text) <= limit:
        return [text]

    chunks = []
    cur = ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > limit:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur:
        chunks.append(cur)
    return chunks

def _display_name(user: discord.abc.User) -> str:
    # Prefer server nickname/display_name
    try:
        return user.display_name
    except Exception:
        return str(user)

# =====================
# EMBED BUILDERS
# =====================
def _build_log_embed(
    *,
    year: int,
    day: int,
    title: str,
    body: str,
    author_name: str,
    image_filename: Optional[str] = None,
    page: int = 1,
    total_pages: int = 1,
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

    if total_pages > 1:
        embed.add_field(name=title, value=f"{body}\n\n*(Page {page}/{total_pages})*", inline=False)
    else:
        embed.add_field(name=title, value=body or "\u200b", inline=False)

    if image_filename:
        embed.add_field(
            name="📸 Image",
            value="Attached below.",
            inline=False,
        )
        # CRITICAL: reference attachment on THIS message
        embed.set_image(url=f"attachment://{image_filename}")

    embed.set_footer(text=f"Logged by {author_name}")
    return embed

def _build_panel_embed() -> discord.Embed:
    e = discord.Embed(
        title="🖋️ Write a Traveler Log",
        description="Tap the button below to write a Traveler Log.\n\n**Tap the button • A form will open**",
        color=TRAVELERLOG_EMBED_COLOR,
    )
    e.set_footer(text=PANEL_MARKER)
    return e

# =====================
# UI: MODALS
# =====================
class WriteLogModal(discord.ui.Modal, title="Write a Traveler Log"):
    def __init__(self, default_year: int, default_day: int):
        super().__init__(timeout=300)

        self.year = discord.ui.TextInput(
            label="Year (number)",
            required=True,
            default=str(default_year),
            max_length=6,
        )
        self.day = discord.ui.TextInput(
            label="Day (number)",
            required=True,
            default=str(default_day),
            max_length=6,
        )
        self.log_title = discord.ui.TextInput(
            label="Title",
            required=True,
            placeholder="Short title for your log entry",
            max_length=256,
        )
        # Discord modal text input max for “paragraph” is 4000.
        self.log_body = discord.ui.TextInput(
            label="Log",
            required=True,
            style=discord.TextStyle.paragraph,
            placeholder="Write your traveler log...",
            max_length=4000,
        )

        self.add_item(self.year)
        self.add_item(self.day)
        self.add_item(self.log_title)
        self.add_item(self.log_body)

        self.result: Optional[Dict[str, Any]] = None

    async def on_submit(self, interaction: discord.Interaction):
        try:
            y = int(str(self.year.value).strip())
        except Exception:
            y = 1
        try:
            d = int(str(self.day.value).strip())
        except Exception:
            d = 1

        self.result = {
            "year": max(1, y),
            "day": max(1, d),
            "title": str(self.log_title.value).strip()[:256],
            "body": str(self.log_body.value).rstrip(),
        }
        await interaction.response.defer(ephemeral=True)

class EditLogModal(discord.ui.Modal, title="Edit Traveler Log"):
    def __init__(self, *, default_year: int, default_day: int, default_title: str, default_body: str):
        super().__init__(timeout=300)

        self.year = discord.ui.TextInput(
            label="Year (number)",
            required=True,
            default=str(default_year),
            max_length=6,
        )
        self.day = discord.ui.TextInput(
            label="Day (number)",
            required=True,
            default=str(default_day),
            max_length=6,
        )
        self.log_title = discord.ui.TextInput(
            label="Title",
            required=True,
            default=default_title[:256] if default_title else "",
            max_length=256,
        )
        self.log_body = discord.ui.TextInput(
            label="Log",
            required=True,
            style=discord.TextStyle.paragraph,
            default=(default_body or "")[:4000],
            max_length=4000,
        )

        self.add_item(self.year)
        self.add_item(self.day)
        self.add_item(self.log_title)
        self.add_item(self.log_body)

        self.result: Optional[Dict[str, Any]] = None

    async def on_submit(self, interaction: discord.Interaction):
        try:
            y = int(str(self.year.value).strip())
        except Exception:
            y = 1
        try:
            d = int(str(self.day.value).strip())
        except Exception:
            d = 1

        self.result = {
            "year": max(1, y),
            "day": max(1, d),
            "title": str(self.log_title.value).strip()[:256],
            "body": str(self.log_body.value).rstrip(),
        }
        await interaction.response.defer(ephemeral=True)

# =====================
# UI: VIEWS / BUTTONS (PERSISTENT)
# =====================
class WritePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Write Log", style=discord.ButtonStyle.primary, emoji="🖋️", custom_id="travelerlogs:write")
    async def write_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        year, day = _get_current_day_year()
        modal = WriteLogModal(default_year=year, default_day=day)
        await interaction.response.send_modal(modal)
        await modal.wait()

        if not modal.result:
            return

        author_name = _display_name(interaction.user)
        chunks = _chunk_text(modal.result["body"])

        # If multiple chunks, we post multiple embeds (auto continuation)
        first_msg: Optional[discord.Message] = None
        last_msg: Optional[discord.Message] = None

        for i, chunk in enumerate(chunks, start=1):
            emb = _build_log_embed(
                year=modal.result["year"],
                day=modal.result["day"],
                title=modal.result["title"],
                body=chunk,
                author_name=author_name,
                image_filename=None,
                page=i,
                total_pages=len(chunks),
            )

            view = LogActionsView(author_id=interaction.user.id)
            msg = await interaction.channel.send(embed=emb, view=view)

            # Track meta only for the FIRST message (the one we attach images to)
            if first_msg is None:
                first_msg = msg
                _LOG_META[msg.id] = {"author_id": interaction.user.id, "image_filename": None}
            last_msg = msg

        # Remember last log in this channel for that user
        if first_msg:
            _LAST_LOG_BY_USER_CHANNEL[(interaction.user.id, interaction.channel_id)] = first_msg.id

        try:
            await interaction.followup.send("✅ Traveler log recorded.", ephemeral=True)
        except Exception:
            pass


class LogActionsView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=None)
        self.author_id = author_id

    @discord.ui.button(label="Edit Log", style=discord.ButtonStyle.secondary, emoji="✏️", custom_id="travelerlogs:edit")
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Only author can edit
        msg = interaction.message
        meta = _LOG_META.get(msg.id)
        if not meta or meta.get("author_id") != interaction.user.id:
            await interaction.response.send_message("❌ Only the log author can edit this.", ephemeral=True)
            return

        # Extract existing fields
        year = 1
        day = 1
        title = "Log"
        body = ""

        try:
            # Field 0 is "Solunaris Time"
            time_val = msg.embeds[0].fields[0].value  # "**Year X • Day Y**"
            # crude parse:
            cleaned = time_val.replace("*", "")
            # "Year 2 • Day 336"
            parts = cleaned.replace("•", "").split()
            # ["Year","2","Day","336"]
            if "Year" in parts and "Day" in parts:
                year = int(parts[parts.index("Year") + 1])
                day = int(parts[parts.index("Day") + 1])
            # Field 1 is title/body
            title = msg.embeds[0].fields[1].name
            body = msg.embeds[0].fields[1].value
            body = body.replace("\n\n*(Page 1/1)*", "")
        except Exception:
            pass

        modal = EditLogModal(default_year=year, default_day=day, default_title=title, default_body=body)
        await interaction.response.send_modal(modal)
        await modal.wait()

        if not modal.result:
            return

        # Preserve image if any
        image_filename = meta.get("image_filename") if meta else None

        # Rebuild embed for page 1 only (we only edit the first message)
        new_chunks = _chunk_text(modal.result["body"])
        new_body = new_chunks[0] if new_chunks else ""
        emb = _build_log_embed(
            year=modal.result["year"],
            day=modal.result["day"],
            title=modal.result["title"],
            body=new_body,
            author_name=_display_name(interaction.user),
            image_filename=image_filename,
            page=1,
            total_pages=max(1, len(new_chunks)),
        )

        # If there is an image, we must keep the attachment on the message.
        # We cannot “re-attach” without re-uploading; so we leave existing attachments intact.
        # (Discord keeps attachments unless you remove them.)
        try:
            await msg.edit(embed=emb, view=LogActionsView(author_id=interaction.user.id))
        except Exception as e:
            await interaction.followup.send(f"❌ Edit failed: {e}", ephemeral=True)
            return

        # If more pages now exist, post continuation messages after edit
        if len(new_chunks) > 1:
            for i, chunk in enumerate(new_chunks[1:], start=2):
                cont = _build_log_embed(
                    year=modal.result["year"],
                    day=modal.result["day"],
                    title=modal.result["title"],
                    body=chunk,
                    author_name=_display_name(interaction.user),
                    image_filename=None,
                    page=i,
                    total_pages=len(new_chunks),
                )
                await interaction.channel.send(embed=cont)

        await interaction.followup.send("✅ Updated.", ephemeral=True)

    @discord.ui.button(label="Add Image", style=discord.ButtonStyle.success, emoji="📸", custom_id="travelerlogs:addimg")
    async def add_img_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg = interaction.message
        meta = _LOG_META.get(msg.id)

        # Only author can add image
        if not meta or meta.get("author_id") != interaction.user.id:
            await interaction.response.send_message("❌ Only the log author can add an image.", ephemeral=True)
            return

        # Enforce max images = 1
        if meta.get("image_filename"):
            await interaction.response.send_message("❌ This log already has an image.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"📸 Send up to {MAX_IMAGES_PER_LOG} image(s) in this channel now.\n"
            "I’ll attach it to your log and delete your upload message.\n\n"
            "Timeout: 180s",
            ephemeral=True,
        )

        def check(m: discord.Message) -> bool:
            if m.author.id != interaction.user.id:
                return False
            if m.channel.id != interaction.channel_id:
                return False
            if not m.attachments:
                return False
            # only allow common image attachments
            for a in m.attachments:
                ctype = (a.content_type or "").lower()
                if ctype.startswith("image/"):
                    return True
            return False

        try:
            upload_msg: discord.Message = await interaction.client.wait_for("message", timeout=180.0, check=check)
        except asyncio.TimeoutError:
            await interaction.followup.send("⌛ Timed out waiting for an image.", ephemeral=True)
            return

        # Pick the first image attachment
        attachment: Optional[discord.Attachment] = None
        for a in upload_msg.attachments:
            ctype = (a.content_type or "").lower()
            if ctype.startswith("image/"):
                attachment = a
                break

        if not attachment:
            await interaction.followup.send("❌ No image attachment found.", ephemeral=True)
            return

        # ✅ CRITICAL FIX:
        # Re-upload the image to the LOG MESSAGE and set embed image to attachment://filename
        # (Do NOT reference attachment.url after deleting upload message.)
        try:
            file = await attachment.to_file()
        except Exception as e:
            await interaction.followup.send(f"❌ Could not read attachment: {e}", ephemeral=True)
            return

        # Update embed to point to attachment://
        image_filename = file.filename

        try:
            emb = msg.embeds[0] if msg.embeds else None
            if emb is None:
                await interaction.followup.send("❌ Log embed missing.", ephemeral=True)
                return

            # Rebuild properly to ensure attachment:// gets used
            # Extract current time/title/body best-effort:
            year = 1
            day = 1
            title = "Log"
            body = ""
            try:
                time_val = emb.fields[0].value.replace("*", "")
                parts = time_val.replace("•", "").split()
                if "Year" in parts and "Day" in parts:
                    year = int(parts[parts.index("Year") + 1])
                    day = int(parts[parts.index("Day") + 1])
                title = emb.fields[1].name
                body = emb.fields[1].value
                body = body.replace("\n\n*(Page 1/1)*", "")
            except Exception:
                pass

            new_embed = _build_log_embed(
                year=year,
                day=day,
                title=title,
                body=body,
                author_name=_display_name(interaction.user),
                image_filename=image_filename,
                page=1,
                total_pages=1,
            )

            # Edit message: attach file and set embed to reference attachment://filename
            await msg.edit(embed=new_embed, attachments=[file], view=LogActionsView(author_id=interaction.user.id))

            # Save meta
            meta["image_filename"] = image_filename
            _LOG_META[msg.id] = meta

            # Delete user upload message to keep channel clean
            try:
                await upload_msg.delete()
            except Exception:
                pass

            await interaction.followup.send("✅ Image attached to your log.", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Failed to attach image: {e}", ephemeral=True)

# =====================
# PUBLIC: REGISTER VIEWS (persistent buttons)
# =====================
def register_views(client: discord.Client):
    """
    Call this in on_ready: client.add_view(WritePanelView()); client.add_view(LogActionsView(...)) not needed
    because LogActionsView is attached per-message, but custom_id must be known.
    """
    client.add_view(WritePanelView())
    # Also register a dummy LogActionsView so Discord knows the custom_ids after restart
    client.add_view(LogActionsView(author_id=0))

# =====================
# PANEL MANAGEMENT
# =====================
async def _find_existing_panel(channel: discord.TextChannel) -> Optional[discord.Message]:
    try:
        pins = await channel.pins()
        for m in pins:
            if m.author.bot and m.embeds:
                emb = m.embeds[0]
                if emb.footer and emb.footer.text == PANEL_MARKER:
                    return m
    except Exception:
        pass
    return None

async def _post_and_pin_panel(channel: discord.TextChannel) -> Optional[discord.Message]:
    try:
        view = WritePanelView()
        emb = _build_panel_embed()
        msg = await channel.send(embed=emb, view=view)
        try:
            await msg.pin()
        except Exception:
            pass
        return msg
    except Exception:
        return None

async def ensure_write_panels(client: discord.Client, guild_id: int):
    """
    Ensures the write panel exists (and pinned) in the TEST channel only.
    (You asked to remove category-wide while testing.)
    """
    await client.wait_until_ready()
    guild = client.get_guild(guild_id)
    if guild is None:
        return

    # Only test channel
    ch = guild.get_channel(TEST_ONLY_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        try:
            ch = await guild.fetch_channel(TEST_ONLY_CHANNEL_ID)
        except Exception:
            return
    if not isinstance(ch, discord.TextChannel):
        return

    existing = await _find_existing_panel(ch)
    if existing is None:
        await _post_and_pin_panel(ch)

# =====================
# SLASH COMMAND: /postlogbutton
# =====================
def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int):
    """
    Optional fallback: /writelog (same as pressing button) + /postlogbutton (admin)
    """
    guild_obj = discord.Object(id=guild_id)

    @tree.command(
        name="postlogbutton",
        description="(Admin) Post the 'Write Log' panel in this channel and pin it",
        guild=guild_obj,
    )
    async def postlogbutton(interaction: discord.Interaction):
        # Check admin role
        ok = False
        try:
            if isinstance(interaction.user, discord.Member):
                ok = any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles)
        except Exception:
            ok = False

        if not ok:
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("❌ This must be used in a text channel.", ephemeral=True)
            return

        existing = await _find_existing_panel(ch)
        if existing:
            await interaction.response.send_message("ℹ️ Panel already exists here (or I couldn’t post it).", ephemeral=True)
            return

        msg = await _post_and_pin_panel(ch)
        if msg:
            await interaction.response.send_message("✅ Posted and pinned.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Could not post/pin (missing perms?).", ephemeral=True)

    @tree.command(
        name="writelog",
        description="Write a traveler log (opens a form)",
        guild=guild_obj,
    )
    async def writelog(interaction: discord.Interaction):
        year, day = _get_current_day_year()
        modal = WriteLogModal(default_year=year, default_day=day)
        await interaction.response.send_modal(modal)

# =====================
# NO LOCK ENFORCEMENT (per your request)
# =====================
async def enforce_travelerlog_lock(message: discord.Message):
    return