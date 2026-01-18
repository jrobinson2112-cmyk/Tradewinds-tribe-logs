import os
import time
import asyncio
import hashlib
from collections import deque
from typing import Deque, Tuple, Optional

import discord
from discord import app_commands

# =====================
# ENV / CONFIG
# =====================
# Where you want admins to run the command (a Discord channel ID)
ADMIN_RCON_CHANNEL_ID = int(os.getenv("ADMIN_RCON_CHANNEL_ID", "0") or 0)

# Poll GetGameLog every N seconds
GAMELOG_POLL_SECONDS = float(os.getenv("GAMELOG_POLL_SECONDS", "10"))

# How long to keep logs in memory (minutes)
GAMELOG_RETENTION_MINUTES = int(os.getenv("GAMELOG_RETENTION_MINUTES", "180"))  # 3 hours default

# Max unique hashes to keep for dedupe
GAMELOG_DEDUPE_MAX = int(os.getenv("GAMELOG_DEDUPE_MAX", "20000"))

# Optional: print a one-line status on startup
GAMELOG_VERBOSE = os.getenv("GAMELOG_VERBOSE", "true").lower() in ("1", "true", "yes", "y")


# =====================
# INTERNAL STATE
# =====================
# Store (seen_ts, line)
_buffer: Deque[Tuple[float, str]] = deque()

# Dedupe seen hashes
_seen_hashes: Deque[str] = deque()
_seen_set = set()

_running = False
_rcon_command = None


def _hash_line(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()


def _remember_hash(h: str):
    if h in _seen_set:
        return
    _seen_hashes.append(h)
    _seen_set.add(h)
    while len(_seen_hashes) > GAMELOG_DEDUPE_MAX:
        old = _seen_hashes.popleft()
        _seen_set.discard(old)


def _trim_old():
    """Trim buffer by retention window."""
    cutoff = time.time() - (GAMELOG_RETENTION_MINUTES * 60)
    while _buffer and _buffer[0][0] < cutoff:
        _buffer.popleft()


def _clean_line(line: str) -> str:
    # minimal cleanup; keep special chars intact
    return " ".join(line.strip().split())


async def seed_gamelog_once():
    """
    Seed dedupe without spamming buffer on deploy:
    - read current GetGameLog
    - mark lines as seen hashes
    - DO NOT add to buffer
    """
    global _rcon_command
    if _rcon_command is None:
        return
    try:
        txt = await _rcon_command("GetGameLog", timeout=15.0)
        lines = [ln for ln in txt.splitlines() if ln.strip()]
        for ln in lines:
            ln = _clean_line(ln)
            _remember_hash(_hash_line(ln))
        if GAMELOG_VERBOSE:
            print("[rcon_gamelogs] ✅ seeded from current GetGameLog (no backlog spam).")
    except Exception as e:
        print(f"[rcon_gamelogs] seed error: {e}")


async def run_gamelogs_loop(rcon_command):
    """
    Background poller:
    - fetch GetGameLog
    - add only NEW lines to buffer with (time_seen, line)
    """
    global _running, _rcon_command
    if _running:
        return
    _running = True
    _rcon_command = rcon_command

    if GAMELOG_VERBOSE:
        print(
            f"[rcon_gamelogs] ✅ running (poll={GAMELOG_POLL_SECONDS}s, retention={GAMELOG_RETENTION_MINUTES}m)"
        )

    await seed_gamelog_once()

    while True:
        try:
            _trim_old()

            txt = await _rcon_command("GetGameLog", timeout=15.0)
            if not txt:
                await asyncio.sleep(GAMELOG_POLL_SECONDS)
                continue

            now = time.time()
            lines = [ln for ln in txt.splitlines() if ln.strip()]

            # Walk from bottom->top so we tend to capture newest first
            # but we still dedupe and store in arrival time order.
            new_lines = []
            for ln in reversed(lines):
                ln = _clean_line(ln)
                h = _hash_line(ln)
                if h in _seen_set:
                    continue
                _remember_hash(h)
                new_lines.append(ln)

            # Add in correct order (oldest first)
            for ln in reversed(new_lines):
                _buffer.append((now, ln))

        except Exception as e:
            print(f"[rcon_gamelogs] loop error: {e}")

        await asyncio.sleep(GAMELOG_POLL_SECONDS)


def setup_gamelogs_commands(tree: app_commands.CommandTree, guild_id: int):
    guild_obj = discord.Object(id=int(guild_id))

    def _channel_ok(i: discord.Interaction) -> bool:
        if ADMIN_RCON_CHANNEL_ID == 0:
            # If not set, allow anywhere (but you said admin channel, so set it).
            return True
        return getattr(i.channel, "id", None) == ADMIN_RCON_CHANNEL_ID

    @tree.command(name="gamelogs", guild=guild_obj)
    async def gamelogs_cmd(i: discord.Interaction, minutes: int = 60):
        """
        Fetch stored GetGameLog lines seen within the last N minutes (real-time window).
        """
        await i.response.defer(ephemeral=True)

        if not _channel_ok(i):
            await i.followup.send(
                f"❌ Use this in the admin channel only.", ephemeral=True
            )
            return

        if minutes < 1 or minutes > 1440:
            await i.followup.send("❌ minutes must be 1..1440", ephemeral=True)
            return

        _trim_old()
        cutoff = time.time() - (minutes * 60)

        lines = [ln for (ts, ln) in list(_buffer) if ts >= cutoff]

        if not lines:
            await i.followup.send(f"ℹ️ No new logs in the last {minutes} minutes.", ephemeral=True)
            return

        header = f"GetGameLog lines (seen in last {minutes} minutes)\nGenerated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        body = "\n".join(lines)
        text = header + body

        # If short enough, send in-message
        if len(text) <= 1900:
            await i.followup.send(f"```text\n{text}\n```", ephemeral=True)
            return

        # Otherwise upload a .txt file
        data = text.encode("utf-8", errors="replace")
        file = discord.File(fp=discord.BytesIO(data), filename=f"gamelogs_last_{minutes}m.txt")
        await i.followup.send(content="✅ Here you go:", file=file, ephemeral=True)

    print("[rcon_gamelogs] ✅ /gamelogs registered")