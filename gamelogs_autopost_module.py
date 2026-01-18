import os
import time
import asyncio
import hashlib
from collections import deque
from typing import Deque, Tuple, Optional, List

import discord

# =====================
# ENV / CONFIG
# =====================
GAMELOGS_CHANNEL_ID = int(os.getenv("GAMELOGS_CHANNEL_ID", "1462433999766028427"))
GAMELOGS_POLL_SECONDS = float(os.getenv("GAMELOGS_POLL_SECONDS", "10"))
GAMELOGS_EMBED_UPDATE_SECONDS = float(os.getenv("GAMELOGS_EMBED_UPDATE_SECONDS", "60"))

# Dedupe sizing
GAMELOGS_DEDUPE_MAX = int(os.getenv("GAMELOGS_DEDUPE_MAX", "20000"))

# How many minutes of lines to keep in memory
GAMELOGS_RETENTION_MINUTES = int(os.getenv("GAMELOGS_RETENTION_MINUTES", "10"))

# Embed look
GAMELOGS_EMBED_TITLE = os.getenv("GAMELOGS_EMBED_TITLE", "📜 Game Logs (live)")
GAMELOGS_EMBED_COLOR = int(os.getenv("GAMELOGS_EMBED_COLOR", "0x2F3136"), 16)

# Discord embed description hard limit is 4096 chars. Leave headroom.
_EMBED_DESC_LIMIT = 3900

# =====================
# INTERNAL STATE
# =====================
# store (seen_ts, line)
_buffer: Deque[Tuple[float, str]] = deque()

_seen_hashes: Deque[str] = deque()
_seen_set = set()

_running = False


def _hash_line(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()


def _remember_hash(h: str):
    if h in _seen_set:
        return
    _seen_hashes.append(h)
    _seen_set.add(h)
    while len(_seen_hashes) > GAMELOGS_DEDUPE_MAX:
        old = _seen_hashes.popleft()
        _seen_set.discard(old)


def _clean_line(line: str) -> str:
    # Keep special chars; normalize whitespace
    return " ".join(line.strip().split())


def _trim_buffer():
    cutoff = time.time() - (GAMELOGS_RETENTION_MINUTES * 60)
    while _buffer and _buffer[0][0] < cutoff:
        _buffer.popleft()


async def _seed_no_backlog(rcon_command):
    """
    Seed dedupe from current GetGameLog so redeploy doesn't spam old lines.
    We DO NOT add seeded lines to buffer.
    """
    try:
        txt = await rcon_command("GetGameLog", timeout=15.0)
        if not txt:
            print("[gamelogs_autopost] seed: empty GetGameLog")
            return
        for ln in txt.splitlines():
            ln = _clean_line(ln)
            if not ln:
                continue
            _remember_hash(_hash_line(ln))
        print("[gamelogs_autopost] ✅ seeded from current GetGameLog (no redeploy backlog spam).")
    except Exception as e:
        print(f"[gamelogs_autopost] seed error: {e}")


def _lines_since(seconds: float) -> List[str]:
    cutoff = time.time() - seconds
    return [ln for (ts, ln) in list(_buffer) if ts >= cutoff]


def _build_embed(lines: List[str]) -> discord.Embed:
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    if not lines:
        desc = "*No new logs in the last minute.*"
    else:
        # Fit into embed limit, keep chronological order
        out = []
        total = 0
        for ln in lines:
            ln = _clean_line(ln)
            if not ln:
                continue
            add_len = len(ln) + 1
            if total + add_len > _EMBED_DESC_LIMIT:
                break
            out.append(ln)
            total += add_len
        desc = "\n".join(out) if out else "*Logs too long to display (window exceeds embed limit).*"

    embed = discord.Embed(
        title=GAMELOGS_EMBED_TITLE,
        description=desc,
        color=GAMELOGS_EMBED_COLOR,
    )
    embed.set_footer(text=f"Last update: {now_str} | poll={GAMELOGS_POLL_SECONDS}s")
    return embed


async def run_gamelogs_autopost_loop(client: discord.Client, rcon_command):
    """
    Poll GetGameLog every GAMELOGS_POLL_SECONDS.
    Edit/update a single embed message every GAMELOGS_EMBED_UPDATE_SECONDS in channel GAMELOGS_CHANNEL_ID.
    """
    global _running
    if _running:
        return
    _running = True

    await client.wait_until_ready()

    ch = client.get_channel(GAMELOGS_CHANNEL_ID)
    if ch is None:
        print(f"[gamelogs_autopost] ❌ Channel not found: {GAMELOGS_CHANNEL_ID}")
        return

    await _seed_no_backlog(rcon_command)

    last_embed_update = 0.0
    embed_message: Optional[discord.Message] = None

    print(
        f"[gamelogs_autopost] ✅ running (channel={GAMELOGS_CHANNEL_ID}, poll={GAMELOGS_POLL_SECONDS}s, embed_update={GAMELOGS_EMBED_UPDATE_SECONDS}s)"
    )

    while True:
        try:
            _trim_buffer()

            # ---- poll ----
            txt = await rcon_command("GetGameLog", timeout=15.0)
            if txt:
                now = time.time()
                raw_lines = [ln for ln in txt.splitlines() if ln.strip()]

                new_lines = []
                for ln in reversed(raw_lines):
                    ln = _clean_line(ln)
                    if not ln:
                        continue
                    h = _hash_line(ln)
                    if h in _seen_set:
                        continue
                    _remember_hash(h)
                    new_lines.append(ln)

                # append in chronological order (oldest first)
                for ln in reversed(new_lines):
                    _buffer.append((now, ln))

            # ---- embed update every N seconds ----
            if (time.time() - last_embed_update) >= GAMELOGS_EMBED_UPDATE_SECONDS:
                lines = _lines_since(GAMELOGS_EMBED_UPDATE_SECONDS)
                embed_obj = _build_embed(lines)

                if embed_message is None:
                    embed_message = await ch.send(embed=embed_obj)
                else:
                    try:
                        await embed_message.edit(embed=embed_obj)
                    except discord.NotFound:
                        embed_message = await ch.send(embed=embed_obj)

                last_embed_update = time.time()

        except Exception as e:
            print(f"[gamelogs_autopost] loop error: {e}")

        await asyncio.sleep(GAMELOGS_POLL_SECONDS)