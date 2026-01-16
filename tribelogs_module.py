import os
import re
import json
import time
import hashlib
import asyncio
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple

import aiohttp
import discord
from discord import app_commands

# ============================================================
# ENV / CONFIG
# ============================================================

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = os.getenv("RCON_PORT")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

# Poll interval for GetGameLog
TRIBELOGS_POLL_SECONDS = float(os.getenv("TRIBELOGS_POLL_SECONDS", "10"))

# Heartbeat: only sent if no activity since last heartbeat window
HEARTBEAT_SECONDS = int(os.getenv("TRIBELOGS_HEARTBEAT_SECONDS", "3600"))  # 60 mins default
HEARTBEAT_ENABLED = os.getenv("TRIBELOGS_HEARTBEAT_ENABLED", "true").lower() in ("1", "true", "yes", "y")

# Persist routes on Railway Volume (recommended)
DATA_DIR = os.getenv("TRIBELOGS_DATA_DIR", "/data")
ROUTES_FILE = os.getenv("TRIBELOGS_ROUTES_FILE", os.path.join(DATA_DIR, "tribelog_routes.json"))

# File that time_module can read to sync from tribe log timestamps
TIMEHINT_FILE = os.getenv("TRIBELOGS_TIMEHINT_FILE", os.path.join(DATA_DIR, "tribelog_latest_time.json"))

# Dedupe tuning (keep last N hashes in memory per route)
DEDUPE_MAX = int(os.getenv("TRIBELOGS_DEDUPE_MAX", "3000"))

# Admin role (passed in setup_tribelog_commands, but we keep a fallback)
DEFAULT_ADMIN_ROLE_ID = int(os.getenv("TRIBELOGS_ADMIN_ROLE_ID", "0") or "0")

# ============================================================
# VALIDATION
# ============================================================

def _require_env():
    missing = []
    if not RCON_HOST:
        missing.append("RCON_HOST")
    if not RCON_PORT:
        missing.append("RCON_PORT")
    if not RCON_PASSWORD:
        missing.append("RCON_PASSWORD")
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

RCON_PORT = int(RCON_PORT or 0)

# ============================================================
# ROUTES MODEL
# ============================================================

@dataclass
class TribeRoute:
    tribe: str
    webhook: str
    thread_id: Optional[str] = None

def _ensure_data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass

def _load_routes() -> List[TribeRoute]:
    _ensure_data_dir()
    if not os.path.exists(ROUTES_FILE):
        return []
    try:
        with open(ROUTES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        routes: List[TribeRoute] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                tribe = str(item.get("tribe", "")).strip()
                webhook = str(item.get("webhook", "")).strip()
                thread_id = item.get("thread_id")
                thread_id = str(thread_id).strip() if thread_id else None
                if tribe and webhook:
                    routes.append(TribeRoute(tribe=tribe, webhook=webhook, thread_id=thread_id))
        return routes
    except Exception:
        return []

def _save_routes(routes: List[TribeRoute]) -> None:
    _ensure_data_dir()
    data = [{"tribe": r.tribe, "webhook": r.webhook, "thread_id": r.thread_id} for r in routes]
    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# In-memory routes (reloadable)
_ROUTES: List[TribeRoute] = []

def _reload_routes():
    global _ROUTES
    _ROUTES = _load_routes()
    print("Tribe routes loaded:", [r.tribe for r in _ROUTES])

# ============================================================
# RCON (Minimal Source RCON)  — works for ASA/Nitrado
# ============================================================

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
    reader, writer = await asyncio.wait_for(asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout)
    try:
        # auth
        writer.write(_rcon_make_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        if len(raw) < 12:
            raise RuntimeError("RCON auth failed (short response)")

        # exec
        writer.write(_rcon_make_packet(2, 2, command))
        await writer.drain()

        chunks = []
        end_time = time.time() + timeout
        while time.time() < end_time:
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

        # parse packets
        out = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i:i+4], "little", signed=True)
            i += 4
            if i + size > len(data) or size < 10:
                break
            pkt = data[i:i+size]
            i += size
            body = pkt[8:-2]  # skip id+type, strip \x00\x00
            txt = body.decode("utf-8", errors="replace")
            if txt:
                out.append(txt)

        return "".join(out).strip()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

# ============================================================
# PARSING / CLEANING
# ============================================================

# Example lines contain:
# "... Tribe Valkyrie, ID 123...: Day 216, 18:13:36: Einar froze ..."
_DAYTIME_RE = re.compile(r"Day\s+(\d+),\s+(\d{1,2}):(\d{2}):(\d{2})\s*:", re.IGNORECASE)

