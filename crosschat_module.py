import os
import re
import time
import asyncio
from collections import deque
from typing import Callable, Optional, Dict, Deque, List

import discord


# =========================
# ENV / CONFIG
# =========================

CROSSCHAT_CHANNEL_ID = int(os.getenv("CROSSCHAT_CHANNEL_ID", "1448575647285776444") or "1448575647285776444")
CROSSCHAT_POLL_SECONDS = float(os.getenv("CROSSCHAT_POLL_SECONDS", "5") or "5")

# Comma-separated map names
CROSSCHAT_MAPS = [m.strip() for m in os.getenv("CROSSCHAT_MAPS", "Solunaris").split(",") if m.strip()]

# Only allow "global" style messages
CROSSCHAT_ONLY_GLOBAL = os.getenv("CROSSCHAT_ONLY_GLOBAL", "1").strip().lower() not in ("0", "false", "no")

# Seed backlog once so redeploy doesn't spam
CROSSCHAT_SEED_BACKLOG_ON_START = os.getenv("CROSSCHAT_SEED_BACKLOG_ON_START", "1").strip().lower() not in ("0", "false", "no")

# Max dedupe memory per map
CROSSCHAT_DEDUPE_MAX = int(os.getenv("CROSSCHAT_DEDUPE_MAX", "800") or "800")

# Discord -> game: command mode
CROSSCHAT_USE_ADMINCHEAT_PREFIX = os.getenv("CROSSCHAT_USE_ADMINCHEAT_PREFIX", "0").strip().lower() in ("1", "true", "yes")

# Optional prefix inserted into the in-game message (keep minimal)
CROSSCHAT_INGAME_PREFIX = os.getenv("CROSSCHAT_INGAME_PREFIX", "").strip()

# ✅ Keep [Discord] tag (user wants this)
CROSSCHAT_DISCORD_TAG = os.getenv("CROSSCHAT_DISCORD_TAG", "[Discord]").strip() or "[Discord]"

# Optional block prefixes for Discord -> game
CROSSCHAT_DISCORD_BLOCK_PREFIXES = [p.strip() for p in os.getenv("CROSSCHAT_DISCORD_BLOCK_PREFIXES", "").split(",") if p.strip()]


# =========================
# INTERNAL STATE
# =========================

_RCON: Optional[Callable] = None
_seen_by_map: Dict[str, Deque[str]] = {m: deque(maxlen=CROSSCHAT_DEDUPE_MAX) for m in CROSSCHAT_MAPS}
_last_poll_ts: Dict[str, float] = {m: 0.0 for m in CROSSCHAT_MAPS}
_serverchat_mode: Optional[str] = None  # "plain" or "admincheat"


# =========================
# HELPERS
# =========================

def set_rcon_command(rcon_command: Callable):
    global _RCON
    _RCON = rcon_command


def _clean_discord_text(s: str) -> str:
    s = (s or "").replace("\n", " ").strip()
    s = s.replace("```", "").replace("`", "")
    s = re.sub(r"\s+", " ", s)
    return s


def _looks_non_global(line: str) -> bool:
    l = line.lower()
    if "[tribe]" in l or " tribe " in f" {l} " or "tribe:" in l:
        return True
    if "[alliance]" in l or " alliance " in f" {l} " or "alliance:" in l:
        return True
    if "[local]" in l or " local " in f" {l} " or "local:" in l:
        return True
    return False


def _looks_global(line: str) -> bool:
    l = line.lower()
    if "[global]" in l or "global:" in l or " global " in f" {l} ":
        return True
    return not _looks_non_global(line)


def _hash_line(line: str) -> str:
    return re.sub(r"\s+", " ", (line or "").strip())


def _parse_getchat_output(raw: str) -> List[str]:
    if not raw:
        return []
    return [ln.strip() for ln in str(raw).splitlines() if ln.strip()]


