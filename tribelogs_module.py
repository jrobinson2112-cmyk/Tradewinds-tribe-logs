# tribelogs_module.py
# RCON -> GetGameLog -> forward tribe log lines to per-tribe Discord webhooks (forum threads supported)
# - /linktribelog, /unlinktribelog, /listroutes (admin only)
# - routes persisted to Railway volume so they survive redeploys
# - dedupe + first-run "seed" (no backlog spam)
# - optional heartbeat (only if no activity)
# - CLEAN log formatting: "Day X, HH:MM:SS - Who ... - What ..."
# - exposes get_latest_tribelog_time() for time_module syncing (uses tribe log timestamps)

import os
import json
import time
import asyncio
import aiohttp
import discord
from discord import app_commands
import re
from typing import Optional, Dict, Any, List, Tuple

# =====================
# ENV
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # only needed by main.py, not used here directly

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "0") or 0)
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

# Persist routes + dedupe info on Railway volume
TRIBELOGS_DATA_DIR = os.getenv("TRIBELOGS_DATA_DIR", "/data")
ROUTES_FILE = os.getenv("TRIBE_ROUTES_FILE", os.path.join(TRIBELOGS_DATA_DIR, "tribe_routes.json"))
DEDUPE_FILE = os.getenv("TRIBE_DEDUPE_FILE", os.path.join(TRIBELOGS_DATA_DIR, "tribe_dedupe.json"))

# Polling / heartbeat
POLL_SECONDS = float(os.getenv("TRIBELOG_POLL_SECONDS", "8"))
MAX_LINES_PER_POLL = int(os.getenv("TRIBELOG_MAX_LINES_PER_POLL", "25"))
HEARTBEAT_ENABLED = os.getenv("TRIBELOG_HEARTBEAT_ENABLED", "1").lower() in ("1", "true", "yes", "on")
HEARTBEAT_SECONDS = int(os.getenv("TRIBELOG_HEARTBEAT_SECONDS", "3600"))  # 60 mins default

# Formatting / colours
USE_COLORS = os.getenv("TRIBELOG_USE_COLORS", "1").lower() in ("1", "true", "yes", "on")

COLOR_RED = 0xE74C3C        # killed/died/destroyed
COLOR_YELLOW = 0xF1C40F     # demolished/unclaimed
COLOR_PURPLE = 0x9B59B6     # claimed
COLOR_GREEN = 0x2ECC71      # tamed
COLOR_LIGHTBLUE = 0x5DADE2  # alliance
COLOR_WHITE = 0xFFFFFF      # everything else

# =====================
# INTERNALS
# =====================
_routes: List[Dict[str, str]] = []
_routes_sig: str = ""
_routes_loaded_once = False
_routes_dirty = False

_dedupe: Dict[str, Dict[str, Any]] = {}  # per tribe: {"seen": {hash: ts}, "last_heartbeat": ts, "last_activity": ts}
_dedupe_dirty = False

_first_run_seeded = False

# Latest tribe-log Day/Time observed (for time_module)
_latest_daytime: Optional[Tuple[int, int, int, int]] = None  # (day, hour, minute, second)
_latest_daytime_ts: float = 0.0

