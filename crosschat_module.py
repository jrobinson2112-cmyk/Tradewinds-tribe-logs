# crosschat_module.py
# ------------------------------------------------------------
# ASA Cross-Chat (RCON -> Discord + Discord -> RCON)
# - Polls RCON GetChat and forwards NEW lines into 1 Discord channel
# - Relays Discord messages from that channel back into in-game global chat (ServerChat)
# - Includes RAW GetChat debug print (first 800 chars) so you can see exactly what ASA returns
#
# Expected to be started from main.py like:
#   asyncio.create_task(crosschat_module.run_crosschat_loop(client, rcon_cmd))
#
# Where rcon_cmd is an async callable:
#   await rcon_cmd("GetChat", timeout=8.0)  (or "admincheat GetChat")
# ------------------------------------------------------------

import os
import time
import asyncio
import hashlib
from collections import deque
import discord

# =========================
# ENV CONFIG
# =========================
# The 1 Discord channel used for crosschat (global)
CROSSCHAT_CHANNEL_ID = int(os.getenv("CROSSCHAT_CHANNEL_ID", "1448575647285776444"))

# Poll interval (seconds)
CROSSCHAT_POLL_SECONDS = float(os.getenv("CROSSCHAT_POLL_SECONDS", "5"))

# Max chat lines to remember for dedupe
CROSSCHAT_DEDUPE_MAX = int(os.getenv("CROSSCHAT_DEDUPE_MAX", "500"))

# Optional: prefix when sending Discord -> game
DISCORD_TO_GAME_PREFIX = os.getenv("DISCORD_TO_GAME_PREFIX", "[Discord]")

# Optional: if you want to only forward lines containing something (leave blank to forward all)
CROSSCHAT_FILTER_CONTAINS = os.getenv("CROSSCHAT_FILTER_CONTAINS", "").strip() or None

# Safety: block @everyone/@here
SAFE_ALLOWED_MENTIONS = discord.AllowedMentions.none()

# =========================
# INTERNAL STATE
# =========================
_started = False
_seen_hashes = deque(maxlen=CROSSCHAT_DEDUPE_MAX)
_last_activity_ts = 0.0

# Command variants (ASA sometimes wants admincheat, sometimes not)
_GETCHAT_CANDIDATES = ("admincheat GetChat", "GetChat")
_SERVERCHAT_CANDIDATES = ("admincheat ServerChat", "ServerChat")


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def _normalize_line(line: str) -> str:
    # Normalize whitespace and strip weird nulls
    line = line.replace("\x00", "").strip()
    # Collapse internal whitespace a bit (keeps readability)
    line = " ".join(line.split())
    return line


def _extract_chat_lines(raw: str) -> list[str]:
    """
    Very permissive parser:
    - Split into lines
    - Drop empties and obvious non-chat boilerplate
    - Return remaining lines
    We'll refine this once we see your RAW GetChat output.
    """
    if not raw:
        return []

    lines = []
    for ln in raw.splitlines():
        ln = _normalize_line(ln)
        if not ln:
            continue

        low = ln.lower()

        # Common non-chat / noise responses
        if "server received" in low:
            continue
        if "no response" in low:
            continue
        if low in ("ok", "executing", "done"):
            continue

        # Optional filter
        if CROSSCHAT_FILTER_CONTAINS and CROSSCHAT_FILTER_CONTAINS.lower() not in low:
            continue

        lines.append(ln)

    return lines


async def _try_rcon(rcon_command, command: str, timeout: float = 8.0) -> str:
    # rcon_command signatures across your project vary, so we try both common patterns
    try:
        return await rcon_command(command, timeout=timeout)
    except TypeError:
        # maybe rcon_command(command) only
        return await rcon_command(command)


async def _getchat(rcon_command) -> str:
    """
    Try both command spellings.
    Returns the first non-empty response (even if it's not chat).
    """
    last = ""
    for cmd in _GETCHAT_CANDIDATES:
        try:
            out = await _try_rcon(rcon_command, cmd, timeout=10.0)
            last = out or last
            # Return immediately if we got something non-empty
            if out and out.strip():
                return out
        except Exception:
            continue
    return last or ""


async def _serverchat(rcon_command, msg: str) -> None:
    """
    Send to in-game global chat.
    Tries admincheat ServerChat and ServerChat.
    """
    # ASA serverchat usually wants quotes kept simple
    msg = msg.replace("\n", " ").strip()
    if not msg:
        return

    last_err = None
    for base in _SERVERCHAT_CANDIDATES:
        cmd = f"{base} {msg}"
        try:
            await _try_rcon(rcon_command, cmd, timeout=8.0)
            return
        except Exception as e:
            last_err = e

    if last_err:
        raise last_err


def setup_crosschat_handlers(client: discord.Client, rcon_command) -> None:
    """
    Registers Discord -> Game relay handler (listens for messages in CROSSCHAT_CHANNEL_ID).
    Safe to call once.
    """
    global _started
    if _started:
        return
    _started = True

    async def _on_message(message: discord.Message):
        try:
            # Ignore bots/webhooks
            if message.author.bot:
                return
            if message.webhook_id is not None:
                return

            # Only our crosschat channel
            if message.channel.id != CROSSCHAT_CHANNEL_ID:
                return

            content = (message.content or "").strip()
            if not content:
                return

            # Prevent pings in-game
            content = content.replace("@everyone", "everyone").replace("@here", "here")

            author = message.author.display_name
            payload = f"{DISCORD_TO_GAME_PREFIX} {author}: {content}"

            await _serverchat(rcon_command, payload)

        except Exception as e:
            print(f"[crosschat] Discord->Game send error: {e}")

    client.add_listener(_on_message, "on_message")


async def run_crosschat_loop(client: discord.Client, rcon_command):
    """
    Polls GetChat and forwards NEW messages to Discord.
    """
    global _last_activity_ts

    # Register handlers once
    setup_crosschat_handlers(client, rcon_command)

    await client.wait_until_ready()
    ch = client.get_channel(CROSSCHAT_CHANNEL_ID)

    if ch is None:
        print(f"[crosschat] ❌ Could not find Discord channel id={CROSSCHAT_CHANNEL_ID}")
        return

    print(f"[crosschat] ✅ running (channel_id={CROSSCHAT_CHANNEL_ID}, poll={CROSSCHAT_POLL_SECONDS}s)")

    while True:
        try:
            raw = await _getchat(rcon_command)

            # ✅ RAW debug (first 800 chars)
            print("[crosschat] RAW GetChat (first 800 chars):", (raw or "")[:800].replace("\n", "\\n"))

            lines = _extract_chat_lines(raw)

            # Forward only NEW lines (dedupe by hash)
            to_send = []
            for ln in lines:
                h = _h(ln)
                if h in _seen_hashes:
                    continue
                _seen_hashes.append(h)
                to_send.append(ln)

            if to_send:
                # Send in order they appear (oldest -> newest)
                for ln in to_send[-20:]:  # safety cap per poll
                    await ch.send(ln, allowed_mentions=SAFE_ALLOWED_MENTIONS)
                    _last_activity_ts = time.time()

        except Exception as e:
            print(f"[crosschat] GetChat/forward error: {e}")

        await asyncio.sleep(CROSSCHAT_POLL_SECONDS)