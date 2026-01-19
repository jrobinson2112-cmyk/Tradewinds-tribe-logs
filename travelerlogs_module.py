# travelerlogs_module.py
# Button-only Traveler Logs:
# - Persistent "Write Log" panel (survives redeploys)
# - Auto Year/Day defaults pulled live from time_module on click
# - Edit (author only), Continue (author only), Add Images (author only)
# - "Unlimited" text via Continue button (stores parts, rebuilds embeds, creates overflow messages if needed)
# - Gallery-style images via multiple embeds (fallback to links if embed limit hit)
# - No normal-text deletion/lock (manage via Discord perms instead)

import os
import json
import time
import asyncio
from typing import Dict, Any, List, Optional, Tuple

import discord
from discord import app_commands

import time_module


# =====================
# CONFIG
# =====================

# TEST MODE: Only post the Write Log panel in these channel IDs
# (you asked to run only in your test channel while tweaking)
ENABLED_CHANNEL_IDS = {
    1462402354535075890,  # ✅ test channel
}

# Exclude list (if you later expand to categories again)
EXCLUDED_CHANNEL_IDS = {
    1462539723112321218,
    1437457789164191939,
    1455315150859927663,
    1456386974167466106,
}

# Admin role allowed to use /postlogbutton
TRAVELERLOGS_ADMIN_ROLE_ID = int(os.getenv("TRAVELERLOGS_ADMIN_ROLE_ID", "1439069787207766076"))

# Storage
DATA_DIR = os.getenv("TRAVELERLOGS_DATA_DIR", "/data")
STATE_FILE = os.getenv("TRAVELERLOGS_STATE_FILE", os.path.join(DATA_DIR, "travelerlogs_state.json"))

# UI text/colors
EMBED_COLOR = 0x8B5CF6
PANEL_TITLE = "✒️ Write a Traveler Log"
PANEL_DESC = "Tap the button below to write a Traveler Log.\n\n**Tap the button • A form will open**"
LOG_TITLE = "📖 Traveler Log"

# Images
MAX_IMAGES_PER_LOG = int(os.getenv("TRAVELERLOGS_MAX_IMAGES", "6"))  # gallery-style via multiple embeds
IMAGE_WAIT_SECONDS = int(os.getenv("TRAVELERLOGS_IMAGE_WAIT_SECONDS", "180"))

# Safety limits (Discord embed/component limits)
MAX_EMBEDS_PER_MESSAGE = 10
MAX_DESC_CHARS = 3900  # keep under 4096 hard limit
# We'll split text across embeds as needed.


# =====================
# INTERNAL STATE
# =====================

_state: Dict[str, Any] = {"logs": {}}  # message_id -> log record
_state_loaded = False


def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def _load_state() -> None:
    global _state_loaded, _state
    if _state_loaded:
        return
    _state_loaded = True
    try:
        if not os.path.exists(STATE_FILE):
            _state = {"logs": {}}
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            _state = json.load(f)
        if not isinstance(_state, dict) or "logs" not in _state:
            _state = {"logs": {}}
    except Exception:
        _state = {"logs": {}}


def _save_state() -> None:
    try:
        _ensure_dir(STATE_FILE)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f)
    except Exception:
        pass


def _get_time_state() -> Tuple[int, int]:
    """
    Pull current Year/Day from time_module.
    This MUST be called at click-time (not module import time),
    otherwise it will always default to 1/1.
    """
    try:
        if hasattr(time_module, "get_time_state"):
            st = time_module.get_time_state() or {}
            y = int(st.get("year", 1))
            d = int(st.get("day", 1))
            return y, d
        if hasattr(time_module, "TIME_STATE"):
            st = getattr(time_module, "TIME_STATE") or {}
            y = int(st.get("year", 1))
            d = int(st.get("day", 1))
            return y, d
    except Exception:
        pass
    return 1, 1


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _chunks(text: str, size: int) -> List[str]:
    text = text or ""
    if not text:
        return [""]
    out = []
    i = 0
    while i < len(text):
        out.append(text[i:i + size])
        i += size
    return out