# =====================
# FILE HELPERS
# =====================
def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _load_json(path: str, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path: str, obj):
    _ensure_dir(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# =====================
# ROUTES
# Each route:
#   {"tribe": "Valkyrie", "webhook": "https://discord.com/api/webhooks/..../....", "thread_id": "1459..."}
# =====================
def _normalize_webhook(url: str) -> str:
    # Remove any existing ?Thread=... or ?thread_id=... so we manage thread_id consistently
    # Keep base webhook URL only.
    if not url:
        return url
    # Discord webhooks accept query params; we want the base.
    return url.split("?", 1)[0].strip()

def _route_signature(routes: List[Dict[str, str]]) -> str:
    # stable signature so we can avoid spam printing
    try:
        return json.dumps(sorted(routes, key=lambda r: (r.get("tribe",""), r.get("webhook",""), r.get("thread_id",""))),
                          ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(routes)

def _load_routes() -> List[Dict[str, str]]:
    global _routes, _routes_sig, _routes_loaded_once
    _ensure_dir(ROUTES_FILE)
    data = _load_json(ROUTES_FILE, default=[])
    if not isinstance(data, list):
        data = []

    norm: List[Dict[str, str]] = []
    for r in data:
        if not isinstance(r, dict):
            continue
        tribe = str(r.get("tribe", "")).strip()
        webhook = _normalize_webhook(str(r.get("webhook", "")).strip())
        thread_id = str(r.get("thread_id", "")).strip()
        if tribe and webhook:
            norm.append({"tribe": tribe, "webhook": webhook, "thread_id": thread_id})

    _routes = norm
    _routes_sig = _route_signature(_routes)
    _routes_loaded_once = True

    # IMPORTANT: print only once on startup (fixes your spam)
    print("Tribe routes loaded:", [r["tribe"] for r in _routes])
    return _routes

def _save_routes():
    global _routes_dirty
    _ensure_dir(ROUTES_FILE)
    _save_json(ROUTES_FILE, _routes)
    _routes_dirty = False

def _maybe_reload_routes_quiet():
    """
    Reload routes only when the commands changed them (routes_dirty flag).
    Avoid spam prints in the poll loop.
    """
    global _routes, _routes_sig, _routes_dirty
    if not _routes_dirty:
        return

    data = _load_json(ROUTES_FILE, default=[])
    if not isinstance(data, list):
        data = []
    norm: List[Dict[str, str]] = []
    for r in data:
        if not isinstance(r, dict):
            continue
        tribe = str(r.get("tribe", "")).strip()
        webhook = _normalize_webhook(str(r.get("webhook", "")).strip())
        thread_id = str(r.get("thread_id", "")).strip()
        if tribe and webhook:
            norm.append({"tribe": tribe, "webhook": webhook, "thread_id": thread_id})

    new_sig = _route_signature(norm)
    _routes = norm
    if new_sig != _routes_sig:
        _routes_sig = new_sig
        print("Tribe routes updated:", [r["tribe"] for r in _routes])

    _routes_dirty = False

# =====================
# DEDUPE
# =====================
def _load_dedupe():
    global _dedupe
    _ensure_dir(DEDUPE_FILE)
    d = _load_json(DEDUPE_FILE, default={})
    if not isinstance(d, dict):
        d = {}
    # keep shape
    for tribe, obj in list(d.items()):
        if not isinstance(obj, dict):
            d.pop(tribe, None)
            continue
        if "seen" not in obj or not isinstance(obj["seen"], dict):
            obj["seen"] = {}
        if "last_heartbeat" not in obj:
            obj["last_heartbeat"] = 0.0
        if "last_activity" not in obj:
            obj["last_activity"] = 0.0
    _dedupe = d

def _save_dedupe():
    global _dedupe_dirty
    if not _dedupe_dirty:
        return
    _ensure_dir(DEDUPE_FILE)
    # Trim seen to keep file from growing forever
    now = time.time()
    for tribe, obj in _dedupe.items():
        seen = obj.get("seen", {})
        if not isinstance(seen, dict):
            obj["seen"] = {}
            continue
        # keep last 48h entries
        cutoff = now - 48 * 3600
        for k, ts in list(seen.items()):
            try:
                if float(ts) < cutoff:
                    seen.pop(k, None)
            except Exception:
                seen.pop(k, None)
        # hard cap size
        if len(seen) > 5000:
            # drop oldest
            items = sorted(seen.items(), key=lambda kv: float(kv[1]) if str(kv[1]).replace(".","",1).isdigit() else 0.0)
            for k, _ in items[: len(items) - 5000]:
                seen.pop(k, None)

    _save_json(DEDUPE_FILE, _dedupe)
    _dedupe_dirty = False

def _hash_line(s: str) -> str:
    # stable lightweight hash (no external libs)
    # includes full string; special chars preserved
    import hashlib
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

# =====================
# RCON (minimal Source-like)
# =====================
def _rcon_make_packet(req_id: int, ptype: int, body: str) -> bytes:
    data = body.encode("utf-8") + b"\x00"
    packet = (
        req_id.to_bytes(4, "little", signed=True)
        + ptype.to_bytes(4, "little", signed=True)
        + data
        + b"\x00"
    )
    size = len(packet)
    return size.to_bytes(4, "little", signed=True) + packet

async def rcon_command(command: str, timeout: float = 8.0) -> str:
    if not (RCON_HOST and RCON_PORT and RCON_PASSWORD):
        raise RuntimeError("RCON env vars missing (RCON_HOST/RCON_PORT/RCON_PASSWORD).")

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout
    )
    try:
        # auth
        writer.write(_rcon_make_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        _ = await asyncio.wait_for(reader.read(4096), timeout=timeout)

        # command
        writer.write(_rcon_make_packet(2, 2, command))
        await writer.drain()

        chunks = []
        end = time.time() + timeout
        while time.time() < end:
            try:
                part = await asyncio.wait_for(reader.read(4096), timeout=0.35)
            except asyncio.TimeoutError:
                break
            if not part:
                break
            chunks.append(part)

        if not chunks:
            return ""

        data = b"".join(chunks)
        out = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i:i+4], "little", signed=True)
            i += 4
            if i + size > len(data) or size < 10:
                break
            pkt = data[i:i+size]
            i += size
            body = pkt[8:-2]
            txt = body.decode("utf-8", errors="ignore")
            if txt:
                out.append(txt)

        return "".join(out).strip()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

