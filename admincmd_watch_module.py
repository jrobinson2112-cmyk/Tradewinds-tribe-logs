# admincmd_watch_module.py
# Watches ASA RCON GetGameLog for in-game admin commands (AdminCmd:)
# and posts a Discord embed each time one is seen.

import os
import time
import json
import asyncio
import hashlib
import re
from typing import List, Optional, Dict, Set

import discord

# =====================
# CONFIG (ENV)
# =====================
ADMINCMD_CHANNEL_ID = int(os.getenv("ADMINCMD_CHANNEL_ID", "0"))  # set this!
ADMINCMD_POLL_SECONDS = float(os.getenv("ADMINCMD_POLL_SECONDS", "10"))
ADMINCMD_SEED_ON_START = os.getenv("ADMINCMD_SEED_ON_START", "1").lower() in ("1", "true", "yes", "on")
ADMINCMD_SHOW_DEBUG = os.getenv("ADMINCMD_SHOW_DEBUG", "0").lower() in ("1", "true", "yes", "on")

# Persist dedupe so restarts don't repost old commands
DATA_DIR = os.getenv("ADMINCMD_DATA_DIR", "/data")
STATE_FILE = os.getenv("ADMINCMD_STATE_FILE", os.path.join(DATA_DIR, "admincmd_state.json"))

EMBED_COLOR = 0xED4245  # red-ish

# =====================
# STATE
# =====================
_seen_hashes: Set[str] = set()

# =====================
# REGEX
# =====================
# Matches any line containing "AdminCmd:" and captures the remainder for display.
ADMINCMD_RE = re.compile(r"AdminCmd:\s*(?P<cmd>.+)$", re.IGNORECASE)

# Optional: capture a timestamp prefix like 2026.01.14_22.50.19 or similar.
TIMESTAMP_RE = re.compile(r"(?P<ts>\d{4}\.\d{2}\.\d{2}[_-]\d{2}\.\d{2}\.\d{2})")

# Optional: pull PlayerName if present (your screenshots show this)
PLAYER_RE = re.compile(r"PlayerName:\s*(?P<player>[^,]+)", re.IGNORECASE)


# =====================
# HELPERS
# =====================
def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def _h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()


def _split_lines(text: str) -> List[str]:
    if not text:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _load_state():
    global _seen_hashes
    try:
        if not os.path.exists(STATE_FILE):
            _seen_hashes = set()
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("seen"), list):
            _seen_hashes = set(str(x) for x in data["seen"][-20000:])
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


def _parse_admincmd_line(line: str) -> Optional[Dict[str, str]]:
    """
    Returns dict with keys:
      - cmd: the admin command string
      - player: optional
      - ts: optional
      - raw: original line
    """
    m = ADMINCMD_RE.search(line)
    if not m:
        return None

    cmd = m.group("cmd").strip()

    # Try to pull player and timestamp if present
    player = None
    pm = PLAYER_RE.search(line)
    if pm:
        player = pm.group("player").strip()

    ts = None
    tm = TIMESTAMP_RE.search(line)
    if tm:
        ts = tm.group("ts")

    return {"cmd": cmd, "player": player or "", "ts": ts or "", "raw": line}


async def _post_admincmd_embed(client: discord.Client, parsed: Dict[str, str]):
    if ADMINCMD_CHANNEL_ID == 0:
        if ADMINCMD_SHOW_DEBUG:
            print("[admincmd_watch] ADMINCMD_CHANNEL_ID not set.")
        return

    ch = client.get_channel(ADMINCMD_CHANNEL_ID)
    if ch is None:
        try:
            ch = await client.fetch_channel(ADMINCMD_CHANNEL_ID)
        except Exception:
            ch = None

    if ch is None:
        if ADMINCMD_SHOW_DEBUG:
            print("[admincmd_watch] ❌ channel not found:", ADMINCMD_CHANNEL_ID)
        return

    title = "🛡️ In-Game Admin Command Used"
    embed = discord.Embed(title=title, color=EMBED_COLOR)

    # Top line / context
    if parsed.get("player"):
        embed.add_field(name="Player", value=parsed["player"], inline=True)
    if parsed.get("ts"):
        embed.add_field(name="Log Time", value=parsed["ts"], inline=True)

    # Command (keep readable)
    cmd = parsed.get("cmd", "")
    if len(cmd) > 1024:
        cmd = cmd[:1020] + "…"
    embed.add_field(name="Command", value=f"```{cmd}```", inline=False)

    # Raw line (optional but useful)
    raw = parsed.get("raw", "")
    if raw:
        # Discord field value max is 1024, so trim
        if len(raw) > 1024:
            raw = raw[:1020] + "…"
        embed.add_field(name="Raw", value=f"```{raw}```", inline=False)

    embed.set_footer(text=f"Detected: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}")

    try:
        await ch.send(embed=embed)
    except Exception as e:
        if ADMINCMD_SHOW_DEBUG:
            print("[admincmd_watch] send error:", e)


# =====================
# PUBLIC LOOP
# =====================
async def run_admincmd_watch_loop(client: discord.Client, rcon_command):
    """
    rcon_command must be awaitable like: await rcon_command("GetGameLog", timeout=10.0)
    """
    if rcon_command is None:
        print("[admincmd_watch] ❌ rcon_command is None (not wired).")
        return

    _ensure_dir(STATE_FILE)
    _load_state()

    # Seed backlog so we don't spam on deploy
    if ADMINCMD_SEED_ON_START:
        try:
            text = await rcon_command("GetGameLog", timeout=12.0)
            lines = _split_lines(text)
            tail = lines[-2000:] if len(lines) > 2000 else lines
            for ln in tail:
                if ADMINCMD_RE.search(ln):
                    _seen_hashes.add(_h(ln))
            _save_state()
            print("[admincmd_watch] ✅ seeded admincmd backlog (no deploy spam).")
        except Exception as e:
            print("[admincmd_watch] seed error:", e)

    print(f"[admincmd_watch] ✅ running (channel_id={ADMINCMD_CHANNEL_ID}, poll={ADMINCMD_POLL_SECONDS:.1f}s)")
    last_state_save = time.time()

    while True:
        try:
            text = await rcon_command("GetGameLog", timeout=12.0)
            lines = _split_lines(text)
            tail = lines[-2000:] if len(lines) > 2000 else lines

            new_posts = 0
            for ln in tail:
                if not ADMINCMD_RE.search(ln):
                    continue

                hh = _h(ln)
                if hh in _seen_hashes:
                    continue

                _seen_hashes.add(hh)

                parsed = _parse_admincmd_line(ln)
                if parsed:
                    await _post_admincmd_embed(client, parsed)
                    new_posts += 1

            # save state ~every 30s
            if time.time() - last_state_save >= 30:
                _save_state()
                last_state_save = time.time()

            if ADMINCMD_SHOW_DEBUG and new_posts:
                print(f"[admincmd_watch] posted {new_posts} admincmd events")

            await asyncio.sleep(max(1.0, ADMINCMD_POLL_SECONDS))

        except Exception as e:
            print(f"[admincmd_watch] loop error: {e}")
            await asyncio.sleep(3)