# Strip RichColor wrappers and other tag-like stuff
_RICH_TAG_RE = re.compile(r"<[^>]+>")

def _strip_rich_tags(s: str) -> str:
    # remove <RichColor ...> etc
    s = _RICH_TAG_RE.sub("", s)
    return s

def _clean_trailing_garbage(s: str) -> str:
    # remove weird end fragments like "</>)" or "!>)" or "))"
    s = s.strip()
    while s.endswith(("</>)", "</)", "!>)", ">)", "))", ")")) and len(s) > 3:
        s = s[:-1].strip()
    return s

def _normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def _extract_day_time_and_message(line: str) -> Optional[Tuple[int, int, int, int, str]]:
    """
    Returns: (day, hh, mm, ss, message_after_time_colon)
    where message_after_time_colon is the part AFTER 'Day X, HH:MM:SS:'.
    """
    m = _DAYTIME_RE.search(line)
    if not m:
        return None
    day = int(m.group(1))
    hh = int(m.group(2))
    mm = int(m.group(3))
    ss = int(m.group(4))

    # text after the matched "Day..., HH:MM:SS:"
    after = line[m.end():].strip()

    # Remove leading punctuation/extra
    after = after.lstrip(":").strip()
    after = _strip_rich_tags(after)
    after = _clean_trailing_garbage(after)
    after = _normalize_spaces(after)

    return day, hh, mm, ss, after

def _extract_player_and_action(after: str) -> str:
    """
    We want: "Who + action" only (keep what ARK gives, but remove extra bracketed metadata if present).
    We'll lightly clean quotes.
    """
    s = after

    # Remove "Frozen by ID: ..." style continuation lines are handled elsewhere;
    # here we just format the main action line.
    s = s.replace("’", "'").replace("“", '"').replace("”", '"')

    # Light cleanup: remove "UniqueNetId..." chunks if ever present: "Name [UniqueNetId:...]" -> "Name"
    s = re.sub(r"\s*\[UniqueNetId:[^\]]+\]", "", s)

    return _normalize_spaces(s)

