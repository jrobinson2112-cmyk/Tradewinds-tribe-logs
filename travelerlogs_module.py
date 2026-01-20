# travelerlogs_module.py
# Button-only Traveler Logs (FINAL):
# - White panel embed with Write Log button
# - Blue log embeds (📜 Traveler Log)
# - Shows Year/Day under title (no "Solunaris time" line)
# - Panel stays at bottom by deleting previous panel and reposting after each log
# - Edit Log + Add Image buttons (only author)
# - Add Image grants temporary Send Messages permission for upload window
# - /postlogbutton admin slash command to manually post the panel
# - Startup ensure: posts panel in test channel (or chosen channel) and removes duplicates
#
# NOTES:
# - Discord cannot "stick" a message permanently at the bottom. This module simulates it by
#   deleting any old panel and reposting a fresh one after each log.

import os
import asyncio
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

# Only show panel in this channel while testing
TEST_ONLY_CHANNEL_ID = int(os.getenv("TRAVELERLOGS_TEST_CHANNEL_ID", "1462402354535075890"))

# Optional exclusions (kept here in case you later scan categories again)
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
# You requested 60 seconds default, configurable via env var.
TEMP_UPLOAD_SECONDS = int(os.getenv("TRAVELERLOGS_UPLOAD_SECONDS", "60"))

# How many recent messages we scan to find/delete old panels
PANEL_SCAN_LIMIT = int(os.getenv("TRAVELERLOGS_PANEL_SCAN_LIMIT", "50"))

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