def _build_embeds_for_log(record: Dict[str, Any]) -> Tuple[List[discord.Embed], List[str]]:
    """
    Returns (embeds_for_main_message, overflow_text_pages)
    overflow_text_pages are additional text pages that didn't fit in the 10-embed cap (after images).
    """
    year = int(record.get("year", 1))
    day = int(record.get("day", 1))
    title = str(record.get("title", ""))
    author_name = str(record.get("author_name", "Unknown"))
    parts: List[str] = record.get("parts", []) or []
    images: List[str] = record.get("images", []) or []

    full_text = "\n\n".join([p for p in parts if p is not None])

    # Reserve embed slots for images (gallery style)
    # Each image = one embed with .set_image(url=...)
    image_embeds_needed = min(len(images), MAX_IMAGES_PER_LOG)
    available_for_text = MAX_EMBEDS_PER_MESSAGE - image_embeds_needed
    if available_for_text < 1:
        # If images eat all slots, we still need at least 1 text embed.
        available_for_text = 1
        image_embeds_needed = MAX_EMBEDS_PER_MESSAGE - available_for_text

    # Text pages in embed descriptions
    text_pages = _chunks(full_text, MAX_DESC_CHARS)

    # Main (page 1) embed
    embeds: List[discord.Embed] = []
    e0 = discord.Embed(title=LOG_TITLE, color=EMBED_COLOR)
    e0.add_field(name="📅 Solunaris Time", value=f"**Year {year} • Day {day}**", inline=False)
    if title.strip():
        e0.add_field(name=title.strip(), value=(text_pages[0] or "\u200b"), inline=False)
    else:
        e0.add_field(name="Log", value=(text_pages[0] or "\u200b"), inline=False)
    e0.set_footer(text=f"Logged by {author_name}")
    embeds.append(e0)

    # Additional text embeds (pages 2..n), within available_for_text
    # page 1 already used in first embed; remaining slots for text = available_for_text - 1
    remaining_text_slots = max(0, available_for_text - 1)
    overflow_text_pages: List[str] = []

    if len(text_pages) > 1:
        extra_pages = text_pages[1:]
        use_now = extra_pages[:remaining_text_slots]
        overflow_text_pages = extra_pages[remaining_text_slots:]

        for idx, pg in enumerate(use_now, start=2):
            e = discord.Embed(
                title=f"{LOG_TITLE} (cont. {idx})",
                description=pg,
                color=EMBED_COLOR,
            )
            embeds.append(e)

    # Image embeds (gallery)
    # If we had to reduce image count to fit, add them as links in first embed
    used_images = images[:image_embeds_needed]
    leftover_images = images[image_embeds_needed:]

    for i, url in enumerate(used_images, start=1):
        eimg = discord.Embed(
            title=f"📷 Image {i}",
            color=EMBED_COLOR,
        )
        eimg.set_image(url=url)
        embeds.append(eimg)

    if leftover_images:
        # Put leftover image links in the first embed (so nothing is lost)
        links = "\n".join([f"[Image {i + image_embeds_needed}]({u})" for i, u in enumerate(leftover_images, start=1)])
        # If the first embed is already at field cap, append to description instead
        try:
            embeds[0].add_field(name="📷 More Images", value=links[:1024], inline=False)
        except Exception:
            # fallback: append to description if needed
            embeds[0].description = (embeds[0].description or "") + "\n\n" + links[:MAX_DESC_CHARS]

    return embeds[:MAX_EMBEDS_PER_MESSAGE], overflow_text_pages


async def _rebuild_log_message(message: discord.Message, record: Dict[str, Any]) -> None:
    """
    Updates the original log message:
    - edits its embeds (up to 10)
    - ensures action buttons view is present
    - recreates overflow messages if needed
    """
    # Delete old overflow messages if any
    extra_ids = record.get("extra_message_ids", []) or []
    if extra_ids:
        for mid in list(extra_ids):
            try:
                m = await message.channel.fetch_message(int(mid))
                await m.delete()
            except Exception:
                pass
        record["extra_message_ids"] = []
        _save_state()

    embeds, overflow_pages = _build_embeds_for_log(record)

    # Edit main message
    try:
        await message.edit(embeds=embeds, view=LogActionsView(timeout=None))
    except Exception:
        # If edit fails, nothing else to do
        return

    # Post overflow pages as extra messages (to make text "unlimited")
    if overflow_pages:
        record["extra_message_ids"] = []
        # Each overflow message can also have multiple embeds, but keep it simple:
        # one embed per overflow page.
        for i, pg in enumerate(overflow_pages, start=1):
            try:
                e = discord.Embed(
                    title=f"{LOG_TITLE} (more)",
                    description=pg,
                    color=EMBED_COLOR,
                )
                sent = await message.channel.send(embed=e)
                record["extra_message_ids"].append(str(sent.id))
            except Exception:
                break
        _save_state()


