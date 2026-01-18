# travelerlogs_module.py
# Button-only Traveler Logs:
# - Posts a persistent "🖋️ Write Log" button message that is ALWAYS the newest message in the channel
#   (by re-posting itself after each log) so it sits at the bottom under the latest log.
# - Button opens a Modal (Title + Log)
# - Log posts as an embed with auto Year/Day pulled from time_module
# - Each posted log embed includes an "✏️ Edit Log" button (author-only) which opens a modal to edit.
# - Optional lock: delete normal text messages in a specific CATEGORY so only bot logs appear.
#
# ENV (optional):
#   TRAVELERLOGS_LOCK_CATEGORY_ID=1434615650890023133   # if set, delete normal user messages in that category
#   TRAVELERLOGS_CONTROL_MESSAGE_TEXT="Tap to write a Traveler Log"
#   TRAVELERLOGS_MAX_LOG_CHARS=3500
#   TRAVELERLOGS_MAX_TITLE_CHARS=120
#   TRAVELERLOGS_ALLOW_EDIT_MINUTES=0   # 0 = unlimited, otherwise limit edits
#
# In main.py:
#   import travelerlogs_module
#   travelerlogs_module.setup_travelerlog_commands(tree, GUILD_ID)   # registers /writelog fallback (optional)
#   (and in on_ready, call travelerlogs_module.ensure_controls_in_category(client) OR per-channel)
#   In on_message: await travelerlogs_module.enforce_travelerlog_lock(message)
#
# If you want FULL button-only (no slash command), you can ignore the setup_travelerlog_commands entirely.

import os
import time
import json
import asyncio
from typing import Optional, Tuple, Dict, Any, Set

import discord
from discord import app_commands

import time_module  # must expose get_time_state() or compatible helper

# =====================
# CONFIG
# =====================
TRAVELERLOG_EMBED_COLOR = 0x8B5CF6  # purple
CONTROL_EMBED_COLOR = 0x2F3136

TRAVELERLOG_TITLE = "📖 Traveler Log"

LOCK_CATEGORY_ID = int(os.getenv("TRAVELERLOGS_LOCK_CATEGORY_ID", "0")) or None

CONTROL_MESSAGE_TEXT = os.getenv(
    "TRAVELERLOGS_CONTROL_MESSAGE_TEXT",
    "Tap the button below to write a Traveler Log."
)

MAX_LOG_CHARS = int(os.getenv("TRAVELERLOGS_MAX_LOG_CHARS", "3500"))
MAX_TITLE_CHARS = int(os.getenv("TRAVELERLOGS_MAX_TITLE_CHARS", "120"))
ALLOW_EDIT_MINUTES = int(os.getenv("TRAVELERLOGS_ALLOW_EDIT_MINUTES", "0"))  # 0 = unlimited

DATA_DIR = os.getenv("TRAVELERLOGS_DATA_DIR", "/data")
STATE_FILE = os.getenv("TRAVELERLOGS_STATE_FILE", os.path.join(DATA_DIR, "travelerlogs_state.json"))

# Custom IDs
CID_OPEN_MODAL = "travlog:open_modal"
CID_EDIT_BTN_PREFIX = "travlog:edit:"  # + message_id

# =====================
# STATE
# =====================
_state: Dict[str, Any] = {
    # channel_id -> control_message_id
    "control_by_channel": {},
    # posted_message_id -> {author_id, channel_id, created_ts}
    "posts": {},
}

_loaded = False
_state_lock = asyncio.Lock()


# =====================
# STATE FILE HELPERS
# =====================
def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def _load_state():
    global _loaded, _state
    if _loaded:
        return
    _ensure_dir(STATE_FILE)
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # merge safely
                _state["control_by_channel"] = data.get("control_by_channel", {}) or {}
                _state["posts"] = data.get("posts", {}) or {}
    except Exception:
        pass
    _loaded = True


def _save_state():
    try:
        _ensure_dir(STATE_FILE)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f)
    except Exception:
        pass


# =====================
# TIME HELPERS
# =====================
def _get_current_day_year() -> Tuple[int, int]:
    """
    Pull current Year + Day from the time system.
    Falls back safely if time isn't initialised yet.
    """
    try:
        # You already had this in your earlier module; keep same expectation.
        st = time_module.get_time_state()
        year = int(st.get("year", 1))
        day = int(st.get("day", 1))
        return year, day
    except Exception:
        return 1, 1


