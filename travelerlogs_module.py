# travelerlogs_module.py
# Button-only Traveler Logs with:
# - Write Log panel (posted + pinned via /postlogbutton or startup ensure)
# - Panel auto-reposts to become the newest message (simulated “stick to bottom”)
# - Edit Log button (only author)
# - Add Image button (only author) - reliable attachment:// method
# - /postlogbutton admin command
#
# Notes:
# - True “sticky bottom UI” is not possible in Discord channels.
#   This module simulates it by deleting the old panel and reposting it after each log.
# - No normal text enforcement (you’re handling via Discord perms).
# - Time shown directly under the title as "Year X • Day Y" (no "🗓️ Solunaris Time" line).
# - Removes any TRAVELERLOG_PANEL_* text messages on startup (cleanup).
# - No visible marker strings like TRAVELERLOG_PANEL_V2 / PANEL_MARKER.

import os
import asyncio
import discord
from discord import app_commands
from typing import Optional, Dict, Any, Tuple

import time_module

# =====================
# CONFIG
# =====================

# Log embed (blue)
TRAVELERLOG_EMBED_COLOR = 0x2563EB  # blue
TRAVELERLOG_TITLE = "📜 Traveler Log"

# Panel embed (white-ish)
PANEL_EMBED_COLOR = 0xFFFFFF

# Testing: if set, ONLY this channel gets the panel; else you can expand later.
TEST_ONLY_CHANNEL_ID = int(os.getenv("TRAVELERLOGS_TEST_CHANNEL_ID", "1462402354535075890"))

# Excluded channels (only relevant if you later expand beyond test-only)
EXCLUDED_CHANNEL_IDS = {
    1462539723112321218,
    1437457789164191939,
    1455315150859927663,
    1456386974167466106,
}

# Admin role allowed to use /postlogbutton
ADMIN_ROLE_ID = int(os.getenv("TRAVELERLOGS_ADMIN_ROLE_ID", "1439069787207766076"))

# Max images per log (Discord embed supports one main image reliably)
MAX_IMAGES_PER_LOG = 1

# =====================
# IN-MEMORY STATE
# =====================

# log message id -> {"author_id": int, "image_filename": str|None}
_LOG_META: Dict[int, Dict[str, Any]] = {}