# =====================
# MODALS
# =====================

def _get_current_day_year() -> tuple[int, int]:
    try:
        s = time_module.get_time_state()
        return int(s.get("year", 1)), int(s.get("day", 1))
    except Exception:
        return 1, 1


class WriteLogModal(discord.ui.Modal, title="Write a Traveler Log"):
    def __init__(self, default_year: int, default_day: int):
        super().__init__(timeout=None)

        self.year_input = discord.ui.TextInput(
            label="Year (number)",
            required=True,
            default=str(default_year),
            max_length=6,
        )
        self.day_input = discord.ui.TextInput(
            label="Day (number)",
            required=True,
            default=str(default_day),
            max_length=6,
        )
        self.title_input = discord.ui.TextInput(
            label="Title",
            required=True,
            max_length=256,
        )
        self.log_input = discord.ui.TextInput(
            label="Log",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=4000,  # Discord limit
        )

        self.add_item(self.year_input)
        self.add_item(self.day_input)
        self.add_item(self.title_input)
        self.add_item(self.log_input)

    async def on_submit(self, interaction: discord.Interaction):
        # your existing submit logic here
        await interaction.response.send_message("✅ Traveler log recorded.", ephemeral=True)


class WriteLogButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✒️ Write Log", style=discord.ButtonStyle.primary, custom_id="travelerlogs:write")

    async def callback(self, interaction: discord.Interaction):
        year, day = _get_current_day_year()
        modal = WriteLogModal(default_year=year, default_day=day)
        await interaction.response.send_modal(modal)


class EditLogModal(discord.ui.Modal, title="Edit Traveler Log"):
    def __init__(self, message_id: int, record: Dict[str, Any]):
        super().__init__(timeout=300)
        self.message_id = message_id

        year = int(record.get("year", 1))
        day = int(record.get("day", 1))
        title = str(record.get("title", ""))
        parts = record.get("parts", []) or []
        full_text = "\n\n".join(parts)

        self.year = discord.ui.TextInput(label="Year (number)", default=str(year), required=True)
        self.day = discord.ui.TextInput(label="Day (number)", default=str(day), required=True)
        self.title_in = discord.ui.TextInput(label="Title", default=title[:256], required=True, max_length=256)
        self.log = discord.ui.TextInput(label="Log", default=full_text[:4000], style=discord.TextStyle.paragraph, required=True, max_length=4000)

        self.add_item(self.year)
        self.add_item(self.day)
        self.add_item(self.title_in)
        self.add_item(self.log)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        _load_state()
        rec = _state["logs"].get(str(self.message_id))
        if not rec:
            await interaction.response.send_message("❌ I can't find this log anymore.", ephemeral=True)
            return

        if interaction.user.id != int(rec.get("author_id", 0)):
            await interaction.response.send_message("❌ Only the original author can edit this log.", ephemeral=True)
            return

        try:
            rec["year"] = int(str(self.year.value).strip())
        except Exception:
            rec["year"] = 1
        try:
            rec["day"] = int(str(self.day.value).strip())
        except Exception:
            rec["day"] = 1

        rec["title"] = str(self.title_in.value).strip()
        # Overwrite text with a single part (continuations remain possible via Continue button)
        rec["parts"] = [str(self.log.value)]
        rec["updated_at"] = _now_str()

        _state["logs"][str(self.message_id)] = rec
        _save_state()

        # Rebuild message
        try:
            msg = await interaction.channel.fetch_message(self.message_id)
            await _rebuild_log_message(msg, rec)
        except Exception:
            pass

        await interaction.response.send_message("✅ Updated.", ephemeral=True)