# =====================
# EMBED BUILDERS
# =====================
def _build_control_embed() -> discord.Embed:
    e = discord.Embed(
        title="🖋️ Write a Traveler Log",
        description=CONTROL_MESSAGE_TEXT,
        color=CONTROL_EMBED_COLOR,
    )
    e.set_footer(text="Tap the button • A form will open")
    return e


def _build_log_embed(author_name: str, year: int, day: int, title: str, entry: str) -> discord.Embed:
    embed = discord.Embed(
        title=TRAVELERLOG_TITLE,
        color=TRAVELERLOG_EMBED_COLOR,
    )
    embed.add_field(
        name="🗓️ Solunaris Time",
        value=f"**Year {year} • Day {day}**",
        inline=False,
    )
    # Keep title separate so it stands out
    embed.add_field(
        name=title.strip()[:MAX_TITLE_CHARS] if title.strip() else "Untitled",
        value=entry.strip()[:MAX_LOG_CHARS] if entry.strip() else "*No content*",
        inline=False,
    )
    embed.set_footer(text=f"Logged by {author_name}")
    return embed


def _build_control_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="🖋️ Write Log",
        style=discord.ButtonStyle.primary,
        custom_id=CID_OPEN_MODAL
    ))
    return view


def _build_edit_view(message_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="✏️ Edit Log",
        style=discord.ButtonStyle.secondary,
        custom_id=f"{CID_EDIT_BTN_PREFIX}{message_id}"
    ))
    return view


# =====================
# CONTROL MESSAGE MANAGEMENT
# =====================
async def _delete_message_safe(msg: discord.Message):
    try:
        await msg.delete()
    except Exception:
        pass


async def _ensure_control_message(channel: discord.TextChannel) -> Optional[int]:
    """
    Ensure a single control message exists for this channel.
    Returns its message_id.
    """
    _load_state()
    async with _state_lock:
        stored_id = _state["control_by_channel"].get(str(channel.id))

    # If we have an ID, verify it exists
    if stored_id:
        try:
            msg = await channel.fetch_message(int(stored_id))
            # Ensure it has the view (Discord may drop components on old messages in rare cases)
            try:
                await msg.edit(embed=_build_control_embed(), view=_build_control_view())
            except Exception:
                pass
            return int(stored_id)
        except Exception:
            # message missing => recreate
            pass

    # Create new control message
    try:
        msg = await channel.send(embed=_build_control_embed(), view=_build_control_view())
    except Exception:
        return None

    async with _state_lock:
        _state["control_by_channel"][str(channel.id)] = str(msg.id)
        _save_state()

    return msg.id


async def bump_control_to_bottom(channel: discord.TextChannel):
    """
    Make the control message the newest message in channel by deleting and re-posting it.
    This keeps the button ALWAYS at the bottom under the most recent log.
    """
    _load_state()
    async with _state_lock:
        stored_id = _state["control_by_channel"].get(str(channel.id))

    # Delete old if exists
    if stored_id:
        try:
            old = await channel.fetch_message(int(stored_id))
            await _delete_message_safe(old)
        except Exception:
            pass

    # Recreate
    new_id = await _ensure_control_message(channel)
    if new_id is None:
        return

    # Pin it (optional). You asked "pinned to the bottom" — pin is independent of bottom,
    # but we can pin for visibility AND keep it bottom by bumping.
    try:
        msg = await channel.fetch_message(int(new_id))
        await msg.pin(reason="Traveler Logs control button")
    except Exception:
        pass


async def ensure_controls_in_category(client: discord.Client, category_id: int):
    """
    OPTIONAL helper: ensure every text channel in a category has a control message.
    Call this once in on_ready after the bot is logged in.
    """
    guilds = client.guilds
    if not guilds:
        return

    for g in guilds:
        cat = g.get_channel(category_id)
        if cat and isinstance(cat, discord.CategoryChannel):
            for ch in cat.text_channels:
                await _ensure_control_message(ch)


