# tribelogs_module.py
# RCON GetGameLog -> per-tribe webhook (forum thread supported) + /linktribelog persistence

import os
import time
import json
import asyncio
import hashlib
import re
from collections import deque
from typing import Dict, Any, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands

# =====================
# ENV
# =====================
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "0") or "0")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

# Polling
POLL_SECONDS = int(os.getenv("TRIBELOG_POLL_SECONDS", "10"))
HEARTBEAT_IDLE_SECONDS = int(os.getenv("TRIBELOG_HEARTBEAT_IDLE_SECONDS", "3600"))  # 60 min

# Persistence (use your Railway volume path)
DATA_DIR = os.getenv("DATA_DIR", "/data")
ROUTES_FILE = os.getenv("TRIBELOG_ROUTES_FILE", os.path.join(DATA_DIR, "tribelog_routes.json"))

# Deduping
DEDUP_MAX = int(os.getenv("TRIBELOG_DEDUP_MAX", "4000"))

# Optional: filter only lines that contain "Day X, HH:MM:SS"
REQUIRE_DAYSTAMP = os.getenv("TRIBELOG_REQUIRE_DAYSTAMP", "1") == "1"

# Optional: allow seeding from current log (prevents backlog spam)
SEED_ON_START = os.getenv("TRIBELOG_SEED_ON_START", "1") == "1"

if not (RCON_HOST and RCON_PORT and RCON_PASSWORD):
    raise RuntimeError("Missing required env vars: RCON_HOST, RCON_PORT, RCON_PASSWORD")

os.makedirs(DATA_DIR, exist_ok=True)

