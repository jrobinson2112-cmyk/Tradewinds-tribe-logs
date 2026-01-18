# travelerlogs_module.py
# Traveler logs (/writelog) with automatic Year/Day pulled from time_module (current calculated time)
# + optional lock enforcement for a channel/category
#
# Env (optional):
#   TRAVELERLOG_EMBED_COLOR=0x8B5CF6
#   TRAVELERLOG_LOCK_CHANNEL_IDS=1462402354535075890,123...
#   TRAVELERLOG_LOCK_CATEGORY_IDS=123456789012345678,987...
#   TRAVELERLOG_LOCK_NAME_CONTAINS=traveler-log   (fallback name rule if no IDs set)

import os
import discord
from discord import app_commands
from typing import Optional, Tuple, List

import time_module  # pulls current Solunaris time


# =====================
# CONFIG
# =====================
TRAVELERLOG_EMBED_COLOR = int(os.getenv("TRAVELERLOG_EMBED_COLOR", "0x8B5CF6"), 16)
TRAVELERLOG_TITLE = "📖 Traveler Log"

_LOCK_CHANNEL_IDS = {
    int(x.strip())
    for x in os.getenv("TRAVELERLOG_LOCK_CHANNEL_IDS", "").split(",")
    if x.strip().isdigit()
}
_LOCK_CATEGORY_IDS = {
    int(x.strip())
    for x in os.getenv("TRAVELERLOG_LOCK_CATEGORY_IDS", "").split(",")
    if x.strip().isdigit()
}
_LOCK_NAME_CONTAINS = os.getenv("TRAVELERLOG_LOCK_NAME_CONTAINS", "traveler-log").strip().lower()


# =====================
# TIME HELPERS
# =====================
def _safe_get_current_year_day() -> Tuple[int, int]:
    """
    Get CURRENT calculated (year, day) from time_module.
    Falls back to (1,1) safely.
    Tries multiple implementations because your time_module evolved over time.
    """
    try:
        # 1) Preferred: if time_module exposes a helper
        fn = getattr(time_module, "get_current_year_day", None)
        if callable(fn):
            y, d = fn()
            return int(y), int(d)

        # 2) If time_module exposes a state getter (must represent CURRENT time, not just anchor)
        fn = getattr(time_module, "get_time_state", None)
        if callable(fn):
            st = fn()
            # accept either {"year":..,"day":..} or {"current_year":..,"current_day":..}
            y = st.get("year", st.get("current_year", 1))
            d = st.get("day", st.get("current_day", 1))
            return int(y), int(d)

        # 3) If time_module has _calc_now() like: (minute_of_day, day, year, seconds_into_minute)
        fn = getattr(time_module, "_calc_now", None)
        if callable(fn):
            out = fn()
            if out and len(out) >= 3:
                minute_of_day, day, year = out[0], out[1], out[2]
                return int(year), int(day)

        # 4) Fallback: read internal _state (may be anchor, but better than nothing)
        st = getattr(time_module, "_state", None)
        if isinstance(st, dict):
            y = st.get("year", 1)
            d = st.get("day", 1)
            return int(y), int(d)

    except Exception:
        pass

    return 1, 1


# =====================
# EMBED HELPERS
# =====================
def _chunk_text(s: str, max_len: int) -> List[str]:
    s = (s or "").strip()
    if not s:
        return [""]
    chunks: List[str] = []
    i = 0
    while i < len(s):
        chunks.append(s[i : i + max_len])
        i += max_len
    return chunks


def _build_travelerlog_embed(author: discord.abc.User, title: str, entry: str) -> discord.Embed:
    year, day = _safe_get_current_year_day()

    embed = discord.Embed(
        title=TRAVELERLOG_TITLE,
        color=TRAVELERLOG_EMBED_COLOR,
    )

    embed.add_field(
        name="🗓️ Solunaris Time",
        value=f"**Year {year} • Day {day}**",
        inline=False,
    )

    # Discord limits:
    # - Embed description: 4096 chars
    # - Field value: 1024 chars
    #
    # We'll put the title as a field name and the entry split across fields.
    clean_title = (title or "Log").strip()
    if len(clean_title) > 256:
        clean_title = clean_title[:253] + "…"

    parts = _chunk_text(entry, 1000)  # stay safely under 1024
    if parts and parts[0]:
        embed.add_field(name=clean_title, value=parts[0], inline=False)
        for idx, p in enumerate(parts[1:], start=2):
            embed.add_field(name=f"{clean_title} (cont. {idx})", value=p, inline=False)
    else:
        embed.add_field(name=clean_title, value="(empty)", inline=False)

    embed.set_footer(text=f"Logged by {author.display_name}")
    return embed


# =====================
# COMMAND SETUP
# =====================
def setup_travelerlog_commands(
    tree: app_commands.CommandTree,
    guild_id: int,
    client: Optional[discord.Client] = None,  # optional; main can pass it or not
):
    """
    Registers /writelog (not admin locked)
    """
    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(
        name="writelog",
        description="Write a traveler log (auto-stamped with current Year & Day)",
        guild=guild_obj,
    )
    @app_commands.describe(
        title="Short title for your log entry",
        entry="The log text",
    )
    async def writelog(interaction: discord.Interaction, title: str, entry: str):
        # Must be used in a text channel/thread
        if interaction.channel is None:
            await interaction.response.send_message("❌ Can't post logs here.", ephemeral=True)
            return

        embed = _build_travelerlog_embed(interaction.user, title, entry)

        # Post to the channel the command was used in
        await interaction.channel.send(embed=embed)
        await interaction.response.send_message("✅ Traveler log recorded.", ephemeral=True)

    print("[travelerlogs_module] ✅ /writelog registered")


# =====================
# OPTIONAL: LOCK ENFORCEMENT
# =====================
def _is_locked_channel(ch: discord.abc.GuildChannel) -> bool:
    # If explicit IDs configured, use them
    if _LOCK_CHANNEL_IDS or _LOCK_CATEGORY_IDS:
        if getattr(ch, "id", None) in _LOCK_CHANNEL_IDS:
            return True
        cat = getattr(ch, "category", None)
        if cat and getattr(cat, "id", None) in _LOCK_CATEGORY_IDS:
            return True
        return False

    # Fallback: name contains rule
    name = getattr(ch, "name", "") or ""
    return _LOCK_NAME_CONTAINS and (_LOCK_NAME_CONTAINS in name.lower())


async def enforce_travelerlog_lock(message: discord.Message):
    """
    Deletes normal messages in locked traveler-log channels/categories.
    Allows:
      - bots
      - messages created by interactions (slash commands)
    """
    if message.author.bot:
        return

    ch = message.channel
    # Only applies to guild text channels / threads
    if not hasattr(ch, "guild"):
        return

    try:
        locked = _is_locked_channel(ch)  # type: ignore[arg-type]
    except Exception:
        locked = False

    if not locked:
        return

    # Allow slash-command output messages (interaction responses)
    if getattr(message, "interaction", None) is not None:
        return

    # Optionally allow attachments? (you wanted locked, so we delete everything)
    try:
        await message.delete()
    except discord.Forbidden:
        pass
    except Exception:
        pass