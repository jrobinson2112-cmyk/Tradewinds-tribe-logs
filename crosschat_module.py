import os
import re
import time
import asyncio
from collections import deque
from typing import Callable, Optional, Dict, Deque, Tuple, List

import discord


# =========================
# ENV / CONFIG
# =========================

CROSSCHAT_CHANNEL_ID = int(os.getenv("CROSSCHAT_CHANNEL_ID", "1448575647285776444") or "1448575647285776444")

# Poll frequency for GetChat
CROSSCHAT_POLL_SECONDS = float(os.getenv("CROSSCHAT_POLL_SECONDS", "5") or "5")

# Comma-separated map names you want shown as the prefix in Discord
# Example: "Solunaris,Midgar"
CROSSCHAT_MAPS = [m.strip() for m in os.getenv("CROSSCHAT_MAPS", "Solunaris").split(",") if m.strip()]

# Only allow "global" style messages (filters out tribe/alliance/local)
CROSSCHAT_ONLY_GLOBAL = os.getenv("CROSSCHAT_ONLY_GLOBAL", "1").strip().lower() not in ("0", "false", "no")

# Prevent backlog spam on restart: seed dedupe with current GetChat output
CROSSCHAT_SEED_BACKLOG_ON_START = os.getenv("CROSSCHAT_SEED_BACKLOG_ON_START", "1").strip().lower() not in ("0", "false", "no")

# Max dedupe memory per map
CROSSCHAT_DEDUPE_MAX = int(os.getenv("CROSSCHAT_DEDUPE_MAX", "800") or "800")

# Discord -> game: which command to use. We'll try ServerChat first, then fallback to "admincheat ServerChat"
CROSSCHAT_USE_ADMINCHEAT_PREFIX = os.getenv("CROSSCHAT_USE_ADMINCHEAT_PREFIX", "0").strip().lower() in ("1", "true", "yes")

# Prefix inserted into the in-game message (keep minimal to avoid ugly server formatting)
# Example: "" or "[D]"
CROSSCHAT_INGAME_PREFIX = os.getenv("CROSSCHAT_INGAME_PREFIX", "").strip()

# If you want to hard-block relaying messages that start with e.g. "!"
CROSSCHAT_DISCORD_BLOCK_PREFIXES = [p.strip() for p in os.getenv("CROSSCHAT_DISCORD_BLOCK_PREFIXES", "").split(",") if p.strip()]


# =========================
# INTERNAL STATE
# =========================

# You pass us the project's rcon_command callable from main.py
_RCON: Optional[Callable] = None

# Per-map dedupe of recently seen chat lines (to avoid repeats/backlog spam)
_seen_by_map: Dict[str, Deque[str]] = {m: deque(maxlen=CROSSCHAT_DEDUPE_MAX) for m in CROSSCHAT_MAPS}

# Per-map last poll timestamp (debug / possible future use)
_last_poll_ts: Dict[str, float] = {m: 0.0 for m in CROSSCHAT_MAPS}

# If we successfully discovered which ServerChat form works
_serverchat_mode: Optional[str] = None  # "plain" or "admincheat"


# =========================
# HELPERS
# =========================

def set_rcon_command(rcon_command: Callable):
    """Bind your project's rcon_command into this module."""
    global _RCON
    _RCON = rcon_command


def _clean_discord_text(s: str) -> str:
    """Remove markdown-ish bits that look bad in ARK chat."""
    s = s.replace("\n", " ").strip()
    # Remove code blocks/backticks
    s = s.replace("```", "").replace("`", "")
    # Collapse multiple spaces
    s = re.sub(r"\s+", " ", s)
    return s


def _looks_non_global(line: str) -> bool:
    """
    Very defensive filtering: reject obvious non-global channels.
    ASA/ASE formats vary; we just detect common markers.
    """
    l = line.lower()
    # common channel markers in GetChat outputs
    if "tribe" in l:
        return True
    if "alliance" in l:
        return True
    if "local" in l:
        return True
    # sometimes GetChat has tags like [Tribe] [Alliance] [Local]
    if "[tribe]" in l or "[alliance]" in l or "[local]" in l:
        return True
    return False