# =====================
# PARSING / CLEANING
# Goal output:
#   "Day 294, 07:12:15 - Atropo claimed baby Megaraptor - Lvl 216 (Megaraptor)"
# We take messy RichColor markup and trim junk.
# =====================
_RICHCOLOR = re.compile(r"<\s*RichColor[^>]*>", re.IGNORECASE)
_TAGS = re.compile(r"</?\s*[^>]+>")  # generic tags
_MULTI_SPACE = re.compile(r"\s{2,}")
# Capture tribe log lines that include tribe name OR are clearly tribe events
# We still route per tribe by checking "Tribe <name>" substring in the original line.
_DAYTIME = re.compile(r"Day\s+(\d+),\s*(\d{1,2}):(\d{2}):(\d{2})")

def _strip_markup(s: str) -> str:
    s = _RICHCOLOR.sub("", s)
    s = _TAGS.sub("", s)
    s = s.replace("\u200b", "")
    s = _MULTI_SPACE.sub(" ", s)
    return s.strip()

def _extract_daytime(s: str) -> Optional[Tuple[int, int, int, int]]:
    m = _DAYTIME.search(s)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))

def _clean_to_desired_format(raw_line: str) -> Optional[str]:
    """
    Takes a raw GetGameLog line and returns the cleaned single-line message, or None if no Day/Time.
    """
    s = raw_line.strip()
    if not s:
        return None
    s = _strip_markup(s)

    dt = _extract_daytime(s)
    if not dt:
        return None

    day, hh, mm, ss = dt

    # Remove anything before the first "Day X, HH:MM:SS" to get the actual event message
    idx = s.lower().find(f"day {day},".lower())
    if idx > 0:
        s = s[idx:]

    # Many lines look like:
    # "Day 294, 07:12:15: Atropo ... "
    # or "... Day 294, 07:12:15: Tribe Valkyrie, ID ...: Atropo ..."
    # Normalize the first delimiter after time from ":" to " - "
    # Find the first ":" after the HH:MM:SS
    time_token = f"{hh:02d}:{mm:02d}:{ss:02d}"
    p = s.find(time_token)
    if p != -1:
        after = s[p + len(time_token):].lstrip()
        # If it starts with ":" remove it
        if after.startswith(":"):
            after = after[1:].lstrip()
        elif after.startswith("-"):
            after = after[1:].lstrip()

        # Some lines include an extra prefix like "Tribe X, ID 123:"
        # Remove leading "Tribe ...:" chunks but keep player/action
        # If it begins with "Tribe " then remove up to the next ":" once
        if after.lower().startswith("tribe "):
            colon = after.find(":")
            if colon != -1:
                after = after[colon+1:].lstrip()

        s = f"Day {day}, {time_token} - {after}".strip()

    # final tidy
    s = _MULTI_SPACE.sub(" ", s).strip(" -")
    return s if s else None

def _pick_color(clean_line: str) -> int:
    """
    Colour classification based on action keywords in the cleaned line.
    """
    if not USE_COLORS:
        return COLOR_WHITE

    t = clean_line.lower()
    # Red - Killed / Died / Death / Destroyed
    if any(k in t for k in (" killed ", " killed", "killed ", " died", " death", " destroyed", "was killed", "was destroyed")):
        return COLOR_RED
    # Yellow - Demolished / Unclaimed
    if " demolished" in t or "unclaimed" in t:
        return COLOR_YELLOW
    # Purple - Claimed
    if " claimed" in t:
        return COLOR_PURPLE
    # Green - Tamed
    if " tamed" in t:
        return COLOR_GREEN
    # Light blue - Alliance
    if " alliance" in t:
        return COLOR_LIGHTBLUE

    return COLOR_WHITE

# =====================
# WEBHOOK POST (forum thread supported)
# =====================
def _build_webhook_url(base: str, thread_id: str) -> str:
    base = base.strip()
    if not base:
        return base
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}wait=true"
    if thread_id:
        url += f"&thread_id={thread_id}"
    return url

