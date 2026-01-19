# travelerlogs_module.py
# Button-only Traveler Logs with:
# - Write Log panel (pinned + re-posted to keep near bottom)
# - Edit Log (author-only)
# - Add Image (author-only, reliable attachment://)
# - /postlogbutton (admin role)
# - Startup cleanup (removes old panels & reposts)
#
# Marker NOTE:
# - User asked to remove visible PANEL_MARKER text.
# - We now identify the panel via a *hidden* message content marker (zero-width chars).
#   It is not visible in Discord UI.

import os
import asyncio
import discord
from discord import app_commands
from typing import Optional, Dict, Any, Tuple

import time_module

# =====================
# CONFIG
# =====================
PANEL_EMBED_COLOR = 0xFFFFFF  # "white" accent strip
LOG_EMBED_COLOR = 0x2563EB    # blue

TRAVELERLOG_TITLE = "📖 Traveler Log"

# Only this channel gets the panel while testing
TEST_ONLY_CHANNEL_ID = int(os.getenv("TRAVELERLOGS_TEST_CHANNEL_ID", "1462402354535075890"))

# Admin role allowed to use /postlogbutton
ADMIN_ROLE_ID = int(os.getenv("TRAVELERLOGS_ADMIN_ROLE_ID", "1439069787207766076"))

# Max images per log (reliable: 1)
MAX_IMAGES_PER_LOG = 1

# Hidden (non-visible) content marker to identify the panel message
# This renders as nothing, but is detectable in code.
PANEL_HIDDEN_MARKER = "\u200b\u200b\u200bTRAVELERLOG_PANEL\u200b\u200b\u200b"

# =====================
# IN-MEMORY STATE
# =====================
# log message id -> {"author_id": int, "image_filename": str|None}
_LOG_META: Dict[int, Dict[str, Any]] = {}

# user_id + channel_id -> last log message id
_LAST_LOG_BY_USER_CHANNEL: Dict[Tuple[int, int], int] = {}

# =====================
# TIME HELPERS
# =====================
def _get_current_day_year() -> Tuple[int, int]:
    try:
        state = time_module.get_time_state()
        return int(state.get("year", 1)), int(state.get("day", 1))
    except Exception:
        return 1, 1

# =====================
# TEXT HELPERS
# =====================
def _chunk_text(text: str, limit: int = 3900) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
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
    emb = discord.Embed(title=TRAVELERLOG_TITLE, color=LOG_EMBED_COLOR)

    if total_pages > 1:
        emb.add_field(name=title, value=f"{body}\n\n*(Page {page}/{total_pages})*", inline=False)
    else:
        emb.add_field(name=title, value=body or "\u200b", inline=False)

    if image_filename:
        emb.add_field(name="📸 Image", value="Attached below.", inline=False)
        emb.set_image(url=f"attachment://{image_filename}")

    # ✅ “Solunaris Time” field removed — shown in footer instead
    emb.set_footer(text=f"Year {year} • Day {day} • Logged by {author_name}")
    return emb

def _build_panel_embed() -> discord.Embed:
    return discord.Embed(
        title="🖋️ Write a Traveler Log",
        description="Tap the button below to write a Traveler Log.\n\n**Tap the button • A form will open**",
        color=PANEL_EMBED_COLOR,
    )

# =====================
# PARSERS (from embed)
# =====================
def _parse_year_day_from_footer(emb: discord.Embed) -> Tuple[int, int]:
    year, day = 1, 1
    try:
        ft = emb.footer.text or ""
        parts = ft.replace("•", "").split()
        if "Year" in parts and "Day" in parts:
            year = int(parts[parts.index("Year") + 1])
            day = int(parts[parts.index("Day") + 1])
    except Exception:
        pass
    return year, day

def _parse_title_body(emb: discord.Embed) -> Tuple[str, str]:
    title = "Log"
    body = ""
    try:
        if emb.fields:
            title = emb.fields[0].name or "Log"
            body = emb.fields[0].value or ""
            body = body.replace("\n\n*(Page 1/1)*", "")
    except Exception:
        pass
    return title, body

