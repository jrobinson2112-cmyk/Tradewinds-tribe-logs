# travelerlogs_module.py
# Button-only Traveler Logs with:
# - Write Log button panel (pinned)
# - Edit Log button (only author)
# - Add Image button (only author) ✅ reliable: re-uploads image to the log message and uses attachment://
# - /postlogbutton admin command to manually post the panel
# - Startup cleanup: remove duplicate panels, ensure exactly 1 pinned panel
#
# Notes:
# - “Gallery style” multiple images in ONE embed isn’t possible (one embed image).
#   So MAX_IMAGES_PER_LOG = 1 to keep it reliable.
# - No “normal text” enforcement (you’ll do via Discord perms).
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
LOG_EMBED_COLOR = 0x2563EB     # blue
PANEL_EMBED_COLOR = 0xFFFFFF   # white
LOG_TITLE = "📜 Traveler Log"

# TEST ONLY channel (you asked to keep it only in test during tweaks)
TEST_ONLY_CHANNEL_ID = int(os.getenv("TRAVELERLOGS_TEST_CHANNEL_ID", "1462402354535075890"))

# Admin role allowed to use /postlogbutton
ADMIN_ROLE_ID = int(os.getenv("TRAVELERLOGS_ADMIN_ROLE_ID", "1439069787207766076"))

# Max images per log (reliable: 1)
MAX_IMAGES_PER_LOG = 1

# Persistent view IDs
WRITE_BUTTON_CUSTOM_ID = "travelerlogs:write"
EDIT_BUTTON_CUSTOM_ID = "travelerlogs:edit"
ADDIMG_BUTTON_CUSTOM_ID = "travelerlogs:addimg"

# Internal marker used to identify panel messages WITHOUT showing anything in embed footer
# (We store this marker in the *content* of the message, which users don’t really see because it’s blank-ish)
PANEL_MARKER_TEXT = "\u200b\u200bTRAVELERLOG_PANEL_V2\u200b\u200b"

# =====================
# IN-MEMORY STATE
# =====================
# log message id -> {"author_id": int, "image_filename": str|None}
_LOG_META: Dict[int, Dict[str, Any]] = {}

# user_id/channel_id -> last log message id (for add image flow)
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
    Embed description hard limit is 4096; keep margin for safety.
    If log is longer, we split into multiple continuation embeds.
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
    # Show Year/Day directly under the title (top)
    desc_top = f"**Year {year} • Day {day}**"
    if total_pages > 1:
        desc_top += f"\n*(Page {page}/{total_pages})*"

    embed = discord.Embed(
        title=LOG_TITLE,
        description=desc_top,
        color=LOG_EMBED_COLOR,
    )

    # Main log content as field(s)
    embed.add_field(name=title or "\u200b", value=body or "\u200b", inline=False)

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
    # IMPORTANT: no footer marker (so TRAVELERLOG_PANEL doesn’t show)
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
        # Discord modal limit: 4000
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
            default=(default_title or "")[:256],
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

    @discord.ui.button(label="Write Log", style=discord.ButtonStyle.primary, emoji="🖋️", custom_id=WRITE_BUTTON_CUSTOM_ID)
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

    @discord.ui.button(label="Edit Log", style=discord.ButtonStyle.secondary, emoji="✏️", custom_id=EDIT_BUTTON_CUSTOM_ID)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg = interaction.message
        meta = _LOG_META.get(msg.id)
        if not meta or meta.get("author_id") != interaction.user.id:
            await interaction.response.send_message("❌ Only the log author can edit this.", ephemeral=True)
            return

        year, day = 1, 1
        title, body = "Log", ""
        try:
            emb = msg.embeds[0]
            # Description contains "**Year X • Day Y**"
            desc = (emb.description or "").replace("*", "")
            # Year 2 • Day 341
            parts = desc.replace("•", "").split()
            if "Year" in parts and "Day" in parts:
                year = int(parts[parts.index("Year") + 1])
                day = int(parts[parts.index("Day") + 1])

            title = emb.fields[0].name
            body = emb.fields[0].value
        except Exception:
            pass

        modal = EditLogModal(default_year=year, default_day=day, default_title=title, default_body=body)
        await interaction.response.send_modal(modal)
        await modal.wait()
        if not modal.result:
            return

        image_filename = meta.get("image_filename")

        new_chunks = _chunk_text(modal.result["body"])
        new_body = new_chunks[0] if new_chunks else ""

        new_embed = _build_log_embed(
            year=modal.result["year"],
            day=modal.result["day"],
            title=modal.result["title"],
            body=new_body,
            author_name=_display_name(interaction.user),
            image_filename=image_filename,
            page=1,
            total_pages=max(1, len(new_chunks)),
        )

        try:
            await msg.edit(embed=new_embed, view=LogActionsView(author_id=interaction.user.id))
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

    @discord.ui.button(label="Add Image", style=discord.ButtonStyle.success, emoji="📸", custom_id=ADDIMG_BUTTON_CUSTOM_ID)
    async def add_img_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg = interaction.message
        meta = _LOG_META.get(msg.id)

        if not meta or meta.get("author_id") != interaction.user.id:
            await interaction.response.send_message("❌ Only the log author can add an image.", ephemeral=True)
            return

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

        attachment: Optional[discord.Attachment] = None
        for a in upload_msg.attachments:
            ctype = (a.content_type or "").lower()
            if ctype.startswith("image/"):
                attachment = a
                break

        if not attachment:
            await interaction.followup.send("❌ No image attachment found.", ephemeral=True)
            return

        try:
            file = await attachment.to_file()
        except Exception as e:
            await interaction.followup.send(f"❌ Could not read attachment: {e}", ephemeral=True)
            return

        image_filename = file.filename

        try:
            # Rebuild embed so it references attachment://filename
            emb = msg.embeds[0] if msg.embeds else None
            if emb is None:
                await interaction.followup.send("❌ Log embed missing.", ephemeral=True)
                return

            # Extract current year/day from description
            year, day = 1, 1
            title, body = "Log", ""
            try:
                desc = (emb.description or "").replace("*", "")
                parts = desc.replace("•", "").split()
                if "Year" in parts and "Day" in parts:
                    year = int(parts[parts.index("Year") + 1])
                    day = int(parts[parts.index("Day") + 1])
                title = emb.fields[0].name
                body = emb.fields[0].value
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

            await msg.edit(embed=new_embed, attachments=[file], view=LogActionsView(author_id=interaction.user.id))

            meta["image_filename"] = image_filename
            _LOG_META[msg.id] = meta

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
    client.add_view(WritePanelView())
    client.add_view(LogActionsView(author_id=0))  # dummy for custom_id registration after restart