def format_compact_log(line: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    Produces:
      display_text: "Day 294, 07:12:15 - Atropo claimed baby Megaraptor - Lvl 216 (Megaraptor)"
      embed: Discord embed dict with colour per action keyword
    """
    parsed = _extract_day_time_and_message(line)
    if not parsed:
        return None

    day, hh, mm, ss, after = parsed
    after2 = _extract_player_and_action(after)

    display = f"Day {day}, {hh:02d}:{mm:02d}:{ss:02d} - {after2}"

    # Colour mapping (your exact rules)
    low = after2.lower()

    # Red - Killed / Died / Death / Destroyed
    if any(k in low for k in [" killed", "killed ", " died", "died ", " death", "destroyed"]):
        color = 0xE74C3C
    # Yellow - Demolished OR Unclaimed
    elif "demolished" in low or " unclaimed" in low or low.startswith("unclaimed"):
        color = 0xF1C40F
    # Purple - Claimed
    elif " claimed" in low or low.startswith("claimed"):
        color = 0x9B59B6
    # Green - Tamed
    elif " tamed" in low or "taming" in low:
        color = 0x2ECC71
    # Light blue - Alliance
    elif "alliance" in low:
        color = 0x5DADE2
    # White - Anything else (e.g. froze)
    else:
        color = 0xFFFFFF

    embed = {
        "embeds": [
            {
                "description": display,
                "color": color,
            }
        ]
    }
    return display, embed

def _is_continuation_noise(line: str) -> bool:
    # skip lines like "Frozen by ID: ...." or obvious server noise
    l = line.strip().lower()
    if not l:
        return True
    if l.startswith("frozen by id:"):
        return True
    if "garbage collection triggered" in l:
        return True
    return False

def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()

def _write_timehint(day: int, hh: int, mm: int, ss: int):
    """
    Writes last seen in-game day/time for time_module to use.
    """
    _ensure_data_dir()
    payload = {
        "day": day,
        "hour": hh,
        "minute": mm,
        "second": ss,
        "updated_at": int(time.time())
    }
    try:
        with open(TIMEHINT_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
        # ============================================================
# DISCORD WEBHOOK SEND (supports forum thread_id)
# ============================================================

async def _post_webhook(session: aiohttp.ClientSession, webhook_url: str, payload: Dict[str, Any], thread_id: Optional[str]):
    # Always use params for thread routing (Forum / thread webhooks)
    params = {"wait": "true"}
    if thread_id:
        params["thread_id"] = str(thread_id)

    async with session.post(webhook_url, params=params, json=payload) as r:
        # Discord returns message JSON on success when wait=true
        if r.status >= 300:
            txt = await r.text()
            raise RuntimeError(f"Webhook post failed: {r.status} {txt}")

# ============================================================
# DEDUPE STATE
# ============================================================

class _RouteDedupe:
    """
    Keeps an ordered ring of recent hashes per route.
    """
    def __init__(self, max_items: int):
        self.max_items = max_items
        self.order: List[str] = []
        self.set = set()

    def seen(self, h: str) -> bool:
        return h in self.set

    def add(self, h: str):
        if h in self.set:
            return
        self.set.add(h)
        self.order.append(h)
        if len(self.order) > self.max_items:
            old = self.order.pop(0)
            self.set.discard(old)

# per-tribe dedupe
_DEDUPE_BY_TRIBE: Dict[str, _RouteDedupe] = {}

def _dedupe_for(tribe: str) -> _RouteDedupe:
    key = tribe.lower().strip()
    d = _DEDUPE_BY_TRIBE.get(key)
    if not d:
        d = _RouteDedupe(DEDUPE_MAX)
        _DEDUPE_BY_TRIBE[key] = d
    return d

# ============================================================
# HEARTBEAT
# ============================================================

_last_activity_ts_by_tribe: Dict[str, float] = {}
_last_heartbeat_ts_by_tribe: Dict[str, float] = {}

def _mark_activity(tribe: str):
    _last_activity_ts_by_tribe[tribe.lower().strip()] = time.time()

def _should_heartbeat(tribe: str) -> bool:
    if not HEARTBEAT_ENABLED:
        return False
    now = time.time()
    k = tribe.lower().strip()
    last_hb = _last_heartbeat_ts_by_tribe.get(k, 0.0)
    if now - last_hb < HEARTBEAT_SECONDS:
        return False
    last_act = _last_activity_ts_by_tribe.get(k, 0.0)
    # only heartbeat if NO activity since last heartbeat window
    if last_act > last_hb:
        return False
    _last_heartbeat_ts_by_tribe[k] = now
    return True

# ============================================================
# MAIN LOOP
# ============================================================

_first_run_seeded = False

async def run_tribelogs_loop():
    """
    Polls RCON GetGameLog and routes tribe logs to their configured webhooks.
    Designed to be started once from main.py via asyncio.create_task().
    """
    _require_env()
    _reload_routes()

    global _first_run_seeded
    print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # reload routes each poll so /linktribelog changes apply quickly
                _reload_routes()
                if not _ROUTES:
                    await asyncio.sleep(TRIBELOGS_POLL_SECONDS)
                    continue

                gamelog = await rcon_command("GetGameLog", timeout=10.0)
                if not gamelog:
                    # no output; still heartbeat if needed
                    for rt in _ROUTES:
                        if _should_heartbeat(rt.tribe):
                            try:
                                hb_payload = {"embeds": [{"description": "Heartbeat: no new logs since last (still polling).", "color": 0x95A5A6}]}
                                await _post_webhook(session, rt.webhook, hb_payload, rt.thread_id)
                            except Exception as e:
                                print(f"Heartbeat error for {rt.tribe}: {e}")
                    await asyncio.sleep(TRIBELOGS_POLL_SECONDS)
                    continue

                # Split to lines and filter/format
                lines = gamelog.splitlines()

                # Seed dedupe (prevents backlog spam on boot)
                if not _first_run_seeded:
                    for ln in lines:
                        if _is_continuation_noise(ln):
                            continue
                        # add to all matching tribes, but don't send
                        for rt in _ROUTES:
                            if rt.tribe.lower() in ln.lower():
                                fc = format_compact_log(ln)
                                if not fc:
                                    continue
                                display, _payload = fc
                                h = _sha1(display)
                                _dedupe_for(rt.tribe).add(h)
                                # also write timehint if present
                                parsed = _extract_day_time_and_message(ln)
                                if parsed:
                                    d, hh, mm, ss, _after = parsed
                                    _write_timehint(d, hh, mm, ss)
                    _first_run_seeded = True
                    await asyncio.sleep(TRIBELOGS_POLL_SECONDS)
                    continue

                # Normal run: forward only NEW entries per tribe
                sent_any_for: Dict[str, int] = {}

                for ln in lines:
                    if _is_continuation_noise(ln):
                        continue

                    # Update time hint if line has Day/Time (any tribe)
                    parsed = _extract_day_time_and_message(ln)
                    if parsed:
                        d, hh, mm, ss, _after = parsed
                        _write_timehint(d, hh, mm, ss)

                    for rt in _ROUTES:
                        if rt.tribe.lower() not in ln.lower():
                            continue

                        fc = format_compact_log(ln)
                        if not fc:
                            continue

                        display, payload = fc
                        h = _sha1(display)

                        ded = _dedupe_for(rt.tribe)
                        if ded.seen(h):
                            continue

                        # mark as seen BEFORE sending (prevents burst duplicates on rate limits)
                        ded.add(h)

                        try:
                            await _post_webhook(session, rt.webhook, payload, rt.thread_id)
                            _mark_activity(rt.tribe)
                            sent_any_for[rt.tribe] = sent_any_for.get(rt.tribe, 0) + 1
                        except Exception as e:
                            print(f"GetGameLog/forward error for {rt.tribe}: {e}")

                # Heartbeats (per tribe) only if no activity
                for rt in _ROUTES:
                    if _should_heartbeat(rt.tribe):
                        try:
                            hb_payload = {"embeds": [{"description": "Heartbeat: no new logs since last (still polling).", "color": 0x95A5A6}]}
                            await _post_webhook(session, rt.webhook, hb_payload, rt.thread_id)
                        except Exception as e:
                            print(f"Heartbeat error for {rt.tribe}: {e}")

            except Exception as e:
                print(f"TribeLogs loop error: {e}")

            await asyncio.sleep(TRIBELOGS_POLL_SECONDS)

# ============================================================
# SLASH COMMANDS
# ============================================================

def setup_tribelog_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int = 0):
    """
    Registers:
      /linktribelog tribe webhook_url thread_id(optional)
      /unlinktribelog tribe
      /listroutes
    """
    if not admin_role_id:
        admin_role_id = DEFAULT_ADMIN_ROLE_ID

    guild_obj = discord.Object(id=int(guild_id))

    def _is_admin(i: discord.Interaction) -> bool:
        if not admin_role_id:
            return False
        try:
            return any(getattr(r, "id", None) == int(admin_role_id) for r in i.user.roles)
        except Exception:
            return False

    @tree.command(name="linktribelog", guild=guild_obj)
    async def linktribelog(i: discord.Interaction, tribe: str, webhook_url: str, thread_id: str = ""):
        if not _is_admin(i):
            await i.response.send_message("❌ No permission.", ephemeral=True)
            return

        tribe = tribe.strip()
        webhook_url = webhook_url.strip()
        thread_id = thread_id.strip() or None

        if not tribe or not webhook_url:
            await i.response.send_message("❌ Tribe and webhook_url are required.", ephemeral=True)
            return

        routes = _load_routes()
        # Replace if exists
        replaced = False
        for r in routes:
            if r.tribe.lower() == tribe.lower():
                r.webhook = webhook_url
                r.thread_id = thread_id
                replaced = True
                break
        if not replaced:
            routes.append(TribeRoute(tribe=tribe, webhook=webhook_url, thread_id=thread_id))

        _save_routes(routes)
        _reload_routes()

        await i.response.send_message(
            f"✅ Linked **{tribe}** to webhook (thread_id={thread_id or 'none'}).",
            ephemeral=True
        )

    @tree.command(name="unlinktribelog", guild=guild_obj)
    async def unlinktribelog(i: discord.Interaction, tribe: str):
        if not _is_admin(i):
            await i.response.send_message("❌ No permission.", ephemeral=True)
            return

        tribe = tribe.strip()
        routes = _load_routes()
        new_routes = [r for r in routes if r.tribe.lower() != tribe.lower()]
        _save_routes(new_routes)
        _reload_routes()

        await i.response.send_message(f"✅ Unlinked **{tribe}**.", ephemeral=True)

    @tree.command(name="listroutes", guild=guild_obj)
    async def listroutes(i: discord.Interaction):
        routes = _load_routes()
        if not routes:
            await i.response.send_message("No routes set.", ephemeral=True)
            return

        lines = []
        for r in routes:
            lines.append(f"- **{r.tribe}** -> thread_id={r.thread_id or 'none'}")
        await i.response.send_message("\n".join(lines), ephemeral=True)

    print("[tribelogs_module] ✅ /linktribelog, /unlinktribelog, /listroutes registered")

# ============================================================
# OPTIONAL: helper for time_module to read
# ============================================================

def read_latest_timehint() -> Optional[Dict[str, Any]]:
    """
    Returns the last seen timehint dict from tribe logs, or None.
    """
    try:
        with open(TIMEHINT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None