# =====================
# UI: MODALS
# =====================
class WriteLogModal(discord.ui.Modal, title="Write a Traveler Log"):
    def __init__(self, default_year: int, default_day: int):
        super().__init__(timeout=300)

        self.year = discord.ui.TextInput(label="Year (number)", required=True, default=str(default_year), max_length=6)
        self.day = discord.ui.TextInput(label="Day (number)", required=True, default=str(default_day), max_length=6)
        self.log_title = discord.ui.TextInput(label="Title", required=True, placeholder="Short title for your log entry", max_length=256)
        self.log_body = discord.ui.TextInput(
            label="Log",
            required=True,
            style=discord.TextStyle.paragraph,
            placeholder="Write your traveler log...",
            max_length=4000,  # Discord hard limit for modal paragraph input
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

        self.year = discord.ui.TextInput(label="Year (number)", required=True, default=str(default_year), max_length=6)
        self.day = discord.ui.TextInput(label="Day (number)", required=True, default=str(default_day), max_length=6)
        self.log_title = discord.ui.TextInput(label="Title", required=True, default=(default_title or "")[:256], max_length=256)
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
# VIEWS (PERSISTENT)
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

        # Repost panel so it stays at bottom
        try:
            if isinstance(interaction.channel, discord.TextChannel):
                await upsert_write_panel(interaction.channel)
        except Exception:
            pass

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

        emb = msg.embeds[0] if msg.embeds else None
        if not emb:
            await interaction.response.send_message("❌ Log embed missing.", ephemeral=True)
            return

        year, day = _parse_year_day_from_footer(emb)
        title, body = _parse_title_body(emb)

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

        # Continuation pages (new messages)
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

        # Repost panel to keep it at bottom
        try:
            if isinstance(interaction.channel, discord.TextChannel):
                await upsert_write_panel(interaction.channel)
        except Exception:
            pass

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

        await interaction.response.send_message(
            f"📸 Send up to {MAX_IMAGES_PER_LOG} image in this channel now.\n"
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

        emb = msg.embeds[0] if msg.embeds else None
        if not emb:
            await interaction.followup.send("❌ Log embed missing.", ephemeral=True)
            return

        year, day = _parse_year_day_from_footer(emb)
        title, body = _parse_title_body(emb)

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
            # Attach image to the log message and reference attachment://filename
            await msg.edit(embed=new_embed, attachments=[file], view=LogActionsView(author_id=interaction.user.id))

            meta["image_filename"] = image_filename
            _LOG_META[msg.id] = meta

            try:
                await upload_msg.delete()
            except Exception:
                pass

            # Repost panel to keep it at bottom
            try:
                if isinstance(interaction.channel, discord.TextChannel):
                    await upsert_write_panel(interaction.channel)
            except Exception:
                pass

            await interaction.followup.send("✅ Image attached to your log.", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Failed to attach image: {e}", ephemeral=True)

# =====================
# PANEL FIND/DELETE/UPSERT
# =====================
async def _is_panel_message(msg: discord.Message) -> bool:
    try:
        if not msg.author.bot:
            return False
        if (msg.content or "") != PANEL_HIDDEN_MARKER:
            return False
        if not msg.embeds:
            return False
        return True
    except Exception:
        return False

async def _delete_existing_panels(channel: discord.TextChannel):
    # delete pinned panels first
    try:
        pins = await channel.pins()
        for m in pins:
            if await _is_panel_message(m):
                try:
                    await m.delete()
                except Exception:
                    pass
    except Exception:
        pass

    # also scan recent messages
    try:
        async for m in channel.history(limit=50):
            if await _is_panel_message(m):
                try:
                    await m.delete()
                except Exception:
                    pass
    except Exception:
        pass

async def upsert_write_panel(channel: discord.TextChannel) -> Optional[discord.Message]:
    """
    Delete any old panels and post a fresh one so:
      - buttons never go stale after redeploy
      - panel stays near bottom
    """
    try:
        await _delete_existing_panels(channel)
    except Exception:
        pass

    try:
        view = WritePanelView()
        emb = _build_panel_embed()
        msg = await channel.send(content=PANEL_HIDDEN_MARKER, embed=emb, view=view)
        try:
            await msg.pin(reason="Traveler Log Write Panel")
        except Exception:
            pass
        return msg
    except Exception:
        return None

# =====================
# PUBLIC: REGISTER VIEWS (persistent buttons)
# =====================
def register_views(client: discord.Client):
    client.add_view(WritePanelView())
    client.add_view(LogActionsView(author_id=0))  # dummy for custom_ids after restart

# =====================
# STARTUP CLEANUP + ENSURE PANEL
# =====================
async def ensure_write_panels(client: discord.Client, guild_id: int):
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

    # Cleanup + post fresh
    await upsert_write_panel(ch)

# =====================
# SLASH COMMANDS
# =====================
def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int = None):
    """
    Registers:
      - /postlogbutton (admin): posts/reposts the write panel in current channel
      - /writelog: fallback command to open modal
    """
    guild_obj = discord.Object(id=guild_id)
    admin_role = int(admin_role_id or ADMIN_ROLE_ID)

    @tree.command(
        name="postlogbutton",
        description="(Admin) Post/Repost the 'Write Log' panel in this channel",
        guild=guild_obj,
    )
    async def postlogbutton(interaction: discord.Interaction):
        ok = False
        try:
            if isinstance(interaction.user, discord.Member):
                ok = any(r.id == admin_role for r in interaction.user.roles)
        except Exception:
            ok = False

        if not ok:
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("❌ Must be used in a text channel.", ephemeral=True)
            return

        msg = await upsert_write_panel(ch)
        if msg:
            await interaction.response.send_message("✅ Posted/reposted and pinned.", ephemeral=True)
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
# No lock enforcement (per your request)
# =====================
async def enforce_travelerlog_lock(message: discord.Message):
    return