# =====================
# RCON (minimal)
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
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout
    )
    try:
        # auth
        writer.write(_rcon_make_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        await asyncio.wait_for(reader.read(4096), timeout=timeout)

        # command
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

        out = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i:i+4], "little", signed=True)
            i += 4
            if size < 10 or i + size > len(data):
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
# ROUTES persistence
# =====================
def _load_routes() -> List[Dict[str, Any]]:
    if not os.path.exists(ROUTES_FILE):
        return []
    try:
        with open(ROUTES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []

def _save_routes(routes: List[Dict[str, Any]]) -> None:
    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(routes, f, indent=2)

def _normalize_webhook_base(url: str) -> str:
    # Store base webhook URL only (strip any query like ?Thread=...)
    return url.split("?", 1)[0].strip()

def _upsert_route(tribe: str, webhook_url: str, thread_id: str) -> Dict[str, Any]:
    routes = _load_routes()
    base = _normalize_webhook_base(webhook_url)

    new_route = {"tribe": tribe.strip(), "webhook": base, "thread_id": str(thread_id).strip()}
    replaced = False
    for idx, r in enumerate(routes):
        if str(r.get("tribe", "")).strip().lower() == tribe.strip().lower():
            routes[idx] = new_route
            replaced = True
            break
    if not replaced:
        routes.append(new_route)

    _save_routes(routes)
    return new_route

# =====================
# Parsing + formatting
# =====================
DAYSTAMP_RE = re.compile(r"Day\s+\d+,\s+\d{1,2}:\d{2}:\d{2}")

def _line_hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def _is_tribe_line(line: str, tribe: str) -> bool:
    if tribe.lower() not in line.lower():
        return False
    if REQUIRE_DAYSTAMP and not DAYSTAMP_RE.search(line):
        return False
    return True

def _clean_line(line: str) -> str:
    # Keep it readable; remove RichColor tags, trailing junk etc.
    s = line.strip()

    # Remove <RichColor ...> ... </> wrappers
    s = re.sub(r"<RichColor[^>]*>", "", s)
    s = s.replace("</>", "")

    # remove common trailing garbage like "!>)", "</>)", "!))", etc.
    s = re.sub(r"[)\s]*[!]*[)\s]*>$", "", s)
    s = re.sub(r"</\)\)\s*$", "", s)
    s = re.sub(r"</\)>\s*$", "", s)
    s = re.sub(r"[!]\)\s*$", "", s)
    s = re.sub(r"\)\)\s*$", ")", s)

    # If your log line includes leading server timestamp like "[2026...][id]2026...: Tribe ..."
    # keep from "Tribe ..." onward if present
    m = re.search(r"(Tribe\s+.+)", s)
    if m:
        s = m.group(1)

    return s.strip()

def _embed_color(text: str) -> int:
    lower = text.lower()

    # Red - Killed / Died / Death / Destroyed
    if any(k in lower for k in ["killed", "died", "death", "destroyed"]):
        return 0xE74C3C

    # Yellow - Demolished + Unclaimed
    if "demolished" in lower or "unclaimed" in lower:
        return 0xF1C40F

    # Purple - Claimed
    if "claimed" in lower:
        return 0x9B59B6

    # Green - Tamed
    if "tamed" in lower or "taming" in lower:
        return 0x2ECC71

    # Light blue - Alliance
    if "alliance" in lower:
        return 0x5DADE2

    # White - anything else (eg froze)
    return 0xFFFFFF

def _build_payload(clean_text: str) -> Dict[str, Any]:
    return {
        "embeds": [
            {
                "description": clean_text,
                "color": _embed_color(clean_text),
            }
        ]
    }

# =====================
# Discord webhook sender (forum thread compatible)
# =====================
async def _post_webhook(session: aiohttp.ClientSession, webhook_base: str, thread_id: str, payload: Dict[str, Any]):
    # Forum thread posting: use thread_id query parameter.
    url = f"{webhook_base}?wait=true&thread_id={thread_id}"
    async with session.post(url, json=payload) as r:
        # Discord can return 200/204 for webhooks depending on wait=true & permissions
        if r.status >= 400:
            txt = await r.text()
            raise RuntimeError(f"Webhook post failed: {r.status} {txt}")

# =====================
# Loop
# =====================
_routes_cache: List[Dict[str, Any]] = []
_seen = deque(maxlen=DEDUP_MAX)
_last_activity_ts = 0.0

def _seed_seen_from_text(text: str):
    if not text:
        return
    for ln in text.splitlines():
        ln = ln.strip()
        if ln:
            _seen.append(_line_hash(ln))

async def _send_heartbeat_if_idle(session: aiohttp.ClientSession):
    global _last_activity_ts
    if not _routes_cache:
        return
    if _last_activity_ts == 0:
        return
    if (time.time() - _last_activity_ts) < HEARTBEAT_IDLE_SECONDS:
        return

    # Send a single heartbeat per route, then reset timer
    for r in _routes_cache:
        try:
            payload = _build_payload("Heartbeat: no new logs since last (still polling).")
            await _post_webhook(session, r["webhook"], r["thread_id"], payload)
        except Exception as e:
            print(f"Heartbeat error for {r.get('tribe')}: {e}")

    _last_activity_ts = time.time()

async def run_tribelogs_loop():
    global _routes_cache, _last_activity_ts

    _routes_cache = _load_routes()
    print("Tribe routes loaded:", [r.get("tribe") for r in _routes_cache])

    async with aiohttp.ClientSession() as session:
        # Seed dedupe from current GetGameLog so we don't spam old stuff
        if SEED_ON_START:
            try:
                text = await rcon_command("GetGameLog", timeout=10.0)
                _seed_seen_from_text(text)
                print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")
            except Exception as e:
                print("Seed error:", e)

        _last_activity_ts = time.time()

        while True:
            try:
                # Refresh routes each poll so /linktribelog takes effect immediately
                _routes_cache = _load_routes()

                text = await rcon_command("GetGameLog", timeout=10.0)
                if not text:
                    await asyncio.sleep(POLL_SECONDS)
                    await _send_heartbeat_if_idle(session)
                    continue

                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                new_lines = []
                for ln in lines:
                    h = _line_hash(ln)
                    if h in _seen:
                        continue
                    _seen.append(h)
                    new_lines.append(ln)

                if new_lines and _routes_cache:
                    sent_any = False
                    # Send each new tribe line to the matching tribe route(s)
                    for ln in new_lines:
                        for r in _routes_cache:
                            tribe = str(r.get("tribe", "")).strip()
                            if not tribe:
                                continue
                            if _is_tribe_line(ln, tribe):
                                clean = _clean_line(ln)
                                payload = _build_payload(clean)
                                try:
                                    await _post_webhook(session, r["webhook"], r["thread_id"], payload)
                                    sent_any = True
                                except Exception as e:
                                    print(f"GetGameLog/forward error for {tribe}: {e}")

                    if sent_any:
                        _last_activity_ts = time.time()

                await _send_heartbeat_if_idle(session)

            except Exception as e:
                print("TribeLogs loop error:", e)

            await asyncio.sleep(POLL_SECONDS)

# =====================
# Slash command registration
# =====================
def setup_tribelog_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int):
    guild = discord.Object(id=guild_id)

    @tree.command(name="linktribelog", guild=guild)
    async def linktribelog(
        i: discord.Interaction,
        tribe_name: str,
        webhook_url: str,
        thread_id: str,
    ):
        # Admin-only
        if not any(r.id == admin_role_id for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        route = _upsert_route(tribe_name, webhook_url, thread_id)
        await i.response.send_message(f"✅ Linked tribe route:\n```json\n{json.dumps(route, indent=2)}\n```", ephemeral=True)

    @tree.command(name="listtribelogs", guild=guild)
    async def listtribelogs(i: discord.Interaction):
        if not any(r.id == admin_role_id for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return
        routes = _load_routes()
        await i.response.send_message(f"```json\n{json.dumps(routes, indent=2)}\n```", ephemeral=True)