def _looks_global(line: str) -> bool:
    """
    Accept if explicitly marked global; if not marked, we'll accept unless it looks non-global.
    """
    l = line.lower()
    if "global" in l or "[global]" in l:
        return True
    # Some servers don't label global in GetChat; treat unlabeled as global unless non-global markers exist
    return not _looks_non_global(line)


def _hash_line(line: str) -> str:
    """
    Stable-ish key for dedupe. We keep it simple:
    strip excessive whitespace; use the line itself.
    """
    return re.sub(r"\s+", " ", line.strip())


def _parse_getchat_output(raw: str) -> List[str]:
    """
    Split GetChat output into individual chat lines.
    """
    if not raw:
        return []
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    # Some RCON wrappers return single-line blobs
    return lines


async def _rcon_call(map_name: str, command: str) -> Optional[str]:
    """
    Call your project's rcon_command with flexible signatures.
    We've seen different versions in your project history.
    """
    if _RCON is None:
        return None

    # Try a few common calling conventions
    # 1) rcon_command(command)
    try:
        res = _RCON(command)
        if asyncio.iscoroutine(res):
            return await res
        return res
    except TypeError:
        pass

    # 2) rcon_command(map_name, command)
    try:
        res = _RCON(map_name, command)
        if asyncio.iscoroutine(res):
            return await res
        return res
    except TypeError:
        pass

    # 3) rcon_command(command, map_name)
    try:
        res = _RCON(command, map_name)
        if asyncio.iscoroutine(res):
            return await res
        return res
    except TypeError:
        pass

    # 4) rcon_command(map_name=..., command=...)
    try:
        res = _RCON(command=command, map_name=map_name)
        if asyncio.iscoroutine(res):
            return await res
        return res
    except Exception:
        return None


async def _send_serverchat(map_name: str, text: str) -> bool:
    """
    Send a message into the game using ServerChat.
    We try the cleanest variant first.
    """
    global _serverchat_mode

    msg = text.strip()
    if not msg:
        return False

    if CROSSCHAT_INGAME_PREFIX:
        msg = f"{CROSSCHAT_INGAME_PREFIX} {msg}".strip()

    # If forced admincheat mode via env:
    if CROSSCHAT_USE_ADMINCHEAT_PREFIX:
        cmd = f'admincheat ServerChat {msg}'
        await _rcon_call(map_name, cmd)
        _serverchat_mode = "admincheat"
        return True

    # If we've already discovered a working mode:
    if _serverchat_mode == "plain":
        await _rcon_call(map_name, f"ServerChat {msg}")
        return True
    if _serverchat_mode == "admincheat":
        await _rcon_call(map_name, f"admincheat ServerChat {msg}")
        return True

    # Try plain ServerChat first
    r1 = await _rcon_call(map_name, f"ServerChat {msg}")
    # Many wrappers return None even on success, so we can't rely on output.
    # We'll optimistically set mode to plain unless we see an obvious error string.
    if isinstance(r1, str) and ("unknown command" in r1.lower() or "not found" in r1.lower()):
        # fallback to admincheat
        await _rcon_call(map_name, f"admincheat ServerChat {msg}")
        _serverchat_mode = "admincheat"
        return True

    _serverchat_mode = "plain"
    return True


# =========================
# DISCORD -> GAME
# =========================

async def on_discord_message(message: discord.Message):
    """
    Call this from main.py's on_message event.

    - Only relays messages from CROSSCHAT_CHANNEL_ID
    - Ignores bots/webhooks
    - Relays to ALL maps in CROSSCHAT_MAPS (global chat)
    """
    try:
        if message.author.bot:
            return
        if message.webhook_id is not None:
            return
        if message.channel.id != CROSSCHAT_CHANNEL_ID:
            return

        content = (message.content or "").strip()
        if not content:
            return

        for p in CROSSCHAT_DISCORD_BLOCK_PREFIXES:
            if content.startswith(p):
                return

        # Make it look like a normal in-game chat line: "Name: message"
        name = message.author.display_name
        clean = _clean_discord_text(content)
        line = f"{name}: {clean}"

        # push to all maps
        for m in CROSSCHAT_MAPS:
            await _send_serverchat(m, line)

    except Exception as e:
        print(f"[crosschat] on_discord_message error: {e}")


