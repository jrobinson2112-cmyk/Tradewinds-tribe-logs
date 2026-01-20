# travelerlogs_module.py
# Button-only Traveler Logs (FINAL + category rollout):
# - White panel embed with Write Log button
# - Blue log embeds (📜 Traveler Log)
# - Shows Year/Day under title (no "Solunaris time" line)
# - Panel stays at bottom by deleting previous panel and reposting after each log
# - Edit Log + Add Image buttons (only author)
# - Add Image grants temporary Send Messages permission for upload window
# - /postlogbutton admin slash command to manually post the panel in the current channel
# - Startup ensure: posts panel in EVERY channel in a category (except EXCLUDED_CHANNEL_IDS)
# - Safe rollout pacing + respects Discord 429 retry_after when hit
#
# NOTES:
# - Discord cannot truly “stick” a message at the bottom permanently.
#   This module simulates it by deleting old panel(s) and reposting a fresh one.
# - Multiple images “gallery style” in one embed isn’t possible; one embed image max.
#   This module enforces MAX_IMAGES_PER_LOG = 1 for reliability.

import os
import asyncio
import time
import discord
from discord import app_commands
from typing import Optional, Dict, Any, Tuple, List

import time_module

# =====================
# CONFIG
# =====================

# Embed colours
PANEL_EMBED_COLOR = 0xFFFFFF  # white
LOG_EMBED_COLOR = 0x1F5FBF    # blue-ish

# Titles
PANEL_TITLE = "🖋️ Write a Traveler Log"
LOG_TITLE = "📜 Traveler Log"

# Category to apply panels to
TRAVELERLOGS_CATEGORY_ID = int(os.getenv("TRAVELERLOGS_CATEGORY_ID", "1434615650890023133"))

# Optional exclusions (won’t get panel even if in category)
EXCLUDED_CHANNEL_IDS = {
    1462539723112321218,
    1437457789164191939,
    1455315150859927663,
    1456386974167466106,
}

# Admin role allowed to use /postlogbutton
ADMIN_ROLE_ID = int(os.getenv("TRAVELERLOGS_ADMIN_ROLE_ID", "1439069787207766076"))

# Image policy (reliable: 1)
MAX_IMAGES_PER_LOG = 1

# Temporary upload window (seconds)
TEMP_UPLOAD_SECONDS = int(os.getenv("TRAVELERLOGS_UPLOAD_SECONDS", "60"))

# How many recent messages we scan to find/delete old panels
PANEL_SCAN_LIMIT = int(os.getenv("TRAVELERLOGS_PANEL_SCAN_LIMIT", "50"))

# Safe rollout pacing (to avoid 429). This is the delay BETWEEN channels.
PANEL_ROLLOUT_DELAY_SECONDS = float(os.getenv("TRAVELERLOGS_PANEL_ROLLOUT_DELAY_SECONDS", "1.5"))

# Optional: only refresh panel if missing (set 0 to always refresh/cleanup duplicates on startup)
PANEL_STARTUP_ONLY_IF_MISSING = os.getenv("TRAVELERLOGS_STARTUP_ONLY_IF_MISSING", "0").lower() in ("1", "true", "yes", "on")

# =====================
# IN-MEMORY STATE
# =====================

# log message id -> {"author_id": int, "image_filename": str|None}
_LOG_META: Dict[int, Dict[str, Any]] = {}

# quick cache: channel_id -> last panel message id (best effort)
_LAST_PANEL_ID: Dict[int, int] = {}

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

def _chunk_text(text: str, limit: int = 3900) -> List[str]:
    """
    Discord embed limit is 4096; keep margin.
    Splits long logs into multiple pages (auto continuation).
    """
    text = text or ""
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
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

def _build_panel_embed() -> discord.Embed:
    e = discord.Embed(
        title=PANEL_TITLE,
        description=(
            "Press **Write Log** to open the form.\n\n"
            "After you post a log, this panel reposts itself so it stays at the bottom."
        ),
        color=PANEL_EMBED_COLOR,
    )
    return e