# =====================
# PANEL MANAGEMENT / CLEANUP
# =====================
async def _find_all_panels(channel: discord.TextChannel, limit: int = 50) -> list[discord.Message]:
    """
    Find panel messages by checking:
    - pinned messages that have our marker content OR our panel embed title.
    - recent history for marker content (in case unpinned duplicates exist).
    """
    found: list[discord.Message] = []

    # Pins first
    try:
        pins = await channel.pins()
        for m in pins:
            if m.author.bot and (
                (m.content and "TRAVELERLOG_PANEL_V2" in m.content)
                or (m.embeds and m.embeds[0].title == "🖋️ Write a Traveler Log")
            ):
                found.append(m)
    except Exception:
        pass

    # Recent history (to delete duplicates)
    try:
        async for m in channel.history(limit=limit):
            if m.author.bot and m.content and "TRAVELERLOG_PANEL_V2" in m.content:
                if m not in found:
                    found.append(m)
    except Exception:
        pass

    return found

async def _post_and_pin_panel(channel: discord.TextChannel) -> Optional[discord.Message]:
    try:
        view = WritePanelView()
        emb = _build_panel_embed()
        # Marker in message content (not embed footer)
        msg = await channel.send(content=PANEL_MARKER_TEXT, embed=emb, view=view)
        try:
            await msg.pin()
        except Exception:
            pass
        return msg
    except Exception:
        return None

async def ensure_write_panels(client: discord.Client, guild_id: int):
    """
    Cleanup + ensure exactly one pinned panel in TEST channel.
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

    panels = await _find_all_panels(ch)

    # Keep 1 panel (prefer a pinned one)
    pinned = [m for m in panels if m.pinned]
    keep: Optional[discord.Message] = pinned[0] if pinned else (panels[0] if panels else None)

    # Delete the rest (duplicates)
    for m in panels:
        if keep and m.id == keep.id:
            continue
        try:
            await m.delete()
        except Exception:
            pass

    # If none, post new
    if keep is None:
        keep = await _post_and_pin_panel(ch)
    else:
        # Make sure it's pinned + has the correct embed/view
        try:
            if not keep.pinned:
                await keep.pin()
        except Exception:
            pass
        try:
            await keep.edit(content=PANEL_MARKER_TEXT, embed=_build_panel_embed(), view=WritePanelView())
        except Exception:
            pass

# =====================
# SLASH COMMANDS
# =====================
def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int):
    """
    /postlogbutton (admin): posts/reposts and pins panel in current channel
    /writelog: optional fallback that opens the same modal
    """
    guild_obj = discord.Object(id=guild_id)

    @tree.command(
        name="postlogbutton",
        description="(Admin) Post/repost the 'Write Log' panel in this channel and pin it",
        guild=guild_obj,
    )
    async def postlogbutton(interaction: discord.Interaction):
        ok = False
        try:
            if isinstance(interaction.user, discord.Member):
                ok = any(r.id == int(admin_role_id) for r in interaction.user.roles)
        except Exception:
            ok = False

        if not ok:
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("❌ This must be used in a text channel.", ephemeral=True)
            return

        panels = await _find_all_panels(ch)
        pinned = [m for m in panels if m.pinned]
        keep: Optional[discord.Message] = pinned[0] if pinned else (panels[0] if panels else None)

        # delete duplicates
        for m in panels:
            if keep and m.id == keep.id:
                continue
            try:
                await m.delete()
            except Exception:
                pass

        if keep is None:
            keep = await _post_and_pin_panel(ch)
            if keep:
                await interaction.response.send_message("✅ Posted/reposted and pinned.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Could not post/pin (missing perms?).", ephemeral=True)
            return

        # Refresh existing
        try:
            await keep.edit(content=PANEL_MARKER_TEXT, embed=_build_panel_embed(), view=WritePanelView())
            if not keep.pinned:
                try:
                    await keep.pin()
                except Exception:
                    pass
            await interaction.response.send_message("✅ Posted/reposted and pinned.", ephemeral=True)
        except Exception:
            await interaction.response.send_message("❌ Could not repost (missing perms?).", ephemeral=True)

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
# NO LOCK ENFORCEMENT
# =====================
async def enforce_travelerlog_lock(message: discord.Message):
    return