# user_id + channel_id -> last log message id (for “Add Image” flow)
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
    Embed field value limit is 1024, but we store most text in a field.
    We keep chunking conservative. If you want truly huge logs, post extra embeds.
    """
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
    embed = discord.Embed(
        title=TRAVELERLOG_TITLE,
        color=TRAVELERLOG_EMBED_COLOR,
    )

    # Show time directly under title (as requested)
    embed.description = f"**Year {year} • Day {day}**"

    if total_pages > 1:
        embed.add_field(name=title, value=f"{body}\n\n*(Page {page}/{total_pages})*", inline=False)
    else:
        embed.add_field(name=title, value=body or "\u200b", inline=False)

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

        # ✅ simulate "stick to bottom": repost panel as newest message
        await repost_panel_to_bottom(interaction.channel)

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

        # Extract existing info
        year = 1
        day = 1
        title = "Log"
        body = ""
        image_filename = meta.get("image_filename")

        try:
            emb = msg.embeds[0]
            # description: "**Year X • Day Y**"
            desc = (emb.description or "").replace("*", "")
            # "Year 2 • Day 342"
            parts = desc.replace("•", "").split()
            if "Year" in parts and "Day" in parts:
                year = int(parts[parts.index("Year") + 1])
                day = int(parts[parts.index("Day") + 1])

            title = emb.fields[0].name
            body = emb.fields[0].value
            body = body.replace("\n\n*(Page 1/1)*", "")
        except Exception:
            pass

        modal = EditLogModal(default_year=year, default_day=day, default_title=title, default_body=body)
        await interaction.response.send_modal(modal)
        await modal.wait()

        if not modal.result:
            return

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

        # Continuation pages
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

        # keep panel newest
        await repost_panel_to_bottom(interaction.channel)

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
            if (a.content_type or "").lower().startswith("image/"):
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

        # Extract current year/day/title/body from the message
        year = 1
        day = 1
        title = "Log"
        body = ""
        try:
            emb = msg.embeds[0]
            desc = (emb.description or "").replace("*", "")
            parts = desc.replace("•", "").split()
            if "Year" in parts and "Day" in parts:
                year = int(parts[parts.index("Year") + 1])
                day = int(parts[parts.index("Day") + 1])
            title = emb.fields[0].name
            body = emb.fields[0].value
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

        try:
            # ✅ critical: attach the file to THIS message and reference attachment://filename
            await msg.edit(embed=new_embed, attachments=[file], view=LogActionsView(author_id=interaction.user.id))

            meta["image_filename"] = image_filename
            _LOG_META[msg.id] = meta

            try:
                await upload_msg.delete()
            except Exception:
                pass

            await interaction.followup.send("✅ Image attached to your log.", ephemeral=True)

            # keep panel newest
            await repost_panel_to_bottom(interaction.channel)

        except Exception as e:
            await interaction.followup.send(f"❌ Failed to attach image: {e}", ephemeral=True)


# =====================
# PERSISTENT VIEW REGISTRATION
# =====================

def register_views(client: discord.Client):
    """
    Call this in main.on_ready():
        travelerlogs_module.register_views(client)
    """
    client.add_view(WritePanelView())
    # Register dummy to keep component custom_ids alive across restarts
    client.add_view(LogActionsView(author_id=0))


# =====================
# PANEL MANAGEMENT
# =====================

async def _find_latest_panel_message(channel: discord.TextChannel) -> Optional[discord.Message]:
    """
    Find the most recent panel message in recent history.
    No marker strings; detect by embed title and presence of components.
    """
    try:
        async for m in channel.history(limit=50):
            if not m.author.bot:
                continue
            if not m.embeds:
                continue
            emb = m.embeds[0]
            if emb.title != "🖋️ Write a Traveler Log":
                continue
            if m.components:
                return m
    except Exception:
        pass
    return None


async def _post_and_pin_panel(channel: discord.TextChannel) -> Optional[discord.Message]:
    """
    Post the panel WITHOUT any text content (no TRAVELERLOG_PANEL_*).
    Pin is optional; if missing perms, it still works.
    """
    try:
        view = WritePanelView()
        emb = _build_panel_embed()
        msg = await channel.send(embed=emb, view=view)  # ✅ no content=
        try:
            await msg.pin()
        except Exception:
            pass
        return msg
    except Exception:
        return None


async def repost_panel_to_bottom(channel: discord.TextChannel):
    """
    Simulates “sticky bottom” by deleting the last panel and reposting a fresh one.
    """
    if not isinstance(channel, discord.TextChannel):
        return

    old = await _find_latest_panel_message(channel)
    if old:
        try:
            await old.delete()
        except Exception:
            pass

    await _post_and_pin_panel(channel)


async def _cleanup_panels(channel: discord.TextChannel):
    """
    Cleanup on startup:
    - delete any old TRAVELERLOG_PANEL* text messages from older versions
    - remove duplicate pinned panels (keep newest)
    """
    # delete old junk marker messages (text-only)
    try:
        async for m in channel.history(limit=75):
            if m.author.bot and (m.content or "").startswith("TRAVELERLOG_PANEL"):
                try:
                    await m.delete()
                except Exception:
                    pass
    except Exception:
        pass

    # remove duplicate pinned panels
    try:
        pins = await channel.pins()
        panels = [m for m in pins if m.author.bot and m.embeds and m.embeds[0].title == "🖋️ Write a Traveler Log"]
        # keep the newest by created_at
        panels.sort(key=lambda x: x.created_at, reverse=True)
        for extra in panels[1:]:
            try:
                await extra.delete()
            except Exception:
                pass
    except Exception:
        pass


async def ensure_write_panels(client: discord.Client, guild_id: int):
    """
    Ensures panel exists in TEST channel only.
    Also runs cleanup on startup.
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

    # Skip excluded channels just in case
    if ch.id in EXCLUDED_CHANNEL_IDS:
        return

    await _cleanup_panels(ch)

    existing = await _find_latest_panel_message(ch)
    if existing is None:
        await _post_and_pin_panel(ch)


# =====================
# SLASH COMMANDS
# =====================

def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int):
    """
    Registers:
    - /postlogbutton (admin role)
    - /writelog (opens modal; optional convenience)
    """
    global ADMIN_ROLE_ID
    ADMIN_ROLE_ID = int(admin_role_id)

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
                ok = any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles)
        except Exception:
            ok = False

        if not ok:
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("❌ Use this in a text channel.", ephemeral=True)
            return

        # Cleanup old junk, then repost to bottom
        await interaction.response.defer(ephemeral=True)
        await _cleanup_panels(ch)
        await repost_panel_to_bottom(ch)

        await interaction.followup.send("✅ Posted/reposted and pinned.", ephemeral=True)

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