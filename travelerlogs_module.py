# travelerlogs_module.py
# Button-only Traveler Logs with:
# - Write Log button panel (pinned)
# - Edit Log button (only author)
# - Add Image button (only author)
#   ✅ Option 3: temporarily grants the user permission to send attachments/messages for 90s
#   ✅ reliable embed image: re-uploads image to the log message and uses attachment://
# - /postlogbutton admin command to manually post/repost the panel
#
# Notes:
# - Discord embeds can only display ONE image (embed.set_image). So we enforce MAX_IMAGES_PER_LOG = 1.
# - “Sticky to bottom” is not possible in Discord. Best alternative is: keep the panel pinned AND auto-repost if deleted.
# - The panel "marker text" is NOT shown in the embed (only used internally to find panel messages).
# - Time line ("🗓️ Solunaris Time") is removed; Year/Day now displayed at top under the title.
# - Panel embed is white, log embed is blue (as requested).

import os
import asyncio
import discord
from discord import app_commands
from typing import Optional, Dict, Any, Tuple

import time_module

# =====================
# CONFIG
# =====================
LOG_EMBED_COLOR = 0x2563EB   # blue
PANEL_EMBED_COLOR = 0xFFFFFF  # white

LOG_TITLE = "📜 Traveler Log"

# If set, ONLY this channel gets the panel (testing mode)
TEST_ONLY_CHANNEL_ID = int(os.getenv("TRAVELERLOGS_TEST_CHANNEL_ID", "1462402354535075890"))

# Excluded channels (won’t get panel even if you later add category scan back)
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

# Internal marker (NOT displayed) to identify the panel message safely across restarts
PANEL_MARKER = "TRAVELERLOG_PANEL_V3"

# Temp perms for uploading an image
TEMP_UPLOAD_SECONDS = 90

# =====================
# IN-MEMORY STATE
# =====================
# log message id -> {"author_id": int, "image_filename": str|None}
_LOG_META: Dict[int, Dict[str, Any]] = {}

# user_id + channel_id -> last log message id (so we know which log they meant)
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
    Discord embed field value hard limit is 1024, but we put log in a field value.
    However we’re currently using embed.add_field(name=title, value=body),
    and field value hard-limit is 1024.
    To allow “auto continuation”, we will put the body in the embed description instead.
    Discord embed description limit is 4096.
    We'll chunk to 3900 for safety.
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
        title=LOG_TITLE,
        color=LOG_EMBED_COLOR,
        description=f"**Year {year} • Day {day}**",
    )

    # Title as a field name, body as a field value is too limiting (1024).
    # Use fields for short things and description for big text.
    # We'll put title as a field and body in description continuation.
    embed.add_field(name=title, value="\u200b", inline=False)

    if total_pages > 1:
        embed.add_field(
            name=f"Log (Page {page}/{total_pages})",
            value=body or "\u200b",
            inline=False,
        )
    else:
        embed.add_field(
            name="Log",
            value=body or "\u200b",
            inline=False,
        )

    if image_filename:
        embed.add_field(name="📸 Image", value="Attached below.", inline=False)
        embed.set_image(url=f"attachment://{image_filename}")

    embed.set_footer(text=f"Logged by {author_name}")
    return embed