def _build_log_embed(
    *,
    year: int,
    day: int,
    entry_title: str,
    body: str,
    author_name: str,
    image_filename: Optional[str] = None,
    page: int = 1,
    total_pages: int = 1,
) -> discord.Embed:
    header = f"**Year {year} • Day {day}**"
    if total_pages > 1:
        header += f"   *(Page {page}/{total_pages})*"

    e = discord.Embed(
        title=LOG_TITLE,
        description=header,
        color=LOG_EMBED_COLOR,
    )
    e.add_field(name=entry_title or "\u200b", value=body or "\u200b", inline=False)

    if image_filename:
        e.set_image(url=f"attachment://{image_filename}")

    e.set_footer(text=f"Logged by {author_name}")
    return e

# =====================
# RATE LIMIT FRIENDLY HELPERS
# =====================

async def _sleep_for_http429(e: Exception):
    """
    discord.py raises HTTPException; sometimes .retry_after exists (RateLimited).
    We'll best-effort sleep if present.
    """
    retry_after = getattr(e, "retry_after", None)
    if retry_after is None:
        # Some HTTPExceptions include text like "Retrying in X seconds" in logs but not on object.
        return
    try:
        ra = float(retry_after)
        if ra > 0:
            await asyncio.sleep(ra)
    except Exception:
        return

# =====================
# PANEL DETECTION / MANAGEMENT
# =====================

def _is_panel_message(msg: discord.Message) -> bool:
    if not msg.author.bot:
        return False
    if not msg.embeds:
        return False
    if (msg.embeds[0].title or "") != PANEL_TITLE:
        return False
    # must have components (button)
    if not msg.components:
        return False
    return True

async def _delete_old_panels(channel: discord.TextChannel):
    """
    Deletes any prior panel messages we can find (recent scan + cached ID),
    so we can repost a new one to keep it at the bottom.
    """
    cid = channel.id

    # 1) Try cached ID first
    cached_id = _LAST_PANEL_ID.get(cid)
    if cached_id:
        try:
            m = await channel.fetch_message(cached_id)
            if _is_panel_message(m):
                await m.delete()
        except Exception:
            pass

    # 2) Scan recent history
    try:
        async for m in channel.history(limit=PANEL_SCAN_LIMIT):
            if _is_panel_message(m):
                try:
                    await m.delete()
                except Exception:
                    pass
    except Exception:
        pass

async def _find_any_panel(channel: discord.TextChannel) -> Optional[discord.Message]:
    """
    Finds an existing panel without deleting it.
    """
    # Check cached
    cached_id = _LAST_PANEL_ID.get(channel.id)
    if cached_id:
        try:
            m = await channel.fetch_message(cached_id)
            if _is_panel_message(m):
                return m
        except Exception:
            pass

    try:
        async for m in channel.history(limit=PANEL_SCAN_LIMIT):
            if _is_panel_message(m):
                _LAST_PANEL_ID[channel.id] = m.id
                return m
    except Exception:
        pass
    return None

async def _post_panel(channel: discord.TextChannel) -> Optional[discord.Message]:
    """
    Posts a new panel at the bottom.
    """
    try:
        view = WritePanelView()
        emb = _build_panel_embed()
        msg = await channel.send(embed=emb, view=view)
        _LAST_PANEL_ID[channel.id] = msg.id
        return msg
    except Exception:
        return None

async def refresh_panel(channel: discord.TextChannel):
    """
    Deletes existing panel(s) and posts a fresh one at the bottom.
    """
    await _delete_old_panels(channel)
    await _post_panel(channel)

async def ensure_panel(channel: discord.TextChannel):
    """
    Ensures a panel exists. If PANEL_STARTUP_ONLY_IF_MISSING is False, also cleans duplicates.
    """
    if PANEL_STARTUP_ONLY_IF_MISSING:
        existing = await _find_any_panel(channel)
        if existing is None:
            await _post_panel(channel)
        return

    # Default: cleanup + refresh
    await refresh_panel(channel)

# =====================
# TEMP PERMISSIONS FOR IMAGE UPLOAD
# =====================

async def _grant_temp_send_messages(channel: discord.TextChannel, member: discord.Member) -> bool:
    """
    Temporarily grants Send Messages permission to a user in this channel.
    Requires bot to have Manage Channels (to set overwrites).
    """
    try:
        ow = channel.overwrites_for(member)
        ow.send_messages = True
        await channel.set_permissions(member, overwrite=ow, reason="TravelerLogs temp upload window")
        return True
    except Exception:
        return False