# =========================
# GAME -> DISCORD
# =========================

async def _post_to_discord(client: discord.Client, text: str):
    """
    Post a line into the crosschat Discord channel.
    """
    chan = client.get_channel(CROSSCHAT_CHANNEL_ID)
    if chan is None:
        try:
            chan = await client.fetch_channel(CROSSCHAT_CHANNEL_ID)
        except Exception:
            chan = None

    if chan is None:
        return

    # Keep it simple/plain so it feels like chat
    await chan.send(text)


async def _poll_map_once(client: discord.Client, map_name: str, seed_only: bool = False):
    """
    Poll GetChat for one map and relay new lines to Discord.
    """
    raw = await _rcon_call(map_name, "admincheat GetChat")
    if raw is None:
        return

    lines = _parse_getchat_output(str(raw))

    if not lines:
        return

    # Dedupe
    seen = _seen_by_map.setdefault(map_name, deque(maxlen=CROSSCHAT_DEDUPE_MAX))

    # We want oldest->newest so chat reads correctly
    new_lines: List[str] = []
    for ln in lines:
        # Filter to global only (if enabled)
        if CROSSCHAT_ONLY_GLOBAL and not _looks_global(ln):
            continue

        # Prevent echo loop from Discord -> game:
        # If you previously injected something, it may come back via GetChat.
        # We'll ignore any line that looks like it starts with your Discord authorship pattern if you want,
        # but safest is to ignore lines containing your optional prefix.
        # (We keep it minimal to avoid missing legit player messages.)
        key = _hash_line(ln)
        if key in seen:
            continue
        new_lines.append(ln)

    # If seeding, just remember them and don't post
    if seed_only:
        for ln in new_lines:
            seen.append(_hash_line(ln))
        return

    # Post + record
    for ln in new_lines:
        seen.append(_hash_line(ln))
        # Discord format you already like:
        # [Solunaris] Player: Message
        await _post_to_discord(client, f"[{map_name}] {ln}")


# =========================
# MAIN LOOP
# =========================

async def run_crosschat_loop(client: discord.Client, rcon_command: Optional[Callable] = None):
    """
    Start the GetChat polling loop.

    Call styles supported:
      - run_crosschat_loop(client, rcon_command)
      - set_rcon_command(...) then run_crosschat_loop(client)
    """
    if rcon_command is not None:
        set_rcon_command(rcon_command)

    if _RCON is None:
        print("[crosschat] ❌ No rcon_command bound. Crosschat disabled.")
        return

    await client.wait_until_ready()

    # Seed backlog once so redeploy doesn't spam
    if CROSSCHAT_SEED_BACKLOG_ON_START:
        try:
            for m in CROSSCHAT_MAPS:
                await _poll_map_once(client, m, seed_only=True)
            print("[crosschat] ✅ seeded backlog from GetChat (no redeploy spam).")
        except Exception as e:
            print(f"[crosschat] seed backlog error: {e}")

    print(f"[crosschat] ✅ running (channel_id={CROSSCHAT_CHANNEL_ID}, poll={CROSSCHAT_POLL_SECONDS}s, global_only={CROSSCHAT_ONLY_GLOBAL})")

    while True:
        try:
            for m in CROSSCHAT_MAPS:
                _last_poll_ts[m] = time.time()
                await _poll_map_once(client, m, seed_only=False)
        except Exception as e:
            print(f"[crosschat] loop error: {e}")

        await asyncio.sleep(CROSSCHAT_POLL_SECONDS)