def _build_panel_embed() -> discord.Embed:
    e = discord.Embed(
        title="🖋️ Write a Traveler Log",
        description="Tap the button below to write a Traveler Log.\n\n**Tap the button • A form will open**",
        color=PANEL_EMBED_COLOR,
    )
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
# TEMP PERMS HELPERS (Option 3)
# =====================
async def _grant_temp_upload_perms(channel: discord.TextChannel, member: discord.Member, seconds: int = TEMP_UPLOAD_SECONDS) -> bool:
    """
    Grant temporary send + attach permissions to a member in a channel, then remove after N seconds.
    Requires the bot to have Manage Channels permission and role hierarchy above user.
    """
    try:
        current = channel.overwrites_for(member)
        # Save old values so we can restore accurately
        old_send = current.send_messages
        old_attach = current.attach_files
        old_embed = current.embed_links

        current.send_messages = True
        current.attach_files = True
        current.embed_links = True

        await channel.set_permissions(member, overwrite=current, reason="Temp upload perms for traveler log image")

        async def _revoke_later():
            await asyncio.sleep(seconds)
            try:
                ow = channel.overwrites_for(member)
                ow.send_messages = old_send
                ow.attach_files = old_attach
                ow.embed_links = old_embed
                await channel.set_permissions(member, overwrite=ow, reason="Revoke temp upload perms")
            except Exception:
                pass

        asyncio.create_task(_revoke_later())
        return True
    except Exception:
        return False

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

        first_msg: Optional[discord.Message] = None

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

            if first_msg is None:
                first_msg = msg
                _LOG_META[msg.id] = {"author_id": interaction.user.id, "image_filename": None}

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
        msg = interaction.message
        meta = _LOG_META.get(msg.id)
        if not meta or meta.get("author_id") != interaction.user.id:
            await interaction.response.send_message("❌ Only the log author can edit this.", ephemeral=True)
            return

        # Extract existing from embed
        year = 1
        day = 1
        title = "Log"
        body = ""

        try:
            emb = msg.embeds[0]
            # description is "**Year X • Day Y**"
            desc = (emb.description or "").replace("*", "")
            parts = desc.replace("•", "").split()
            if "Year" in parts and "Day" in parts:
                year = int(parts[parts.index("Year") + 1])
                day = int(parts[parts.index("Day") + 1])

            title = emb.fields[0].name
            # body stored in "Log" field
            for f in emb.fields:
                if f.name.startswith("Log"):
                    body = f.value
                    break
        except Exception:
            pass

        modal = EditLogModal(default_year=year, default_day=day, default_title=title, default_body=body)
        await interaction.response.send_modal(modal)
        await modal.wait()

        if not modal.result:
            return

        image_filename = meta.get("image_filename") if meta else None
        new_chunks = _chunk_text(modal.result["body"])

        # edit only first message; additional pages are posted anew
        emb = _build_log_embed(
            year=modal.result["year"],
            day=modal.result["day"],
            title=modal.result["title"],
            body=new_chunks[0] if new_chunks else "",
            author_name=_display_name(interaction.user),
            image_filename=image_filename,
            page=1,
            total_pages=max(1, len(new_chunks)),
        )

        try:
            await msg.edit(embed=emb, view=LogActionsView(author_id=interaction.user.id))
        except Exception as e:
            await interaction.followup.send(f"❌ Edit failed: {e}", ephemeral=True)
            return

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

        if not meta or meta.get("author_id") != interaction.user.id:
            await interaction.response.send_message("❌ Only the log author can add an image.", ephemeral=True)
            return

        if meta.get("image_filename"):
            await interaction.response.send_message("❌ This log already has an image.", ephemeral=True)
            return

        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("❌ This must be used in a text channel.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ Could not resolve your server membership.", ephemeral=True)
            return

        ok = await _grant_temp_upload_perms(ch, interaction.user, seconds=TEMP_UPLOAD_SECONDS)
        if not ok:
            await interaction.response.send_message(
                "❌ I couldn’t grant temporary upload permissions.\n"
                "Make sure I have **Manage Channels** and my role is above the member.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"📸 Upload **1 image** in this channel now.\n"
            f"You have **{TEMP_UPLOAD_SECONDS} seconds**.\n"
            "I’ll attach it to your log and delete your upload message.",
            ephemeral=True,
        )

        def check(m: discord.Message) -> bool:
            if m.author.id != interaction.user.id:
                return False
            if m.channel.id != interaction.channel_id:
                return False
            if not m.attachments:
                return False
            return any((a.content_type or "").startswith("image/") for a in m.attachments)

        try:
            upload_msg: discord.Message = await interaction.client.wait_for("message", timeout=float(TEMP_UPLOAD_SECONDS), check=check)
        except asyncio.TimeoutError:
            await interaction.followup.send("⌛ Timed out waiting for an image.", ephemeral=True)
            return

        attachment = next((a for a in upload_msg.attachments if (a.content_type or "").startswith("image/")), None)
        if not attachment:
            await interaction.followup.send("❌ No image attachment found.", ephemeral=True)
            return

        try:
            file = await attachment.to_file()
        except Exception as e:
            await interaction.followup.send(f"❌ Could not read attachment: {e}", ephemeral=True)
            return

        image_filename = file.filename

        # Extract current fields from embed to rebuild cleanly
        year = 1
        day = 1
        title = "Log"
        body = ""
        try:
            emb0 = msg.embeds[0]
            desc = (emb0.description or "").replace("*", "")
            parts = desc.replace("•", "").split()
            if "Year" in parts and "Day" in parts:
                year = int(parts[parts.index("Year") + 1])
                day = int(parts[parts.index("Day") + 1])
            title = emb0.fields[0].name
            for f in emb0.fields:
                if f.name.startswith("Log"):
                    body = f.value
                    break
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

        try:
            await msg.edit(embed=new_embed, attachments=[file], view=LogActionsView(author_id=interaction.user.id))
            meta["image_filename"] = image_filename
            _LOG_META[msg.id] = meta
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to attach image: {e}", ephemeral=True)
            return

        try:
            await upload_msg.delete()
        except Exception:
            pass

        await interaction.followup.send("✅ Image attached.", ephemeral=True)

