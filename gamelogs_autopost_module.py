# gamelogs_autopost_module.py
# Polls ASA RCON "GetGameLog" every N seconds, and POSTS (not edits) a new embed every 60s
# containing ONLY the new log lines seen during that minute.
#
# ✅ No redeploy spam: on startup it "seeds" dedupe from current GetGameLog output
# ✅ Posts a NEW embed only when there ARE new logs in that minute
# ✅ Does NOT delete or edit previous embeds
# ✅ Also feeds GetGameLog text to time_module.ingest_gamelog_text(text) (for accurate time sync)
#
# Env vars:
#   GAMELOGS_CHANNEL_ID=1462433999766028427
#   GAMELOGS_POLL_SECONDS=10
#   GAMELOGS_POST_EVERY_SECONDS=60
#   GAMELOGS_MAX_LINES_PER_EMBED=40
#   GAMELOGS_SEED_ON_START=1
#   GAMELOGS_SHOW_DEBUG=0
#
# Optional:
#   GAMELOGS_DATA_DIR=/data
#   GAMELOGS_STATE_FILE=/data/gamelogs_state.json

import os
import time
import json
import asyncio
import hashlib
from typing import List, Set

import discord

# IMPORTANT: feed log text to time module cache for auto-sync
import time_module

# =====================
# CONFIG (ENV)
# =====================
GAMELOGS_CHANNEL_ID = int(os.getenv("GAMELOGS_CHANNEL_ID", "1462433999766028427"))
POLL_SECONDS = float(os.getenv("GAMELOGS_POLL_SECONDS", "10"))
POST_EVERY_SECONDS = int(os.getenv("GAMELOGS_POST_EVERY_SECONDS", "60"))
MAX_LINES_PER_EMBED = int(os.getenv("GAMELOGS_MAX_LINES_PER_EMBED", "40"))
SEED_ON_START = os.getenv("GAMELOGS_SEED_ON_START", "1").lower() in ("1", "true", "yes", "on")
SHOW_DEBUG = os.getenv("GAMELOGS_SHOW_DEBUG", "0").lower() in ("1", "true", "yes", "on")

# Persist dedupe so restarts don't replay the whole world
DATA_DIR = os.getenv("GAMELOGS_DATA_DIR", "/data")
STATE_FILE = os.getenv("GAMELOGS_STATE_FILE", os.path.join(DATA_DIR, "gamelogs_state.json"))

EMBED_COLOR = 0x2F3136  # dark-ish

# =====================
# STATE
# =====================
_seen_hashes: Set[str] = set()
_buffer: List[str] = []
_last_post_ts: float = 0.0

# =====================
# HELPERS
# =====================
def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _load_state():
    global _seen_hashes
    try:
        if not os.path.exists(STATE_FILE):
            _seen_hashes = set()
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("seen"), list):
            _seen_hashes = set(str(x) for x in data["seen"][-20000:])  # cap memory
        else:
            _seen_hashes = set()
    except Exception:
        _seen_hashes = set()

def _save_state():
    try:
        _ensure_dir(STATE_FILE)
        seen_list = list(_seen_hashes)
        if len(seen_list) > 20000:
            seen_list = seen_list[-20000:]
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"seen": seen_list}, f)
    except Exception:
        pass

def _h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def _split_lines(text: str) -> List[str]:
    if not text:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip()]

def _truncate_for_embed(lines: List[str]) -> str:
    """
    Discord embed description limit = 4096 chars.
    We'll join lines and truncate safely.
    """
    joined = "\n".join(lines)
    if len(joined) <= 3900:
        return joined
    return joined[:3890] + "\n… (truncated)"

async def _post_minute_embed(client: discord.Client, lines: List[str]):
    """
    Posts a NEW embed containing the new log lines for the minute window.
    Caller should ensure lines is non-empty.
    """
    if not lines:
        return  # safety

    ch = client.get_channel(GAMELOGS_CHANNEL_ID)
    if ch is None:
        try:
            ch = await client.fetch_channel(GAMELOGS_CHANNEL_ID)
        except Exception:
            ch = None

    if ch is None:
        if SHOW_DEBUG:
            print("[gamelogs_autopost] ❌ channel not found:", GAMELOGS_CHANNEL_ID)
        return

    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    trimmed = lines[-MAX_LINES_PER_EMBED:]
    desc = _truncate_for_embed(trimmed)

    embed = discord.Embed(
        title="📜 Game Logs (minute)",
        description=desc,
        color=EMBED_COLOR,
    )
    embed.set_footer(text=f"Posted: {now_str} | poll={POLL_SECONDS:.1f}s")

    try:
        await ch.send(embed=embed)
    except Exception as e:
        if SHOW_DEBUG:
            print("[gamelogs_autopost] send error:", e)
            
# =====================
# PUBLIC LOOP
# =====================
async def run_gamelogs_autopost_loop(client: discord.Client, rcon_command):
    """
    rcon_command must be awaitable like: await rcon_command("GetGameLog", timeout=10.0)
    """
    global _buffer, _last_post_ts

    if rcon_command is None:
        print("[gamelogs_autopost] ❌ rcon_command is None (not wired).")
        return

    _ensure_dir(STATE_FILE)
    _load_state()

    # Seed (no redeploy spam): mark existing lines as seen, but DO NOT post them
    if SEED_ON_START:
        try:
            text = await rcon_command("GetGameLog", timeout=12.0)

            # Feed cache for time sync
            try:
                time_module.ingest_gamelog_text(text)
            except Exception:
                pass

            lines = _split_lines(text)
            for ln in lines[-2000:]:  # only tail
                _seen_hashes.add(_h(ln))
            _save_state()
            print("[gamelogs_autopost] ✅ seeded backlog from GetGameLog (no redeploy spam).")
        except Exception as e:
            print("[gamelogs_autopost] seed error:", e)

    _last_post_ts = time.time()
    print(
        f"[gamelogs_autopost] ✅ running "
        f"(channel_id={GAMELOGS_CHANNEL_ID}, poll={POLL_SECONDS:.1f}s, post_every={POST_EVERY_SECONDS}s)"
    )

    last_state_save = time.time()

    while True:
        try:
            # Poll GetGameLog
            text = await rcon_command("GetGameLog", timeout=12.0)

            # Feed cache for time_module auto-sync (critical)
            try:
                time_module.ingest_gamelog_text(text)
            except Exception:
                pass

            lines = _split_lines(text)

            # Only process the tail to keep things fast
            tail = lines[-2000:] if len(lines) > 2000 else lines

            new_count = 0
            for ln in tail:
                hh = _h(ln)
                if hh in _seen_hashes:
                    continue
                _seen_hashes.add(hh)
                _buffer.append(ln)
                new_count += 1

            # Periodic save of dedupe set (every ~30s)
            if time.time() - last_state_save >= 30:
                _save_state()
                last_state_save = time.time()

            # Post every minute as a NEW embed ONLY if there were new logs in that minute
            if time.time() - _last_post_ts >= POST_EVERY_SECONDS:
                if _buffer:
                    await _post_minute_embed(client, _buffer)
                else:
                    if SHOW_DEBUG:
                        print("[gamelogs_autopost] (minute) no new logs -> not posting")

                _buffer = []
                _last_post_ts = time.time()

            if SHOW_DEBUG and new_count:
                print(f"[gamelogs_autopost] +{new_count} new lines buffered")

            await asyncio.sleep(max(1.0, POLL_SECONDS))

        except Exception as e:
            print(f"[gamelogs_autopost] loop error: {e}")
            await asyncio.sleep(3)