async def _revoke_temp_send_messages(channel: discord.TextChannel, member: discord.Member):
    """
    Reverts the member overwrite for send_messages (set back to None).
    """
    try:
        ow = channel.overwrites_for(member)
        ow.send_messages = None
        await channel.set_permissions(member, overwrite=ow, reason="TravelerLogs temp upload window end")
    except Exception:
        pass

# =====================
# UI: MODALS
# =====================

class WriteLogModal(discord.ui.Modal, title="Write a Traveler Log"):
    def __init__(self, default_year: int, default_day: int):
        super().__init__(timeout=300)

        self.year = discord.ui.TextInput(label="Year (number)", required=True, default=str(default_year), max_length=6)
        self.day = discord.ui.TextInput(label="Day (number)", required=True, default=str(default_day), max_length=6)
        self.entry_title = discord.ui.TextInput(label="Title", required=True, placeholder="Short title", max_length=256)
        self.entry_body = discord.ui.TextInput(
            label="Log",
            required=True,
            style=discord.TextStyle.paragraph,
            placeholder="Write your traveler log...",
            max_length=4000,  # modal max
        )

        self.add_item(self.year)
        self.add_item(self.day)
        self.add_item(self.entry_title)
        self.add_item(self.entry_body)

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
            "title": str(self.entry_title.value).strip()[:256],
            "body": str(self.entry_body.value).rstrip(),
        }
        await interaction.response.defer(ephemeral=True)