async def _rcon_call(map_name: str, command: str) -> Optional[str]:
    if _RCON is None:
        return None

    # Try multiple calling conventions (your project has varied)
    try:
        res = _RCON(command)
        return await res if asyncio.iscoroutine(res) else res
    except TypeError:
        pass

    try:
        res = _RCON(map_name, command)
        return await res if asyncio.iscoroutine(res) else res
    except TypeError:
        pass

    try:
        res = _RCON(command, map_name)
        return await res if asyncio.iscoroutine(res) else res
    except TypeError:
        pass

    try:
        res = _RCON(command=command, map_name=map_name)
        return await res if asyncio.iscoroutine(res) else res
    except Exception:
        return None


async def _send_serverchat(map_name: str, text: str) -> bool:
    """
    Note: ARK formats ServerChat as yellow server text. That styling cannot be changed via RCON.
    """
    global _serverchat_mode

    msg = (text or "").strip()
    if not msg:
        return False

    if CROSSCHAT_INGAME_PREFIX:
        msg = f"{CROSSCHAT_INGAME_PREFIX} {msg}".strip()

    if CROSSCHAT_USE_ADMINCHEAT_PREFIX:
        await _rcon_call(map_name, f"admincheat ServerChat {msg}")
        _serverchat_mode = "admincheat"
        return True

    if _serverchat_mode == "plain":
        await _rcon_call(map_name, f"ServerChat {msg}")
        return True
    if _serverchat_mode == "admincheat":
        await _rcon_call(map_name, f"admincheat ServerChat {msg}")
        return True

    r1 = await _rcon_call(map_name, f"ServerChat {msg}")
    if isinstance(r1, str) and ("unknown command" in r1.lower() or "not found" in r1.lower()):
        await _rcon_call(map_name, f"admincheat ServerChat {msg}")
        _serverchat_mode = "admincheat"
        return True

    _serverchat_mode = "plain"
    return True


# =========================
# DISCORD -> GAME
# =========================

async def on_discord_message(message: discord.Message):
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

        name = message.author.display_name
        clean = _clean_discord_text(content)

        # ✅ Put [Discord] back, and keep it tight/clean
        # This is as close as you can get via RCON while preserving the tag.
        line = f"{CROSSCHAT_DISCORD_TAG} {name}: {clean}".strip()

        for m in CROSSCHAT_MAPS:
            await _send_serverchat(m, line)

    except Exception as e:
        print(f"[crosschat] on_discord_message error: {e}")


# =========================
# GAME -> DISCORD
# =========================

async def _post_to_discord(client: discord.Client, text: str):
    chan = client.get_channel(CROSSCHAT_CHANNEL_ID)
    if chan is None:
        try:
            chan = await client.fetch_channel(CROSSCHAT_CHANNEL_ID)
        except Exception:
            chan = None
    if chan is None:
        return
    await chan.send(text)


async def _poll_map_once(client: discord.Client, map_name: str, seed_only: bool = False):
    raw = await _rcon_call(map_name, "admincheat GetChat")
    if raw is None:
        return

    lines = _parse_getchat_output(raw)
    if not lines:
        return

    seen = _seen_by_map.setdefault(map_name, deque(maxlen=CROSSCHAT_DEDUPE_MAX))

    new_lines: List[str] = []
    for ln in lines:
        if CROSSCHAT_ONLY_GLOBAL and not _looks_global(ln):
            continue

        key = _hash_line(ln)
        if key in seen:
            continue
        new_lines.append(ln)

    if seed_only:
        for ln in new_lines:
            seen.append(_hash_line(ln))
        return

    for ln in new_lines:
        seen.append(_hash_line(ln))
        await _post_to_discord(client, f"[{map_name}] {ln}")


# =========================
# MAIN LOOP
# =========================

async def run_crosschat_loop(client: discord.Client, rcon_command: Optional[Callable] = None):
    if rcon_command is not None:
        set_rcon_command(rcon_command)

    if _RCON is None:
        print("[crosschat] ❌ No rcon_command bound. Crosschat disabled.")
        return

    await client.wait_until_ready()

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