# =====================
# INTERACTION HANDLERS (Buttons + Modals)
# =====================
class TravelerLogCreateModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Write Traveler Log")

        self.log_title = discord.ui.TextInput(
            label="Title",
            placeholder="A short title for your log",
            required=True,
            max_length=MAX_TITLE_CHARS,
        )
        self.log_entry = discord.ui.TextInput(
            label="Log",
            placeholder="Write your log here…",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=MAX_LOG_CHARS,
        )
        self.add_item(self.log_title)
        self.add_item(self.log_entry)

    async def on_submit(self, interaction: discord.Interaction):
        year, day = _get_current_day_year()

        embed = _build_log_embed(
            author_name=interaction.user.display_name,
            year=year,
            day=day,
            title=str(self.log_title.value),
            entry=str(self.log_entry.value),
        )

        # Post the log embed
        msg = await interaction.channel.send(embed=embed)
        # Add edit button
        try:
            await msg.edit(view=_build_edit_view(msg.id))
        except Exception:
            pass

        # Store authorship
        _load_state()
        async with _state_lock:
            _state["posts"][str(msg.id)] = {
                "author_id": str(interaction.user.id),
                "channel_id": str(interaction.channel.id),
                "created_ts": time.time(),
            }
            # Keep posts map from growing forever (basic cap)
            if len(_state["posts"]) > 5000:
                # drop oldest ~1000
                items = list(_state["posts"].items())
                items.sort(key=lambda kv: float(kv[1].get("created_ts", 0)))
                for k, _v in items[:1000]:
                    _state["posts"].pop(k, None)
            _save_state()

        # Bump control message so it's always the newest message
        try:
            await bump_control_to_bottom(interaction.channel)
        except Exception:
            pass

        await interaction.response.send_message("✅ Traveler log recorded.", ephemeral=True)


class TravelerLogEditModal(discord.ui.Modal):
    def __init__(self, target_message_id: int, existing_title: str, existing_body: str):
        super().__init__(title="Edit Traveler Log")
        self.target_message_id = target_message_id

        self.log_title = discord.ui.TextInput(
            label="Title",
            required=True,
            max_length=MAX_TITLE_CHARS,
            default=existing_title[:MAX_TITLE_CHARS] if existing_title else "",
        )
        self.log_entry = discord.ui.TextInput(
            label="Log",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=MAX_LOG_CHARS,
            default=existing_body[:MAX_LOG_CHARS] if existing_body else "",
        )
        self.add_item(self.log_title)
        self.add_item(self.log_entry)

    async def on_submit(self, interaction: discord.Interaction):
        # Rebuild embed with current time stamp (year/day always current OR keep original?)
        # You asked it to be auto-stamped from time system; for edits we will KEEP the original stamp
        # if we can read it, otherwise use current.
        channel = interaction.channel
        try:
            msg = await channel.fetch_message(int(self.target_message_id))
        except Exception:
            await interaction.response.send_message("❌ Could not find that log message.", ephemeral=True)
            return

        # Try to read existing Year/Day from embed field 0, else use current
        year, day = _get_current_day_year()
        try:
            if msg.embeds:
                emb0 = msg.embeds[0]
                for f in getattr(emb0, "fields", []):
                    if "Solunaris Time" in (f.name or ""):
                        # f.value like "**Year 2 • Day 329**"
                        import re
                        m = re.search(r"Year\s+(\d+).*Day\s+(\d+)", f.value or "")
                        if m:
                            year = int(m.group(1))
                            day = int(m.group(2))
                        break
        except Exception:
            pass

        new_embed = _build_log_embed(
            author_name=interaction.user.display_name,
            year=year,
            day=day,
            title=str(self.log_title.value),
            entry=str(self.log_entry.value),
        )

        try:
            await msg.edit(embed=new_embed, view=_build_edit_view(msg.id))
        except Exception:
            await interaction.response.send_message("❌ I couldn't edit that message (missing permissions?).", ephemeral=True)
            return

        await interaction.response.send_message("✅ Log updated.", ephemeral=True)


async def _handle_open_modal(interaction: discord.Interaction):
    await interaction.response.send_modal(TravelerLogCreateModal())