class EditLogModal(discord.ui.Modal, title="Edit Traveler Log"):
    def __init__(self, *, default_year: int, default_day: int, default_title: str, default_body: str):
        super().__init__(timeout=300)

        self.year = discord.ui.TextInput(label="Year (number)", required=True, default=str(default_year), max_length=6)
        self.day = discord.ui.TextInput(label="Day (number)", required=True, default=str(default_day), max_length=6)
        self.entry_title = discord.ui.TextInput(label="Title", required=True, default=(default_title or "")[:256], max_length=256)
        self.entry_body = discord.ui.TextInput(
            label="Log",
            required=True,
            style=discord.TextStyle.paragraph,
            default=(default_body or "")[:4000],
            max_length=4000,
        )

        self.add_item(self.year)
        self.add_item(self.day)
        self.add_item(self.entry_title)
        self.add_item(self.entry_body)

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
            "title": str(self.entry_title.value).strip()[:256],
            "body": str(self.entry_body.value).rstrip(),
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
                entry_title=modal.result["title"],
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

        # Refresh panel to keep it at bottom
        if isinstance(interaction.channel, discord.TextChannel):
            await refresh_panel(interaction.channel)

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

        year = 1
        day = 1
        title = ""
        body = ""

        try:
            emb = msg.embeds[0]
            desc = (emb.description or "").replace("*", "")
            tokens = desc.replace("•", "").replace("(", "").replace(")", "").split()
            if "Year" in tokens and "Day" in tokens:
                year = int(tokens[tokens.index("Year") + 1])
                day = int(tokens[tokens.index("Day") + 1])
            if emb.fields:
                title = emb.fields[0].name
                body = emb.fields[0].value
        except Exception:
            pass

        modal = EditLogModal(default_year=year, default_day=day, default_title=title, default_body=body)
        await interaction.response.send_modal(modal)
        await modal.wait()
        if not modal.result:
            return

        image_filename = meta.get("image_filename") if meta else None

        new_chunks = _chunk_text(modal.result["body"])
        new_body = new_chunks[0] if new_chunks else ""

        new_embed = _build_log_embed(
            year=modal.result["year"],
            day=modal.result["day"],
            entry_title=modal.result["title"],
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
                    entry_title=modal.result["title"],
                    body=chunk,
                    author_name=_display_name(interaction.user),
                    image_filename=None,
                    page=i,
                    total_pages=len(new_chunks),
                )
                await interaction.channel.send(embed=cont)

        if isinstance(interaction.channel, discord.TextChannel):
            await refresh_panel(interaction.channel)

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

        member: Optional[discord.Member] = interaction.user if isinstance(interaction.user, discord.Member) else None
        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel) or member is None:
            await interaction.response.send_message("❌ This only works in server text channels.", ephemeral=True)
            return

        granted = await _grant_temp_send_messages(ch, member)

        await interaction.response.send_message(
            f"📸 Upload **1 image** in this channel within **{TEMP_UPLOAD_SECONDS}s**.\n"
            f"(I {'temporarily allowed' if granted else 'could not change permissions, but try anyway'} sending.)",
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

        upload_msg: Optional[discord.Message] = None
        try:
            upload_msg = await interaction.client.wait_for("message", timeout=float(TEMP_UPLOAD_SECONDS), check=check)
        except asyncio.TimeoutError:
            pass
        finally:
            await _revoke_temp_send_messages(ch, member)

        if upload_msg is None:
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
            emb = msg.embeds[0]
            year = 1
            day = 1
            title = ""
            body = ""

            desc = (emb.description or "").replace("*", "")
            tokens = desc.replace("•", "").replace("(", "").replace(")", "").split()
            if "Year" in tokens and "Day" in tokens:
                year = int(tokens[tokens.index("Year") + 1])
                day = int(tokens[tokens.index("Day") + 1])

            if emb.fields:
                title = emb.fields[0].name
                body = emb.fields[0].value

            new_embed = _build_log_embed(
                year=year,
                day=day,
                entry_title=title,
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

            if isinstance(interaction.channel, discord.TextChannel):
                await refresh_panel(interaction.channel)

            await interaction.followup.send("✅ Image attached.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to attach image: {e}", ephemeral=True)

# =====================
# PUBLIC: REGISTER VIEWS (persistent)
# =====================

def register_views(client: discord.Client):
    """
    Call in main.py on_ready:
      travelerlogs_module.register_views(client)
    """
    client.add_view(WritePanelView())
    # dummy so custom_ids survive restart
    client.add_view(LogActionsView(author_id=0))

# =====================
# CATEGORY ENSURE (SAFE)
# =====================

async def ensure_write_panels(client: discord.Client, guild_id: int):
    """
    Ensures a panel exists in EVERY text channel under TRAVELERLOGS_CATEGORY_ID,
    except those in EXCLUDED_CHANNEL_IDS. Paces requests to avoid 429.
    """
    await client.wait_until_ready()
    guild = client.get_guild(guild_id)
    if guild is None:
        return

    category = guild.get_channel(TRAVELERLOGS_CATEGORY_ID)
    if category is None:
        try:
            category = await guild.fetch_channel(TRAVELERLOGS_CATEGORY_ID)
        except Exception:
            return

    if not isinstance(category, discord.CategoryChannel):
        return

    # Only text channels
    targets: List[discord.TextChannel] = []
    for ch in category.channels:
        if isinstance(ch, discord.TextChannel) and ch.id not in EXCLUDED_CHANNEL_IDS:
            targets.append(ch)

    # Sort by position for predictable rollout
    targets.sort(key=lambda c: c.position)

    for idx, ch in enumerate(targets, start=1):
        try:
            await ensure_panel(ch)
        except discord.HTTPException as e:
            # If rate limited, respect retry_after if present
            await _sleep_for_http429(e)
        except Exception:
            pass

        # pacing between channels (skip after last)
        if idx < len(targets):
            await asyncio.sleep(max(0.2, PANEL_ROLLOUT_DELAY_SECONDS))

# =====================
# SLASH COMMANDS
# =====================

def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int):
    """
    Includes:
      /postlogbutton  (admin) - refreshes panel in current channel
      /writelog       - opens same modal as button
    """
    guild_obj = discord.Object(id=guild_id)

    @tree.command(
        name="postlogbutton",
        description="(Admin) Post/refresh the 'Write Log' panel in this channel",
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
            await interaction.response.send_message("❌ Use this in a server text channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await refresh_panel(ch)
        await interaction.followup.send("✅ Panel refreshed (old panels removed).", ephemeral=True)

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
# NO LOCK ENFORCEMENT (you said perms handle this)
# =====================

async def enforce_travelerlog_lock(message: discord.Message):
    return