# =====================
# PUBLIC: REGISTER VIEWS (persistent buttons)
# =====================
def register_views(client: discord.Client):
    """
    Call this in on_ready to register persistent views so buttons still work after redeploy.
    """
    client.add_view(WritePanelView())
    # dummy so Discord knows these custom_ids after restart
    client.add_view(LogActionsView(author_id=0))

# =====================
# PANEL MANAGEMENT
# =====================
async def _find_existing_panel(channel: discord.TextChannel) -> Optional[discord.Message]:
    """
    Finds the pinned panel message by:
      - pinned messages
      - bot author
      - has our WritePanelView custom_id AND a marker in message content
    We keep the marker ONLY in message.content (not visible in embed).
    """
    try:
        pins = await channel.pins()
        for m in pins:
            if not m.author.bot:
                continue
            if m.content != PANEL_MARKER:
                continue
            # must have a component/button with our custom_id
            if m.components:
                return m
    except Exception:
        pass
    return None

async def _post_and_pin_panel(channel: discord.TextChannel) -> Optional[discord.Message]:
    try:
        view = WritePanelView()
        emb = _build_panel_embed()
        # Marker stored in message content (not in embed)
        msg = await channel.send(content=PANEL_MARKER, embed=emb, view=view)
        try:
            await msg.pin()
        except Exception:
            pass
        return msg
    except Exception:
        return None

async def _cleanup_old_panels(channel: discord.TextChannel):
    """
    On startup: remove any pinned panels that match our marker but are old/duplicate.
    Keep only the newest one.
    """
    try:
        pins = await channel.pins()
        panels = [m for m in pins if m.author.bot and m.content == PANEL_MARKER]
        if len(panels) <= 1:
            return
        # keep newest by created_at
        panels.sort(key=lambda m: m.created_at, reverse=True)
        keep = panels[0]
        for m in panels[1:]:
            try:
                await m.unpin()
            except Exception:
                pass
            try:
                await m.delete()
            except Exception:
                pass
        # ensure the kept one is still pinned
        try:
            await keep.pin()
        except Exception:
            pass
    except Exception:
        pass

async def ensure_write_panels(client: discord.Client, guild_id: int):
    """
    Ensures the write panel exists (and pinned) in the TEST channel only.
    Also cleans up old/duplicate panels on startup.
    """
    await client.wait_until_ready()
    guild = client.get_guild(guild_id)
    if guild is None:
        return

    ch = guild.get_channel(TEST_ONLY_CHANNEL_ID)
    if not isinstance(ch, discord.TextChannel):
        try:
            ch = await guild.fetch_channel(TEST_ONLY_CHANNEL_ID)
        except Exception:
            return
    if not isinstance(ch, discord.TextChannel):
        return

    await _cleanup_old_panels(ch)

    existing = await _find_existing_panel(ch)
    if existing is None:
        await _post_and_pin_panel(ch)

# =====================
# SLASH COMMANDS
# =====================
def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int):
    """
    - /postlogbutton : admin only, posts/reposts the panel and pins it
    - /writelog      : opens the same modal as the button (handy fallback)
    """
    global ADMIN_ROLE_ID
    ADMIN_ROLE_ID = int(admin_role_id)

    guild_obj = discord.Object(id=guild_id)

    def _is_admin(member: discord.Member) -> bool:
        return any(r.id == ADMIN_ROLE_ID for r in getattr(member, "roles", []))

    @tree.command(
        name="postlogbutton",
        description="(Admin) Post/repost the 'Write Log' panel in this channel and pin it",
        guild=guild_obj,
    )
    async def postlogbutton(interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not _is_admin(interaction.user):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("❌ Use this in a text channel.", ephemeral=True)
            return

        # cleanup duplicates, then ensure exists
        await _cleanup_old_panels(ch)
        existing = await _find_existing_panel(ch)
        if existing is None:
            msg = await _post_and_pin_panel(ch)
            if msg:
                await interaction.response.send_message("✅ Posted/reposted and pinned.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Could not post/pin (missing perms?).", ephemeral=True)
            return

        # Re-pin if needed
        try:
            await existing.pin()
        except Exception:
            pass

        await interaction.response.send_message("✅ Panel already exists and is pinned.", ephemeral=True)

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
# (No lock enforcement - handled via Discord perms)
# =====================
async def enforce_travelerlog_lock(message: discord.Message):
    return