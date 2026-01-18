import os
import re
import time
import asyncio
from collections import deque
from typing import Optional, Callable, Awaitable

import discord

# =========================
# ENV / CONFIG
# =========================

CROSSCHAT_ENABLED = os.getenv("CROSSCHAT_ENABLED", "1").strip() not in ("0", "false", "False", "no", "NO")

# Discord channel to bridge
CROSSCHAT_DISCORD_CHANNEL_ID = int(os.getenv("CROSSCHAT_DISCORD_CHANNEL_ID", "1448575647285776444"))

# Poll interval for GetChat
CROSSCHAT_POLL_SECONDS = float(os.getenv("CROSSCHAT_POLL_SECONDS", "5"))

# Prevent loops / spam
CROSSCHAT_DEDUPE_MAX = int(os.getenv("CROSSCHAT_DEDUPE_MAX", "300"))
CROSSCHAT_MAX_LINES_PER_POLL = int(os.getenv("CROSSCHAT_MAX_LINES_PER_POLL", "10"))

# Message formatting
CROSSCHAT_MAP_NAME = os.getenv("CROSSCHAT_MAP_NAME", "Solunaris")
CROSSCHAT_DISCORD_TO_INGAME_PREFIX = os.getenv("CROSSCHAT_DISCORD_TO_INGAME_PREFIX", "[Discord]")
CROSSCHAT_INGAME_TO_DISCORD_PREFIX = os.getenv("CROSSCHAT_INGAME_TO_DISCORD_PREFIX", f"[{CROSSCHAT_MAP_NAME}]")

# Ark chat has limits; keep it safe
INGAME_MAX_LEN = int(os.getenv("CROSSCHAT_INGAME_MAX_LEN", "200"))

# If GetChat returns a lot of history, seed dedupe on first run
CROSSCHAT_SEED_DEDUPE = os.getenv("CROSSCHAT_SEED_DEDUPE", "1").strip() not in ("0", "false", "False", "no", "NO")


# =========================
# RCON CALL WRAPPER
# =========================

async def _call_rcon(rcon_command: Callable, command: str) -> str:
    """
    Your project rcon_command typically looks like:
      - rcon_command(command)
      - rcon_command(command, timeout=...)
    NOT: rcon_command(host, port, password, command)

    This wrapper tries the compatible call patterns.
    """
    # Try: rcon_command(command)
    try:
        res = rcon_command(command)
        if asyncio.iscoroutine(res):
            return (await res) or ""
        return res or ""
    except TypeError:
        pass

    # Try: rcon_command(command, timeout)
    try:
        res = rcon_command(command, 10)
        if asyncio.iscoroutine(res):
            return (await res) or ""
        return res or ""
    except TypeError as e:
        raise TypeError(f"rcon_command signature not supported by crosschat_module: {e}")


# =========================
# PARSING
# =========================

# Common server log prefix style:
# [2026.01.14-22.50.19:410][852]2026.01.14_22.50.19: AdminCmd: ...
_LOG_PREFIX_RE = re.compile(r"^\[?\d{4}\.\d{2}\.\d{2}[-_]\d{2}\.\d{2}\.\d{2}[:\.\d]*\]?\s*(?:\[\d+\])?\s*\d{4}\.\d{2}\.\d{2}[_-]\d{2}\.\d{2}\.\d{2}:\s*")

# Fallback: strip leading bracket groups like [....][...]
_BRACKET_PREFIX_RE = re.compile(r"^(?:\[[^\]]+\]\s*){1,3}")