async def _post_embed(session: aiohttp.ClientSession, webhook_base: str, thread_id: str, embed: Dict[str, Any]):
    url = _build_webhook_url(webhook_base, thread_id)
    async with session.post(url, json={"embeds": [embed]}) as r:
        # Discord commonly returns 200 or 204 depending; 2xx is fine
        if 200 <= r.status < 300:
            return True, None
        try:
            data = await r.json()
        except Exception:
            data = await r.text()
        return False, f"Webhook post failed: {r.status} {data}"

# =====================
# PUBLIC: time module hook
# =====================
def get_latest_tribelog_time() -> Optional[Tuple[int, int, int, int]]:
    """
    Returns the most recent (day, hour, minute, second) seen in tribe log lines.
    """
    return _latest_daytime

# =====================
# COMMANDS
# =====================
def setup_tribelog_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int):
    """
    Admin-only commands:
      /linktribelog tribe webhook thread_id
      /unlinktribelog tribe
      /listroutes
    """
    guild_obj = discord.Object(id=int(guild_id))

    def _is_admin(i: discord.Interaction) -> bool:
        try:
            return any(getattr(r, "id", None) == int(admin_role_id) for r in getattr(i.user, "roles", []))
        except Exception:
            return False

    @tree.command(name="linktribelog", guild=guild_obj)
    async def linktribelog(i: discord.Interaction, tribe: str, webhook: str, thread_id: str = ""):
        global _routes, _routes_dirty
        if not _is_admin(i):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        tribe = (tribe or "").strip()
        webhook = _normalize_webhook((webhook or "").strip())
        thread_id = (thread_id or "").strip()

        if not tribe or not webhook:
            await i.response.send_message("❌ Provide tribe + webhook.", ephemeral=True)
            return

        # load once if needed
        if not _routes_loaded_once:
            _load_routes()

        # upsert
        found = False
        for r in _routes:
            if r["tribe"].lower() == tribe.lower():
                r["tribe"] = tribe
                r["webhook"] = webhook
                r["thread_id"] = thread_id
                found = True
                break
        if not found:
            _routes.append({"tribe": tribe, "webhook": webhook, "thread_id": thread_id})

        _save_routes()
        _routes_dirty = True  # triggers quiet reload / updated print once

        await i.response.send_message(
            f"✅ Linked **{tribe}** → webhook (thread_id={thread_id or 'none'})",
            ephemeral=True
        )

    @tree.command(name="unlinktribelog", guild=guild_obj)
    async def unlinktribelog(i: discord.Interaction, tribe: str):
        global _routes, _routes_dirty
        if not _is_admin(i):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        tribe = (tribe or "").strip()
        if not tribe:
            await i.response.send_message("❌ Provide a tribe name.", ephemeral=True)
            return

        if not _routes_loaded_once:
            _load_routes()

        before = len(_routes)
        _routes = [r for r in _routes if r["tribe"].lower() != tribe.lower()]
        after = len(_routes)

        _save_routes()
        _routes_dirty = True

        if after < before:
            await i.response.send_message(f"✅ Unlinked **{tribe}**.", ephemeral=True)
        else:
            await i.response.send_message(f"ℹ️ No route found for **{tribe}**.", ephemeral=True)

    @tree.command(name="listroutes", guild=guild_obj)
    async def listroutes(i: discord.Interaction):
        if not _routes_loaded_once:
            _load_routes()

        if not _routes:
            await i.response.send_message("No tribe routes linked yet.", ephemeral=True)
            return

        lines = []
        for r in _routes:
            lines.append(f"- **{r['tribe']}** (thread_id={r.get('thread_id') or 'none'})")
        msg = "Current tribe routes:\n" + "\n".join(lines)
        await i.response.send_message(msg, ephemeral=True)

    print("[tribelogs_module] ✅ /linktribelog, /unlinktribelog, /listroutes registered")

