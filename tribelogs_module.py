import os
import re
import json
import time
import asyncio
from typing import Dict, Any, List, Optional, Tuple
from collections import deque

import discord
from discord import app_commands
import aiohttp

# ============================================================
# ENV / CONFIG
# ============================================================

DISCORD_GUILD_ID_DEFAULT = int(os.getenv("GUILD_ID", "0") or "0")

RCON_HOST = os.getenv("RCON_HOST", "")
RCON_PORT = int(os.getenv("RCON_PORT", "0") or "0")
RCON_PASSWORD = os.getenv("RCON_PASSWORD", "")

TRIBELOG_POLL_SECONDS = float(os.getenv("TRIBELOG_POLL_SECONDS", "15") or "15")
ROUTES_PATH = os.getenv("TRIBE_ROUTES_PATH", "/data/tribe_routes.json")

# Heartbeat control (ONLY posts if idle)
HEARTBEAT_ENABLED = (os.getenv("HEARTBEAT_ENABLED", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
HEARTBEAT_IDLE_SECONDS = int(float(os.getenv("HEARTBEAT_IDLE_SECONDS", "3600") or "3600"))
HEARTBEAT_COLOR = int(os.getenv("HEARTBEAT_COLOR", "9807270") or "9807270")

# Dedupe window per tribe (how many recent lines we remember)
DEDUP_MAX = int(os.getenv("TRIBELOG_DEDUP_MAX", "2500") or "2500")

# When true, on boot we read current GetGameLog and seed dedupe so we don't spam backlog
SEED_DEDUPE_ON_START = (os.getenv("SEED_DEDUPE_ON_START", "1") or "1").strip().lower() not in ("0", "false", "no", "off")

# ============================================================
# RCON (prefer players_module's working implementation if present)
# ============================================================

_rcon_lock = asyncio.Lock()

async def rcon_command(cmd: str, timeout: float = 8.0) -> str:
    """
    Uses players_module.rcon_command if available (your project already had this working),
    otherwise falls back to a small internal implementation.

    Returns raw string response (may be empty).
    """
    try:
        # If your project already has a proven RCON function, reuse it.
        from players_module import rcon_command as _players_rcon
        return await _players_rcon(cmd)
    except Exception:
        pass

    # Fallback: very small async RCON using "valve/source RCON" style libs is messy without deps.
    # If you ever hit this fallback, you should rely on your existing players_module.rcon_command instead.
    raise RuntimeError("No working rcon_command found. Ensure players_module.rcon_command exists and works.")

# ============================================================
# ROUTES PERSISTENCE
# ============================================================

def _ensure_routes_dir():
    d = os.path.dirname(ROUTES_PATH)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def load_routes() -> List[Dict[str, str]]:
    _ensure_routes_dir()
    if not os.path.exists(ROUTES_PATH):
        return []
    try:
        with open(ROUTES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            # sanitize
            cleaned = []
            for r in data:
                if not isinstance(r, dict):
                    continue
                tribe = str(r.get("tribe", "")).strip()
                webhook = str(r.get("webhook", "")).strip()
                thread_id = str(r.get("thread_id", "")).strip()
                if tribe and webhook:
                    cleaned.append({"tribe": tribe, "webhook": webhook, "thread_id": thread_id})
            return cleaned
    except Exception:
        return []
    return []

def save_routes(routes: List[Dict[str, str]]) -> None:
    _ensure_routes_dir()
    tmp = ROUTES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(routes, f, indent=2, ensure_ascii=False)
    os.replace(tmp, ROUTES_PATH)

def upsert_route(tribe: str, webhook: str, thread_id: str = "") -> Dict[str, str]:
    tribe = tribe.strip()
    webhook = webhook.strip()
    thread_id = str(thread_id).strip()
    routes = load_routes()

    # overwrite if tribe exists (case-insensitive)
    replaced = False
    for r in routes:
        if r.get("tribe", "").lower() == tribe.lower():
            r["tribe"] = tribe
            r["webhook"] = webhook
            r["thread_id"] = thread_id
            replaced = True
            break

    if not replaced:
        routes.append({"tribe": tribe, "webhook": webhook, "thread_id": thread_id})

    save_routes(routes)
    return {"tribe": tribe, "webhook": webhook, "thread_id": thread_id}

def remove_route(tribe: str) -> bool:
    tribe = tribe.strip()
    routes = load_routes()
    before = len(routes)
    routes = [r for r in routes if r.get("tribe", "").lower() != tribe.lower()]
    save_routes(routes)
    return len(routes) != before

# ============================================================
# CLEANING / PARSING
# ============================================================

# Finds day/time ANYWHERE and captures the remainder message after the time punctuation
# Matches:
#   "... Day 294, 07:12:15: Atropo claimed ..."
#   "... Day 236, 21:49:58 - Your Baby ... was killed ..."
_DAYTIME_ANYWHERE_RE = re.compile(
    r"Day\s+(\d+)\s*,\s*(\d{1,2}):(\d{2}):(\d{2})\s*[:\-]\s*(.*)$",
    re.IGNORECASE
)

# Strip Nitrado timestamp prefixes like: 2026.01.16_04.23.47:
_NITRADO_TS_PREFIX_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}\s*:\s*")

# Remove <RichColor ...> and any <...> tags (non-greedy)
_TAG_RE = re.compile(r"<[^>]+>")

# Remove KillerSID blocks like [KillerSID: 240154768]
_KILLERSID_RE = re.compile(r"\[KillerSID\s*:\s*\d+\]", re.IGNORECASE)

# Remove trailing "(TribeName)!" on kill lines while keeping dino type "(Megaraptor)"
# e.g. "... (The Dwellers)!" -> removed
_TRAILING_TRIBE_BANG_RE = re.compile(r"\s*\(\s*[^()]{2,80}\s*\)!\s*$")

# Some lines have "Tribe X, ID 123456:" header
_TRIBE_HEADER_RE = re.compile(r"^\s*Tribe\s+.+?\bID\s+\d+\s*:\s*", re.IGNORECASE)

# normalize weird punctuation spacing
_WS_RE = re.compile(r"\s+")

def clean_tribelog_line(raw_line: str) -> Tuple[Optional[str], Optional[Tuple[int, int, int, int]]]:
    """
    Turns raw GetGameLog line into:
      "Day 294, 07:12:15 - Atropo claimed Baby Megaraptor - Lvl 216 (Megaraptor)"

    Returns (cleaned_line, (day, hh, mm, ss)) or (None, None) if no day/time found.
    """
    line = (raw_line or "").strip()
    if not line:
        return None, None

    # Some servers prefix the entire line with nitrado timestamp
    line = _NITRADO_TS_PREFIX_RE.sub("", line).strip()

    m = _DAYTIME_ANYWHERE_RE.search(line)
    if not m:
        return None, None

    day = int(m.group(1))
    hh = int(m.group(2))
    mm = int(m.group(3))
    ss = int(m.group(4))
    msg = (m.group(5) or "").strip()

    # msg itself can still contain another nitrado timestamp prefix
    msg = _NITRADO_TS_PREFIX_RE.sub("", msg).strip()

    # Strip richcolor/tags
    msg = _TAG_RE.sub("", msg).strip()

    # Strip "Tribe X, ID 123:" header if present
    msg = _TRIBE_HEADER_RE.sub("", msg).strip()

    # Strip killersid metadata
    msg = _KILLERSID_RE.sub("", msg).strip()

    # Remove any leftover double spaces/newlines
    msg = _WS_RE.sub(" ", msg).strip()

    # If message ends with "(SomeTribe)!" remove that (common on kill lines)
    msg = _TRAILING_TRIBE_BANG_RE.sub("", msg).strip()

    # If message ends with "!": remove it (keeps sentence cleaner)
    msg = msg[:-1].strip() if msg.endswith("!") else msg

    # Final whitespace normalize
    msg = _WS_RE.sub(" ", msg).strip()

    cleaned = f"Day {day}, {hh:02d}:{mm:02d}:{ss:02d} - {msg}"
    return cleaned, (day, hh, mm, ss)

def line_matches_tribe(raw_line: str, tribe_name: str) -> bool:
    """
    Route detection:
    - Matches "Tribe <name>" lines
    - Matches "(<name>)" occurrences on kill lines
    """
    t = tribe_name.strip()
    if not t:
        return False
    s = raw_line.lower()
    tl = t.lower()
    if f"tribe {tl}" in s:
        return True
    if f"({tl})" in s:
        return True
    return False
    # ============================================================
# DISCORD COMMANDS
# ============================================================

def _has_admin_role(interaction: discord.Interaction, admin_role_id: int) -> bool:
    if admin_role_id <= 0:
        return True
    try:
        if not interaction.user or not getattr(interaction.user, "roles", None):
            return False
        return any(getattr(r, "id", None) == admin_role_id for r in interaction.user.roles)
    except Exception:
        return False

def setup_tribelog_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int = 0):
    """
    Registers:
      /linktribelog webhook_url tribe_name forum_post_channel_id
      /unlinktribelog tribe_name
      /listroutes
    Restriction: only admin_role_id can use link/unlink.
    """

    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(name="linktribelog", description="Link a tribe name to a Discord webhook + forum post thread/channel id.", guild=guild_obj)
    @app_commands.describe(
        webhook_url="Discord webhook URL to post logs to",
        tribe_name="Exact tribe name (must match text in logs)",
        forum_post_channel_id="Forum post thread/channel id (used as thread_id for webhook posts)"
    )
    async def linktribelog(interaction: discord.Interaction, webhook_url: str, tribe_name: str, forum_post_channel_id: str):
        if not _has_admin_role(interaction, int(admin_role_id or 0)):
            await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
            return

        r = upsert_route(tribe_name, webhook_url, str(forum_post_channel_id))
        await interaction.response.send_message(f"✅ Linked tribe route: {r}", ephemeral=True)

    @tree.command(name="unlinktribelog", description="Remove a linked tribe route.", guild=guild_obj)
    @app_commands.describe(tribe_name="Tribe name to remove")
    async def unlinktribelog(interaction: discord.Interaction, tribe_name: str):
        if not _has_admin_role(interaction, int(admin_role_id or 0)):
            await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
            return

        ok = remove_route(tribe_name)
        await interaction.response.send_message("✅ Removed." if ok else "ℹ️ Tribe route not found.", ephemeral=True)

    @tree.command(name="listroutes", description="List all linked tribe routes.", guild=guild_obj)
    async def listroutes(interaction: discord.Interaction):
        routes = load_routes()
        if not routes:
            await interaction.response.send_message("ℹ️ No routes configured.", ephemeral=True)
            return
        names = ", ".join(sorted([r.get("tribe", "?") for r in routes]))
        await interaction.response.send_message(f"📌 Routes: {names}", ephemeral=True)

    print("[tribelogs_module] ✅ /linktribelog, /unlinktribelog, /listroutes registered")

# ============================================================
# WEBHOOK POSTING (per-tribe routes)
# ============================================================

def _split_webhook_base(url: str) -> str:
    """
    Ensure we post to the base webhook URL without accidental query params already attached.
    """
    if not url:
        return url
    return url.split("?", 1)[0].strip()

async def _send_webhook_embed(session: aiohttp.ClientSession, webhook_url: str, thread_id: str, embed: Dict[str, Any]):
    base = _split_webhook_base(webhook_url)

    params = {"wait": "true"}
    if thread_id and thread_id.strip() and thread_id.strip() != "0":
        params["thread_id"] = thread_id.strip()

    async with session.post(base, params=params, json={"embeds": [embed]}) as r:
        # We consider 2xx as success. If not, bubble a readable error.
        if r.status < 200 or r.status >= 300:
            try:
                data = await r.json()
            except Exception:
                data = await r.text()
            raise RuntimeError(f"{base}?wait=true&thread_id={thread_id} -> HTTP {r.status} {data}")

def _make_log_embed(clean_line: str) -> Dict[str, Any]:
    # neutral embed; you can adjust color later if you want
    return {"description": clean_line, "color": 0xFFFFFF}

def _make_heartbeat_embed() -> Dict[str, Any]:
    return {"description": "Heartbeat: no new logs since last (still polling).", "color": int(HEARTBEAT_COLOR)}

# ============================================================
# LOOP
# ============================================================

# Per-tribe dedupe memory: { tribe_lower: deque([...]) + set for fast membership }
_seen_deques: Dict[str, deque] = {}
_seen_sets: Dict[str, set] = {}

_last_any_log_ts: Dict[str, float] = {}       # per tribe
_last_heartbeat_ts: Dict[str, float] = {}     # per tribe

def _dedupe_has(tribe: str, cleaned_line: str) -> bool:
    k = tribe.lower()
    s = _seen_sets.setdefault(k, set())
    return cleaned_line in s

def _dedupe_add(tribe: str, cleaned_line: str) -> None:
    k = tribe.lower()
    dq = _seen_deques.setdefault(k, deque())
    s = _seen_sets.setdefault(k, set())
    if cleaned_line in s:
        return
    dq.append(cleaned_line)
    s.add(cleaned_line)
    while len(dq) > DEDUP_MAX:
        old = dq.popleft()
        s.discard(old)

async def _seed_dedupe_from_current():
    """
    Reads current GetGameLog once and seeds dedupe sets so we do not spam historical lines on boot.
    """
    routes = load_routes()
    if not routes:
        return
    try:
        raw = await rcon_command("GetGameLog")
    except Exception:
        return
    if not raw:
        return
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    for ln in lines:
        cleaned, _ = clean_tribelog_line(ln)
        if not cleaned:
            continue
        for r in routes:
            tribe = r.get("tribe", "")
            if tribe and line_matches_tribe(ln, tribe):
                _dedupe_add(tribe, cleaned)

async def run_tribelogs_loop():
    """
    Polls RCON GetGameLog, routes to configured tribe webhooks,
    cleans format, dedupes lines, and sends idle heartbeat only if no activity.
    """
    routes = load_routes()
    print("Tribe routes loaded:", [r.get("tribe") for r in routes])

    if SEED_DEDUPE_ON_START and routes:
        await _seed_dedupe_from_current()
        print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")

    async with aiohttp.ClientSession() as session:
        while True:
            routes = load_routes()  # reload so /linktribelog survives restarts + can change live
            now = time.time()

            try:
                raw = await rcon_command("GetGameLog")
            except Exception as e:
                # Don't crash; just try again
                print("GetGameLog error:", str(e))
                await asyncio.sleep(TRIBELOG_POLL_SECONDS)
                continue

            lines = [ln for ln in (raw or "").splitlines() if ln.strip()]

            posted_any = {r.get("tribe", "").lower(): False for r in routes if r.get("tribe")}

            for ln in lines:
                cleaned, _dt = clean_tribelog_line(ln)
                if not cleaned:
                    continue

                for r in routes:
                    tribe = (r.get("tribe") or "").strip()
                    if not tribe:
                        continue
                    if not line_matches_tribe(ln, tribe):
                        continue

                    # Dedup per tribe
                    if _dedupe_has(tribe, cleaned):
                        continue
                    _dedupe_add(tribe, cleaned)

                    # Post cleaned
                    try:
                        embed = _make_log_embed(cleaned)
                        await _send_webhook_embed(session, r.get("webhook", ""), r.get("thread_id", ""), embed)
                        posted_any[tribe.lower()] = True
                        _last_any_log_ts[tribe.lower()] = now
                    except Exception as e:
                        print(f"GetGameLog/forward error: {e}")

            # Heartbeat per tribe only if:
            # - enabled
            # - NO new logs since last
            # - idle for >= HEARTBEAT_IDLE_SECONDS
            if HEARTBEAT_ENABLED:
                for r in routes:
                    tribe = (r.get("tribe") or "").strip()
                    if not tribe:
                        continue
                    tl = tribe.lower()

                    if posted_any.get(tl):
                        # we had activity this cycle; don't heartbeat
                        continue

                    last_log = _last_any_log_ts.get(tl, 0.0)
                    last_hb = _last_heartbeat_ts.get(tl, 0.0)

                    # Only heartbeat if we've been idle long enough AND we haven't just heartbeated recently.
                    if last_log and (now - last_log) < HEARTBEAT_IDLE_SECONDS:
                        continue
                    if last_hb and (now - last_hb) < HEARTBEAT_IDLE_SECONDS:
                        continue

                    # If we've never seen logs for this tribe yet, don't spam heartbeat immediately.
                    # Require at least one cycle of "no activity" time window:
                    if last_log == 0.0 and last_hb == 0.0:
                        continue

                    try:
                        hb = _make_heartbeat_embed()
                        await _send_webhook_embed(session, r.get("webhook", ""), r.get("thread_id", ""), hb)
                        _last_heartbeat_ts[tl] = now
                        print(f"Heartbeat sent for {tribe}")
                    except Exception as e:
                        print(f"Heartbeat error for {tribe}: {e}")

            await asyncio.sleep(TRIBELOG_POLL_SECONDS)