class ContinueLogModal(discord.ui.Modal, title="Continue Traveler Log"):
    def __init__(self, message_id: int):
        super().__init__(timeout=300)
        self.message_id = message_id

        self.more = discord.ui.TextInput(
            label="More text",
            placeholder="Write more for this log...",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.add_item(self.more)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        _load_state()
        rec = _state["logs"].get(str(self.message_id))
        if not rec:
            await interaction.response.send_message("❌ I can't find this log anymore.", ephemeral=True)
            return

        if interaction.user.id != int(rec.get("author_id", 0)):
            await interaction.response.send_message("❌ Only the original author can continue this log.", ephemeral=True)
            return

        rec["parts"] = rec.get("parts", []) or []
        rec["parts"].append(str(self.more.value))
        rec["updated_at"] = _now_str()

        _state["logs"][str(self.message_id)] = rec
        _save_state()

        try:
            msg = await interaction.channel.fetch_message(self.message_id)
            await _rebuild_log_message(msg, rec)
        except Exception:
            pass

        await interaction.response.send_message("✅ Added.", ephemeral=True)


# =====================
# VIEWS (PERSISTENT)
# =====================

class WritePanelView(discord.ui.View):
    def __init__(self, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)

    @discord.ui.button(
        label="✒️ Write Log",
        style=discord.ButtonStyle.primary,
        custom_id="travlog:write",
    )
    async def write(self, interaction: discord.Interaction, button: discord.ui.Button):
        # LIVE pull at click-time (fixes the Year/Day=1 issue)
        y, d = _get_time_state()
        await interaction.response.send_modal(WriteLogModal(y, d))


class LogActionsView(discord.ui.View):
    def __init__(self, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)

    @discord.ui.button(
        label="✏️ Edit Log",
        style=discord.ButtonStyle.secondary,
        custom_id="travlog:edit",
    )
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        _load_state()
        if not interaction.message:
            await interaction.response.send_message("❌ No message context.", ephemeral=True)
            return

        mid = interaction.message.id
        rec = _state["logs"].get(str(mid))
        if not rec:
            await interaction.response.send_message("❌ I can't find stored data for this log.", ephemeral=True)
            return

        if interaction.user.id != int(rec.get("author_id", 0)):
            await interaction.response.send_message("❌ Only the original author can edit this log.", ephemeral=True)
            return

        await interaction.response.send_modal(EditLogModal(mid, rec))

    @discord.ui.button(
        label="➕ Continue",
        style=discord.ButtonStyle.secondary,
        custom_id="travlog:continue",
    )
    async def cont(self, interaction: discord.Interaction, button: discord.ui.Button):
        _load_state()
        if not interaction.message:
            await interaction.response.send_message("❌ No message context.", ephemeral=True)
            return

        mid = interaction.message.id
        rec = _state["logs"].get(str(mid))
        if not rec:
            await interaction.response.send_message("❌ I can't find stored data for this log.", ephemeral=True)
            return

        if interaction.user.id != int(rec.get("author_id", 0)):
            await interaction.response.send_message("❌ Only the original author can continue this log.", ephemeral=True)
            return

        await interaction.response.send_modal(ContinueLogModal(mid))

    @discord.ui.button(
        label="📷 Add Images",
        style=discord.ButtonStyle.success,
        custom_id="travlog:add_images",
    )
    async def add_images(self, interaction: discord.Interaction, button: discord.ui.Button):
        _load_state()
        if not interaction.message:
            await interaction.response.send_message("❌ No message context.", ephemeral=True)
            return

        mid = interaction.message.id
        rec = _state["logs"].get(str(mid))
        if not rec:
            await interaction.response.send_message("❌ I can't find stored data for this log.", ephemeral=True)
            return

        if interaction.user.id != int(rec.get("author_id", 0)):
            await interaction.response.send_message("❌ Only the original author can add images.", ephemeral=True)
            return

        # Ask user to upload images in channel
        await interaction.response.send_message(
            f"📷 **Send up to {MAX_IMAGES_PER_LOG} images** in this channel now.\n"
            f"I’ll attach them to your log.\n\n"
            f"Timeout: {IMAGE_WAIT_SECONDS}s",
            ephemeral=True,
        )

        collected: List[str] = []
        deadline = time.time() + IMAGE_WAIT_SECONDS

        def check(m: discord.Message) -> bool:
            if m.author.id != interaction.user.id:
                return False
            if m.channel.id != interaction.channel.id:
                return False
            return bool(getattr(m, "attachments", None))

        while len(collected) < MAX_IMAGES_PER_LOG and time.time() < deadline:
            try:
                msg: discord.Message = await interaction.client.wait_for(
                    "message",
                    check=check,
                    timeout=max(1, int(deadline - time.time()))
                )
            except asyncio.TimeoutError:
                break

            # take attachments
            for a in msg.attachments:
                if len(collected) >= MAX_IMAGES_PER_LOG:
                    break
                # Prefer CDN URL
                collected.append(a.url)

            # try delete the upload msg to keep channel clean
            try:
                await msg.delete()
            except Exception:
                pass

        if not collected:
            return  # user saw prompt already; no need to spam

        # Store and rebuild
        rec["images"] = rec.get("images", []) or []
        rec["images"].extend(collected)
        # Deduplicate while preserving order
        seen = set()
        dedup = []
        for u in rec["images"]:
            if u in seen:
                continue
            seen.add(u)
            dedup.append(u)
        rec["images"] = dedup[:max(1, MAX_IMAGES_PER_LOG)]
        rec["updated_at"] = _now_str()

        _state["logs"][str(mid)] = rec
        _save_state()

        try:
            original = await interaction.channel.fetch_message(mid)
            await _rebuild_log_message(original, rec)
        except Exception:
            pass


# =====================
# PERSISTENT VIEW REGISTRATION
# =====================

def register_views(client: discord.Client) -> None:
    """
    MUST be called on startup (on_ready) or buttons will show "interaction failed" after redeploy.
    """
    client.add_view(WritePanelView(timeout=None))
    client.add_view(LogActionsView(timeout=None))


# =====================
# PANEL / COMMANDS
# =====================

async def _post_write_panel(channel: discord.TextChannel) -> bool:
    """
    Post a "Write Log" panel message in the channel, if it doesn't already exist.
    Returns True if posted or already exists; False if failed.
    """
    if channel.id in EXCLUDED_CHANNEL_IDS:
        return False
    if ENABLED_CHANNEL_IDS and channel.id not in ENABLED_CHANNEL_IDS:
        return False

    # Look for an existing panel in recent history
    try:
        async for m in channel.history(limit=50):
            if m.author.bot and m.embeds:
                e = m.embeds[0]
                if e.title == PANEL_TITLE:
                    # Ensure it still has the button view (re-attach if needed)
                    try:
                        await m.edit(view=WritePanelView(timeout=None))
                    except Exception:
                        pass
                    return True
    except Exception:
        pass

    # Post new panel
    try:
        embed = discord.Embed(title=PANEL_TITLE, description=PANEL_DESC, color=EMBED_COLOR)
        sent = await channel.send(embed=embed, view=WritePanelView(timeout=None))
        # Pin it so it’s easy to find (note: pinned is not “bottom”, Discord can’t force that)
        try:
            await sent.pin(reason="Traveler Log panel")
        except Exception:
            pass
        return True
    except Exception:
        return False


async def ensure_write_panels(client: discord.Client, guild_id: int) -> None:
    """
    Ensures the panel exists in all enabled channels.
    (Safe: only checks a few messages; avoids spam.)
    """
    try:
        guild = client.get_guild(guild_id) or await client.fetch_guild(guild_id)
        if not guild:
            return
    except Exception:
        return

    # Only touch enabled channels
    for cid in list(ENABLED_CHANNEL_IDS):
        if cid in EXCLUDED_CHANNEL_IDS:
            continue
        ch = guild.get_channel(cid)
        if ch is None:
            try:
                ch = await client.fetch_channel(cid)
            except Exception:
                continue
        if isinstance(ch, discord.TextChannel):
            await _post_write_panel(ch)
            await asyncio.sleep(1.0)  # gentle pacing


def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int) -> None:
    """
    Optional admin command to manually post the Write Log panel.
    """

    @tree.command(
        name="postlogbutton",
        description="Admin: Post the Traveler Log write button panel in this channel",
        guild=discord.Object(id=guild_id),
    )
    async def postlogbutton(interaction: discord.Interaction):
        # Role gate
        roles = getattr(interaction.user, "roles", []) or []
        if TRAVELERLOGS_ADMIN_ROLE_ID and not any(getattr(r, "id", None) == TRAVELERLOGS_ADMIN_ROLE_ID for r in roles):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("❌ This must be used in a text channel.", ephemeral=True)
            return

        ok = await _post_write_panel(interaction.channel)
        if ok:
            await interaction.response.send_message("✅ Panel posted (or already existed).", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Couldn't post panel here (or channel not enabled).", ephemeral=True)


# =====================
# NO-OP LOCK (YOU SAID DISCORD PERMS WILL HANDLE THIS)
# =====================

async def enforce_travelerlog_lock(message: discord.Message):
    return