def _chunk_text(text: str, limit: int = 3900) -> List[str]:
    """
    Discord embed description limit is 4096; keep margin.
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
        description="Press **Write Log** to open the form.\n\n"
                    "After you post a log, this panel will re-post itself so it stays at the bottom.",
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
    # Year/Day under title at top:
    header = f"**Year {year} • Day {day}**"
    if total_pages > 1:
        header += f"   *(Page {page}/{total_pages})*"

    e = discord.Embed(
        title=LOG_TITLE,
        description=header,
        color=LOG_EMBED_COLOR,
    )

    # Main content as fields
    e.add_field(name=entry_title or "\u200b", value=body or "\u200b", inline=False)

    if image_filename:
        e.set_image(url=f"attachment://{image_filename}")

    e.set_footer(text=f"Logged by {author_name}")
    return e

# =====================
# PANEL CATEGORY MODE
# =====================

# Put your category ID here (or set env var TRAVELERLOGS_CATEGORY_ID)
TRAVELERLOGS_CATEGORY_ID = int(os.getenv("TRAVELERLOGS_CATEGORY_ID", "1434615650890023133"))

# Delay between channels to avoid rate limits
PANEL_SCAN_DELAY_SECONDS = float(os.getenv("TRAVELERLOGS_PANEL_SCAN_DELAY_SECONDS", "1.25"))


def _is_panel_message(m: discord.Message) -> bool:
    """
    Detect our panel without any visible marker text.
    We detect:
      - message by the bot
      - has a view/components
      - has a button with custom_id = "travelerlogs:write"
    """
    try:
        if not m.author.bot:
            return False
        if not m.components:
            return False

        # discord.py components are ActionRow -> children (buttons)
        for row in m.components:
            for child in getattr(row, "children", []):
                if getattr(child, "custom_id", None) == "travelerlogs:write":
                    return True
        return False
    except Exception:
        return False


async def _find_existing_panel_anywhere(channel: discord.TextChannel) -> discord.Message | None:
    """
    Look for an existing panel either:
      - pinned (preferred)
      - or among the most recent messages (fallback)
    """
    # 1) pinned check
    try:
        pins = await channel.pins()
        for m in pins:
            if _is_panel_message(m):
                return m
    except Exception:
        pass

    # 2) recent history fallback (in case it got unpinned)
    try:
        async for m in channel.history(limit=25):
            if _is_panel_message(m):
                return m
    except Exception:
        pass

    return None


async def ensure_write_panels(client: discord.Client, guild_id: int):
    """
    Ensures the write panel exists (and pinned) in every text channel inside
    TRAVELERLOGS_CATEGORY_ID, except EXCLUDED_CHANNEL_IDS.

    Safe behavior:
      - skips excluded channels
      - skips non-text channels
      - rate-limit friendly (sleep between channels)
    """
    await client.wait_until_ready()

    guild = client.get_guild(int(guild_id))
    if guild is None:
        try:
            guild = await client.fetch_guild(int(guild_id))
        except Exception:
            return

    # Fetch category
    cat = guild.get_channel(TRAVELERLOGS_CATEGORY_ID)
    if cat is None:
        try:
            cat = await guild.fetch_channel(TRAVELERLOGS_CATEGORY_ID)
        except Exception as e:
            print(f"[travelerlogs] ❌ could not fetch category {TRAVELERLOGS_CATEGORY_ID}: {e}")
            return

    if not isinstance(cat, discord.CategoryChannel):
        print(f"[travelerlogs] ❌ TRAVELERLOGS_CATEGORY_ID is not a category: {TRAVELERLOGS_CATEGORY_ID}")
        return

    # Iterate channels in category
    for ch in cat.channels:
        try:
            if not isinstance(ch, discord.TextChannel):
                continue
            if ch.id in EXCLUDED_CHANNEL_IDS:
                continue

            existing = await _find_existing_panel_anywhere(ch)
            if existing is None:
                # Post + pin a fresh one
                msg = await _post_and_pin_panel(ch)
                if msg:
                    print(f"[travelerlogs] ✅ panel posted in #{ch.name}")
                else:
                    print(f"[travelerlogs] ⚠️ could not post/pin in #{ch.name}")

            await asyncio.sleep(PANEL_SCAN_DELAY_SECONDS)

        except Exception as e:
            # Keep going; don't die on one channel
            print(f"[travelerlogs] ensure_write_panels error in #{getattr(ch,'name','?')}: {e}")
            await asyncio.sleep(PANEL_SCAN_DELAY_SECONDS)

# =====================
# TEMP PERMISSIONS FOR IMAGE UPLOAD
# =====================

async def _grant_temp_send_messages(channel: discord.TextChannel, member: discord.Member, seconds: int) -> bool:
    """
    Temporarily grants Send Messages permission to a user in this channel.
    Requires bot to have Manage Channels (to set overwrites).
    """
    try:
        ow = channel.overwrites_for(member)
        # Explicitly allow send_messages
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
        # Remove explicit allow/deny, return to role-based perms
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

        # Extract from embed
        year = 1
        day = 1
        title = ""
        body = ""

        try:
            emb = msg.embeds[0]
            # description: "**Year X • Day Y** ..."
            desc = (emb.description or "").replace("*", "")
            # "Year 2 • Day 336"
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

        # Keep existing attachment if any; do not wipe it.
        try:
            await msg.edit(embed=new_embed, view=LogActionsView(author_id=interaction.user.id))
        except Exception as e:
            await interaction.followup.send(f"❌ Edit failed: {e}", ephemeral=True)
            return

        # Continuations:
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

        # Keep panel at bottom
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

        # Temp allow send messages for upload window
        member: Optional[discord.Member] = None
        try:
            if isinstance(interaction.user, discord.Member):
                member = interaction.user
        except Exception:
            member = None

        ch = interaction.channel
        if not isinstance(ch, discord.TextChannel) or member is None:
            await interaction.response.send_message("❌ This only works in server text channels.", ephemeral=True)
            return

        granted = await _grant_temp_send_messages(ch, member, TEMP_UPLOAD_SECONDS)

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
            # revoke regardless
            await _revoke_temp_send_messages(ch, member)

        if upload_msg is None:
            await interaction.followup.send("⌛ Timed out waiting for an image.", ephemeral=True)
            return

        # First image attachment
        attachment: Optional[discord.Attachment] = None
        for a in upload_msg.attachments:
            ctype = (a.content_type or "").lower()
            if ctype.startswith("image/"):
                attachment = a
                break

        if not attachment:
            await interaction.followup.send("❌ No image attachment found.", ephemeral=True)
            return

        # Re-upload to the LOG message so attachment:// works even if we delete upload msg
        try:
            file = await attachment.to_file()
        except Exception as e:
            await interaction.followup.send(f"❌ Could not read attachment: {e}", ephemeral=True)
            return

        image_filename = file.filename

        # Rebuild embed (preserve current year/day/title/body)
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

            # keep panel at bottom
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
    # Register dummy actions view so the custom_ids survive restart
    client.add_view(LogActionsView(author_id=0))

# =====================
# STARTUP ENSURE
# =====================

async def ensure_write_panels(client: discord.Client, guild_id: int):
    """
    Ensures panel exists in TEST_ONLY_CHANNEL_ID and removes duplicates.
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

    if ch.id in EXCLUDED_CHANNEL_IDS:
        return

    # Cleanup any old panels and post a fresh one
    await refresh_panel(ch)

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
# NO LOCK ENFORCEMENT (you said perms handle this)
# =====================

async def enforce_travelerlog_lock(message: discord.Message):
    return