# =====================
# LOOP
# =====================
async def run_tribelogs_loop(client: Optional[discord.Client] = None):
    """
    Polls GetGameLog and forwards new tribe log lines to each tribe's route.

    IMPORTANT behaviour:
    - First run: seeds dedupe with the *current* GetGameLog output (no backlog spam)
    - After that: only forwards new lines
    - Heartbeat: every HEARTBEAT_SECONDS but ONLY if no activity in that tribe since last heartbeat
    """
    global _first_run_seeded, _dedupe_dirty, _latest_daytime, _latest_daytime_ts

    if not _routes_loaded_once:
        _load_routes()

    _load_dedupe()

    async with aiohttp.ClientSession() as session:
        # First-run seed
        if not _first_run_seeded:
            try:
                text = await rcon_command("GetGameLog", timeout=12.0)
                now = time.time()
                # seed all tribes: mark all parsable lines as seen
                lines = [ln for ln in text.splitlines() if ln.strip()]
                for route in _routes:
                    tribe = route["tribe"]
                    obj = _dedupe.setdefault(tribe, {"seen": {}, "last_heartbeat": 0.0, "last_activity": 0.0})
                    seen = obj.setdefault("seen", {})
                    for ln in lines[-1000:]:
                        # only seed lines relevant to this tribe, to keep seen smaller
                        if f"tribe {tribe}".lower() in ln.lower():
                            h = _hash_line(ln)
                            seen[h] = now
                    obj.setdefault("last_activity", 0.0)
                    obj.setdefault("last_heartbeat", 0.0)
                _dedupe_dirty = True
                _save_dedupe()
                _first_run_seeded = True
                print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")
            except Exception as e:
                print(f"First run seed error: {e}")
                _first_run_seeded = True  # don't loop forever seeding

        # Main loop
        while True:
            try:
                _maybe_reload_routes_quiet()

                if not _routes:
                    await asyncio.sleep(max(2.0, POLL_SECONDS))
                    continue

                text = await rcon_command("GetGameLog", timeout=12.0)
                raw_lines = [ln for ln in text.splitlines() if ln.strip()]
                # Focus on tail to reduce reprocessing
                tail = raw_lines[-1200:] if len(raw_lines) > 1200 else raw_lines

                any_forwarded_this_cycle = False

                # For each route, scan for tribe lines, clean, dedupe, send
                for route in _routes:
                    tribe = route["tribe"]
                    webhook = route["webhook"]
                    thread_id = route.get("thread_id", "")

                    obj = _dedupe.setdefault(tribe, {"seen": {}, "last_heartbeat": 0.0, "last_activity": 0.0})
                    seen = obj.setdefault("seen", {})

                    new_msgs: List[Tuple[str, str]] = []  # (clean_line, raw_line)
                    for ln in tail:
                        # route match
                        if f"tribe {tribe}".lower() not in ln.lower():
                            continue

                        h = _hash_line(ln)
                        if h in seen:
                            continue

                        clean = _clean_to_desired_format(ln)
                        if not clean:
                            # still mark as seen to prevent repeat processing of junk
                            seen[h] = time.time()
                            _dedupe_dirty = True
                            continue

                        new_msgs.append((clean, ln))
                        seen[h] = time.time()
                        _dedupe_dirty = True

                    if new_msgs:
                        # only send up to MAX_LINES_PER_POLL to avoid bursts
                        new_msgs = new_msgs[-MAX_LINES_PER_POLL:]
                        for clean, _raw in new_msgs:
                            # update latest time for time_module (best-effort)
                            dt = _extract_daytime(clean)
                            if dt:
                                day, hh, mm, ss = dt
                                # accept if newer in wallclock or simply replace (we only need "recent")
                                _latest_daytime = (day, hh, mm, ss)
                                _latest_daytime_ts = time.time()

                            embed = {
                                "description": clean,
                                "color": _pick_color(clean),
                            }
                            ok, err = await _post_embed(session, webhook, thread_id, embed)
                            if not ok:
                                print(f"GetGameLog/forward error for {tribe}: {err}")
                                # don't break; keep going

                        obj["last_activity"] = time.time()
                        any_forwarded_this_cycle = True
                        any_forwarded_this_cycle = True

                    # Heartbeat (only if enabled AND no activity for HEARTBEAT_SECONDS)
                    if HEARTBEAT_ENABLED:
                        now = time.time()
                        last_act = float(obj.get("last_activity", 0.0) or 0.0)
                        last_hb = float(obj.get("last_heartbeat", 0.0) or 0.0)
                        # send heartbeat if it's been HEARTBEAT_SECONDS since last heartbeat AND no activity since last heartbeat
                        if (now - last_hb) >= HEARTBEAT_SECONDS and last_act <= last_hb:
                            hb_embed = {
                                "description": "Heartbeat: no new logs since last (still polling).",
                                "color": 0x95A5A6,
                            }
                            ok, err = await _post_embed(session, webhook, thread_id, hb_embed)
                            if ok:
                                obj["last_heartbeat"] = now
                                _dedupe_dirty = True
                            else:
                                print(f"Heartbeat error for {tribe}: {err}")

                if _dedupe_dirty:
                    _save_dedupe()

                await asyncio.sleep(max(1.0, POLL_SECONDS))

            except Exception as e:
                # Keep loop alive
                print(f"TribeLogs loop error: {e}")
                await asyncio.sleep(3)