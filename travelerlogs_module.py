# travelerlogs_module.py
# Button-only Traveler Logs (FINAL, category-wide panels + safe rate-limit pacing) + LOCATION FIELD:
# - White panel embed with Write Log button
# - Blue log embeds (📜 Traveler Log)
# - Shows Year/Day under title at top (no "Solunaris time" field)
# - ✅ Adds mandatory one-line "Location" field in Write + Edit modals
# - ✅ Displays Location under Year/Day at the top of the embed
# - Panel stays at bottom by deleting previous panel and reposting after each log
# - Edit Log + Add Image buttons (only author)
# - Add Image grants temporary Send Messages permission for upload window
# - /postlogbutton admin slash command to manually post the panel in the current channel
# - Startup ensure: posts panel in EVERY channel in the category (except exclusions) safely to avoid 429s
#
# IMPORTANT LIMITS / REALITY CHECK:
# - Discord cannot truly "stick" a message permanently at the bottom. We simulate it by reposting after logs.
# - Embeds: description max 4096. Field values max 1024. This module uses DESCRIPTION for long log text.

import os
import asyncio
import discord
from discord import app_commands
from typing import Optional, Dict, Any, Tuple, List, Set

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

# Category-wide mode: put the panel in every channel in this category
TRAVELERLOGS_CATEGORY_ID = int(os.getenv("TRAVELERLOGS_CATEGORY_ID", "1434615650890023133"))

# Exclusions (channels inside category that should NOT get a panel)
EXCLUDED_CHANNEL_IDS: Set[int] = {
    1462539723112321218,
    1437457789164191939,
    1455315150859927663,
    1456386974167466106,
}

# Admin role allowed to use /postlogbutton
ADMIN_ROLE_ID = int(os.getenv("TRAVELERLOGS_ADMIN_ROLE_ID", "1439069787207766076"))

# Image policy (reliable: 1)
MAX_IMAGES_PER_LOG = 1

# Temporary upload window (seconds) - configurable
TEMP_UPLOAD_SECONDS = int(os.getenv("TRAVELERLOGS_UPLOAD_SECONDS", "60"))

# How many recent messages we scan to find/delete old panels
PANEL_SCAN_LIMIT = int(os.getenv("TRAVELERLOGS_PANEL_SCAN_LIMIT", "50"))

# Rate-limit safety when ensuring panels across many channels (seconds)
ENSURE_PANEL_DELAY_SECONDS = float(os.getenv("TRAVELERLOGS_ENSURE_PANEL_DELAY", "2.5"))

# =====================
# IN-MEMORY STATE
# =====================

# log message id -> {"author_id": int, "image_filename": str|None}
_LOG_META: Dict[int, Dict[str, Any]] = {}

# Quick cache: channel_id -> last panel message id (best effort)
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