def _normalize_whitespace(s: str) -> str:
    s = s.replace("\r", " ").replace("\n", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def _extract_chat_lines(getchat_raw: str) -> list[str]:
    """
    Attempt to extract readable chat lines from GetChat output.
    We keep this flexible because ASA/GetChat formats vary between providers.
    """
    if not getchat_raw:
        return []

    lines = []
    for raw_ln in getchat_raw.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue

        # Remove heavy log prefixes
        ln = _LOG_PREFIX_RE.sub("", ln)
        ln = _BRACKET_PREFIX_RE.sub("", ln)
        ln = _normalize_whitespace(ln)

        if not ln:
            continue

        # Filter out obvious non-chat noise
        low = ln.lower()
        if "admincmd:" in low:
            continue
        if "[debug]" in low:
            continue
        if "joined this ark" in low or "left this ark" in low:
            continue

        # Typical chat may look like: "PlayerName: message"
        # Keep only plausible chat lines containing ":"
        if ":" in ln and len(ln) < 400:
            lines.append(ln)

    return lines


def _discord_safe(s: str) -> str:
    # Avoid Discord mentions and formatting abuse
    s = s.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    return s


def _ingame_safe(s: str) -> str:
    s = _normalize_whitespace(s)
    # Remove quotes that can break command parsing
    s = s.replace('"', "'")
    # Hard cap
    if len(s) > INGAME_MAX_LEN:
        s = s[:INGAME_MAX_LEN - 1] + "…"
    return s


# =========================
# STATE (DEDUPE)
# =========================

_seen = deque(maxlen=CROSSCHAT_DEDUPE_MAX)
_seeded = False
_last_poll_ts = 0.0
async def run_crosschat_loop(client: discord.Client, rcon_command: Callable):
    """
    Polls RCON 'admincheat GetChat' and forwards new messages into the Discord channel.
    """
    global _seeded, _last_poll_ts

    if not CROSSCHAT_ENABLED:
        print("[crosschat] disabled by CROSSCHAT_ENABLED=0")
        return

    await client.wait_until_ready()

    channel = client.get_channel(CROSSCHAT_DISCORD_CHANNEL_ID)
    if channel is None:
        print(f"[crosschat] ERROR: Discord channel {CROSSCHAT_DISCORD_CHANNEL_ID} not found/visible to bot.")
        return

    print(f"[crosschat] ✅ running (channel_id={CROSSCHAT_DISCORD_CHANNEL_ID}, poll={CROSSCHAT_POLL_SECONDS}s)")

    while not client.is_closed():
        try:
            # Avoid accidental hammering if loop gets delayed
            now = time.time()
            if _last_poll_ts and (now - _last_poll_ts) < (CROSSCHAT_POLL_SECONDS * 0.5):
                await asyncio.sleep(CROSSCHAT_POLL_SECONDS)
                continue
            _last_poll_ts = now

            raw = await _call_rcon(rcon_command, "admincheat GetChat")
            lines = _extract_chat_lines(raw)

            if not lines:
                await asyncio.sleep(CROSSCHAT_POLL_SECONDS)
                continue

            # First run: seed dedupe so we don't spam old history
            if not _seeded and CROSSCHAT_SEED_DEDUPE:
                for ln in lines[-CROSSCHAT_DEDUPE_MAX:]:
                    _seen.append(ln)
                _seeded = True
                await asyncio.sleep(CROSSCHAT_POLL_SECONDS)
                continue
            _seeded = True

            # Forward only new lines (deduped)
            new_lines = []
            for ln in lines:
                if ln in _seen:
                    continue
                _seen.append(ln)
                new_lines.append(ln)

            if not new_lines:
                await asyncio.sleep(CROSSCHAT_POLL_SECONDS)
                continue

            # Limit per poll to avoid discord spam bursts
            new_lines = new_lines[-CROSSCHAT_MAX_LINES_PER_POLL:]

            for ln in new_lines:
                msg = f"{CROSSCHAT_INGAME_TO_DISCORD_PREFIX} {_discord_safe(ln)}"
                await channel.send(msg)

        except Exception as e:
            print(f"[crosschat] GetChat error for {CROSSCHAT_MAP_NAME}: {e}")

        await asyncio.sleep(CROSSCHAT_POLL_SECONDS)


async def on_discord_message(message: discord.Message, rcon_command: Callable):
    """
    Relays messages from the Discord channel into the game using:
      admincheat ServerChat <message>
    Global chat only, per your request.
    """
    if not CROSSCHAT_ENABLED:
        return

    # Only bridge the configured channel
    if message.channel.id != CROSSCHAT_DISCORD_CHANNEL_ID:
        return

    # Ignore bots/webhooks to avoid loops
    if message.author.bot:
        return

    # Ignore commands
    if message.content.startswith("/"):
        return

    content = message.content.strip()
    if not content:
        return

    # Remove newlines + cap
    safe = _ingame_safe(content)

    # Add author tag
    author = message.author.display_name
    author = _ingame_safe(author)

    payload = f"{CROSSCHAT_DISCORD_TO_INGAME_PREFIX} {author}: {safe}"

    try:
        await _call_rcon(rcon_command, f"admincheat ServerChat {payload}")
    except Exception as e:
        print(f"[crosschat] Discord->Game error: {e}")