async def _handle_edit_button(interaction: discord.Interaction, target_message_id: int):
    _load_state()
    post = _state["posts"].get(str(target_message_id))

    # If we don't have state, we can still try to allow edit only if user is author by reading footer name is unreliable.
    if not post:
        await interaction.response.send_message("❌ I can't verify ownership for this log (state missing).", ephemeral=True)
        return

    author_id = int(post.get("author_id", "0") or 0)
    created_ts = float(post.get("created_ts", 0) or 0)

    if interaction.user.id != author_id:
        await interaction.response.send_message("❌ Only the original author can edit this log.", ephemeral=True)
        return

    if ALLOW_EDIT_MINUTES > 0:
        if time.time() - created_ts > (ALLOW_EDIT_MINUTES * 60):
            await interaction.response.send_message("❌ Editing window has expired for this log.", ephemeral=True)
            return

    # Fetch message to prefill
    try:
        msg = await interaction.channel.fetch_message(int(target_message_id))
    except Exception:
        await interaction.response.send_message("❌ Could not find that log message.", ephemeral=True)
        return

    existing_title = ""
    existing_body = ""
    try:
        if msg.embeds:
            emb = msg.embeds[0]
            # fields[1] is title/body (as created)
            if len(emb.fields) >= 2:
                existing_title = emb.fields[1].name or ""
                existing_body = emb.fields[1].value or ""
    except Exception:
        pass

    await interaction.response.send_modal(
        TravelerLogEditModal(target_message_id, existing_title, existing_body)
    )


# =====================
# PUBLIC: register persistent views + optional slash
# =====================
def register_persistent_views(client: discord.Client):
    """
    Call this ONCE (in on_ready) so buttons keep working after restarts.
    """
    # Control view
    client.add_view(_build_control_view())

    # Edit view is message-id specific; but Discord requires exact custom_id matching
    # We can register a dummy view with a dynamic handler by using on_interaction below instead.
    # So no add_view() for edit buttons here.


def setup_travelerlog_commands(tree: app_commands.CommandTree, guild_id: int):
    """
    OPTIONAL fallback /writelog (in case you ever want it).
    Button-only usage does not require this at all.
    """
    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(name="writelog", guild=guild_obj, description="Write a traveler log (opens form)")
    async def writelog_cmd(i: discord.Interaction):
        await i.response.send_modal(TravelerLogCreateModal())


# =====================
# PUBLIC: interaction router (call from main.on_interaction)
# =====================
async def handle_interaction(interaction: discord.Interaction):
    """
    Route our button interactions.
    Must be called from main.py via client.event on_interaction.
    """
    try:
        if not interaction.type == discord.InteractionType.component:
            return
        data = interaction.data or {}
        cid = data.get("custom_id")
        if not cid:
            return

        if cid == CID_OPEN_MODAL:
            await _handle_open_modal(interaction)
            return

        if cid.startswith(CID_EDIT_BTN_PREFIX):
            mid_str = cid.split(CID_EDIT_BTN_PREFIX, 1)[1]
            try:
                mid = int(mid_str)
            except Exception:
                await interaction.response.send_message("❌ Invalid edit target.", ephemeral=True)
                return
            await _handle_edit_button(interaction, mid)
            return
    except Exception:
        # never crash the bot on interactions
        return


# =====================
# PUBLIC: lock enforcement (call from main.on_message)
# =====================
async def enforce_travelerlog_lock(message: discord.Message):
    """
    Deletes normal user text messages inside the configured CATEGORY.
    This allows channels to remain clean while still allowing button interactions.
    """
    if message.author.bot:
        return

    if LOCK_CATEGORY_ID is None:
        return

    try:
        if not isinstance(message.channel, discord.TextChannel):
            return
        if not message.channel.category or message.channel.category.id != LOCK_CATEGORY_ID:
            return
    except Exception:
        return

    # Allow messages that are just system interaction notices? (ignore)
    # We only delete normal user messages.
    try:
        await message.delete()
    except discord.Forbidden:
        pass
    except Exception:
        pass


# =====================
# PUBLIC: ensure control exists in a channel (call from main.on_ready)
# =====================
async def ensure_control_in_channel(client: discord.Client, channel_id: int):
    ch = client.get_channel(int(channel_id))
    if ch is None:
        try:
            ch = await client.fetch_channel(int(channel_id))
        except Exception:
            ch = None
    if ch is None or not isinstance(ch, discord.TextChannel):
        return
    await _ensure_control_message(ch)
    # Optional: pin it (and keep bottom via bumping)
    try:
        msg_id = _state["control_by_channel"].get(str(ch.id))
        if msg_id:
            msg = await ch.fetch_message(int(msg_id))
            await msg.pin(reason="Traveler Logs control button")
    except Exception:
        pass