def _chunk_text(text: str, limit: int = 3400) -> List[str]:
    """
    Embed description hard limit 4096; keep margin for header/location/title and spacing.
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

def _sanitize_location(loc: str) -> str:
    loc = (loc or "").strip()
    # Keep it one line
    loc = loc.replace("\n", " ").replace("\r", " ")
    # Reasonable length to prevent embed bloat
    if len(loc) > 120:
        loc = loc[:120].rstrip() + "…"
    return loc

# =====================
# EMBED BUILDERS
# =====================

def _build_panel_embed() -> discord.Embed:
    e = discord.Embed(
        title=PANEL_TITLE,
        description="Press **Write Log** to open the form.\n\n"
                    "After you post a log, this panel re-posts itself so it stays at the bottom.",
        color=PANEL_EMBED_COLOR,
    )
    return e

def _build_log_embed(
    *,
    year: int,
    day: int,
    location: str,
    entry_title: str,
    body: str,
    author_name: str,
    image_filename: Optional[str] = None,
    page: int = 1,
    total_pages: int = 1,
) -> discord.Embed:
    # Structured description so we can parse it back reliably:
    # Line 1: **Year X • Day Y** *(Page a/b)*
    # Line 2: **Location:** Somewhere
    # Line 3: **Title**
    # Blank
    # Body...

    header = f"**Year {year} • Day {day}**"
    if total_pages > 1:
        header += f"   *(Page {page}/{total_pages})*"

    loc_line = f"**Location:** {(_sanitize_location(location) or 'Unknown')}"
    title_line = f"**{(entry_title or '').strip() or 'Untitled'}**"

    desc_parts = [header, loc_line, title_line]
    if body:
        desc_parts.append(body)

    e = discord.Embed(
        title=LOG_TITLE,
        description="\n\n".join(desc_parts)[:4096],
        color=LOG_EMBED_COLOR,
    )

    if image_filename:
        e.set_image(url=f"attachment://{image_filename}")

    e.set_footer(text=f"Logged by {author_name}")
    return e

def _parse_log_embed_description(desc: str) -> Tuple[int, int, str, str, str]:
    """
    Returns (year, day, location, title, body) from our structured description.
    Safe defaults if parsing fails.
    """
    year, day = 1, 1
    location = ""
    title = ""
    body = ""

    if not desc:
        return year, day, location, title, body

    lines = desc.splitlines()

    # First line: **Year X • Day Y**   *(Page a/b)*
    try:
        first = lines[0].replace("*", "")
        tokens = first.replace("•", "").split()
        # tokens like: ['Year','2','Day','336','(Page','1/2)'] (page bits may exist)
        if "Year" in tokens and "Day" in tokens:
            year = int(tokens[tokens.index("Year") + 1])
            day = int(tokens[tokens.index("Day") + 1])
    except Exception:
        pass

    # Find "Location:" line and Title line
    # Because we add blank lines between sections, lines may include empty strings.
    # We'll search for a line containing "Location:" (after stripping *)
    loc_idx = None
    for i, ln in enumerate(lines[:10]):  # only need the top bit
        cleaned = ln.replace("*", "").strip()
        if cleaned.lower().startswith("location:") or cleaned.lower().startswith("location"):
            loc_idx = i
            break
        if "Location:" in cleaned:
            loc_idx = i
            break

    if loc_idx is not None:
        try:
            loc_line = lines[loc_idx].replace("*", "").strip()
            # "Location: Something" or "Location:  Something"
            if "Location:" in loc_line:
                location = loc_line.split("Location:", 1)[1].strip()
            else:
                # fallback
                parts = loc_line.split(":", 1)
                if len(parts) == 2:
                    location = parts[1].strip()
        except Exception:
            location = ""

        # Title should be after location line, skipping blanks
        t_idx = loc_idx + 1
        while t_idx < len(lines) and not lines[t_idx].strip():
            t_idx += 1
        if t_idx < len(lines):
            try:
                title = lines[t_idx].strip()
                # remove surrounding ** if present
                if title.startswith("**") and title.endswith("**") and len(title) >= 4:
                    title = title[2:-2].strip()
                else:
                    title = title.replace("**", "").strip()
            except Exception:
                title = ""

        # Body is everything after title line (skip blank lines)
        b_idx = t_idx + 1
        while b_idx < len(lines) and not lines[b_idx].strip():
            b_idx += 1
        if b_idx < len(lines):
            body = "\n".join(lines[b_idx:]).strip()
    else:
        # If location line missing, best-effort:
        # try treat second non-empty line as title, rest body
        nonempty = [ln for ln in lines if ln.strip()]
        if len(nonempty) >= 2:
            possible_title = nonempty[1].replace("**", "").strip()
            title = possible_title
            body = "\n".join(nonempty[2:]).strip() if len(nonempty) > 2 else ""

    return year, day, location, title, body

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
    if not msg.components:
        return False
    return True

async def _delete_old_panels(channel: discord.TextChannel):
    """
    Deletes any prior panel messages (recent scan + cached ID).
    """
    cid = channel.id
    cached_id = _LAST_PANEL_ID.get(cid)
    if cached_id:
        try:
            m = await channel.fetch_message(cached_id)
            if _is_panel_message(m):
                await m.delete()
        except Exception:
            pass

    try:
        async for m in channel.history(limit=PANEL_SCAN_LIMIT):
            if _is_panel_message(m):
                try:
                    await m.delete()
                except Exception:
                    pass
    except Exception:
        pass

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

# =====================
# TEMP PERMISSIONS FOR IMAGE UPLOAD
# =====================

async def _grant_temp_send_messages(channel: discord.TextChannel, member: discord.Member) -> bool:
    try:
        ow = channel.overwrites_for(member)
        ow.send_messages = True
        await channel.set_permissions(member, overwrite=ow, reason="TravelerLogs temp upload window")
        return True
    except Exception:
        return False

async def _revoke_temp_send_messages(channel: discord.TextChannel, member: discord.Member):
    try:
        ow = channel.overwrites_for(member)
        ow.send_messages = None
        await channel.set_permissions(member, overwrite=ow, reason="TravelerLogs temp upload window end")
    except Exception:
        pass

# =====================
# UI: MODALS  ✅ LOCATION ADDED
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
        self.location = discord.ui.TextInput(
            label="Location",
            required=True,
            placeholder="Where are you? (one line)",
            max_length=120,
        )
        self.entry_title = discord.ui.TextInput(
            label="Title",
            required=True,
            placeholder="Short title for your log entry",
            max_length=256,
        )
        self.entry_body = discord.ui.TextInput(
            label="Log",
            required=True,
            style=discord.TextStyle.paragraph,
            placeholder="Write your traveler log...",
            max_length=4000,
        )

        self.add_item(self.year)
        self.add_item(self.day)
        self.add_item(self.location)
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

        loc = _sanitize_location(str(self.location.value))

        self.result = {
            "year": max(1, y),
            "day": max(1, d),
            "location": loc if loc else "Unknown",
            "title": str(self.entry_title.value).strip()[:256],
            "body": str(self.entry_body.value).rstrip(),
        }
        await interaction.response.defer(ephemeral=True)

class EditLogModal(discord.ui.Modal, title="Edit Traveler Log"):
    def __init__(self, *, default_year: int, default_day: int, default_location: str, default_title: str, default_body: str):
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
        self.location = discord.ui.TextInput(
            label="Location",
            required=True,
            default=_sanitize_location(default_location) or "Unknown",
            max_length=120,
        )
        self.entry_title = discord.ui.TextInput(
            label="Title",
            required=True,
            default=(default_title or "")[:256],
            max_length=256,
        )
        self.entry_body = discord.ui.TextInput(
            label="Log",
            required=True,
            style=discord.TextStyle.paragraph,
            default=(default_body or "")[:4000],
            max_length=4000,
        )

        self.add_item(self.year)
        self.add_item(self.day)
        self.add_item(self.location)
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

        loc = _sanitize_location(str(self.location.value))

        self.result = {
            "year": max(1, y),
            "day": max(1, d),
            "location": loc if loc else "Unknown",
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
                location=modal.result["location"],
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

        year, day, location, title, body = 1, 1, "Unknown", "", ""

        try:
            emb = msg.embeds[0]
            year, day, location, title, body = _parse_log_embed_description(emb.description or "")
        except Exception:
            pass

        modal = EditLogModal(
            default_year=year,
            default_day=day,
            default_location=location,
            default_title=title,
            default_body=body,
        )
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
            location=modal.result["location"],
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

        # Continuations
        if len(new_chunks) > 1:
            for i, chunk in enumerate(new_chunks[1:], start=2):
                cont = _build_log_embed(
                    year=modal.result["year"],
                    day=modal.result["day"],
                    location=modal.result["location"],
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
            f"(I {'temporarily allowed' if granted else 'could not change permissions, but you can still try'} sending.)",
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

        try:
            emb = msg.embeds[0]
            year, day, location, title, body = _parse_log_embed_description(emb.description or "")

            new_embed = _build_log_embed(
                year=year,
                day=day,
                location=location or "Unknown",
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
    client.add_view(LogActionsView(author_id=0))

# =====================
# STARTUP ENSURE (CATEGORY-WIDE)
# =====================

async def ensure_write_panels(client: discord.Client, guild_id: int):
    """
    Ensures a panel exists in EVERY text channel inside TRAVELERLOGS_CATEGORY_ID,
    except excluded channels. Paces requests to avoid 429.
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

    for ch in category.channels:
        if not isinstance(ch, discord.TextChannel):
            continue
        if ch.id in EXCLUDED_CHANNEL_IDS:
            continue

        try:
            await refresh_panel(ch)
        except Exception:
            pass

        await asyncio.sleep(max(0.5, ENSURE_PANEL_DELAY_SECONDS))

# =====================
# SLASH COMMANDS
# =====================

def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int):
    """
    Includes:
      /postlogbutton  (admin)
      /writelog       (opens same modal as button)
    """
    guild_obj = discord.Object(id=guild_id)

    @tree.command(
        name="postlogbutton",
        description="(Admin) Post the 'Write Log' panel in this channel",
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
        await interaction.followup.send("✅ Panel posted (and any old panel removed).", ephemeral=True)

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
# NO LOCK ENFORCEMENT (Discord perms handle this)
# =====================

async def enforce_travelerlog_lock(message: discord.Message):
    return