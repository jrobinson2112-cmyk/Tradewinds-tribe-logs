import os
import re
import json
import time
import asyncio
import aiohttp
import discord
from discord import app_commands
from typing import Dict, Any, List, Optional, Tuple

from players_module import rcon_command  # uses your existing RCON helper

# =========================
# PERSISTENCE (Railway volume)
# =========================
DATA_DIR = os.getenv("DATA_DIR", "/data")
ROUTES_FILE = os.path.join(DATA_DIR, "tribelog_routes.json")
DEDUPE_FILE = os.path.join(DATA_DIR, "tribelog_dedupe.json")

# ✅ Shared time file for time_module auto-sync
LAST_INGAME_TIME_FILE = os.path.join(DATA_DIR, "last_ingame_time.json")

def _ensure_data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass

# =========================
# CONFIG
# =========================
GUILD_ID = int(os.getenv("GUILD_ID", "1430388266393276509"))
ADMIN_ROLE_ID_DEFAULT = int(os.getenv("ADMIN_ROLE_ID", "1439069787207766076"))

GAMELOG_POLL_SECONDS = float(os.getenv("TRIBELOG_POLL_SECONDS", "15"))
HEARTBEAT_IDLE_SECONDS = float(os.getenv("TRIBELOG_HEARTBEAT_IDLE_SECONDS", "3600"))  # 60 mins
MAX_FORWARD_PER_TRIBE_PER_POLL = int(os.getenv("TRIBELOG_MAX_FORWARD_PER_POLL", "20"))

# =========================
# PARSING
# =========================
_DAYTIME_RE = re.compile(r"Day\s+(\d+)\s*,\s*(\d{1,2}):(\d{2}):(\d{2})")

def _line_mentions_tribe(line: str, tribe: str) -> bool:
    low = line.lower()
    t = tribe.lower()
    return (f"tribe {t}" in low) or (t in low)

def _extract_daytime(line: str) -> Optional[Tuple[int, int, int, int]]:
    m = _DAYTIME_RE.search(line)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))

# =========================
# ✅ NEW: write last in-game timestamp for time_module
# =========================
def write_last_ingame_time(day: int, hour: int, minute: int, second: int, source: str = "GetGameLog"):
    _ensure_data_dir()
    payload = {
        "day": int(day),
        "hour": int(hour),
        "minute": int(minute),
        "second": int(second),
        "source": str(source),
        "written_at_epoch": int(time.time()),
    }
    tmp = LAST_INGAME_TIME_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, LAST_INGAME_TIME_FILE)

# =========================
# EMBED COLORS
# =========================
COLOR_RED = 0xE74C3C        # Killed / Died / Death / Destroyed
COLOR_YELLOW = 0xF1C40F     # Demolished + Unclaimed
COLOR_PURPLE = 0x9B59B6     # Claimed
COLOR_GREEN = 0x2ECC71      # Tamed
COLOR_LIGHT_BLUE = 0x5DADE2 # Alliance
COLOR_WHITE = 0xFFFFFF      # Anything else

def pick_color(message: str) -> int:
    m = message.lower()
    if any(k in m for k in ["killed", "died", "death", "destroyed"]):
        return COLOR_RED
    if "demolish" in m or "demolished" in m or "unclaimed" in m:
        return COLOR_YELLOW
    if "claimed" in m:
        return COLOR_PURPLE
    if "tamed" in m:
        return COLOR_GREEN
    if "alliance" in m:
        return COLOR_LIGHT_BLUE
    return COLOR_WHITE
def load_routes() -> List[Dict[str, str]]:
    _ensure_data_dir()
    if not os.path.exists(ROUTES_FILE):
        return []
    try:
        with open(ROUTES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            out = []
            for r in data:
                if not isinstance(r, dict):
                    continue
                tribe = str(r.get("tribe", "")).strip()
                webhook = str(r.get("webhook", "")).strip()
                thread_id = str(r.get("thread_id", "")).strip()
                if tribe and webhook and thread_id:
                    out.append({"tribe": tribe, "webhook": webhook, "thread_id": thread_id})
            return out
    except Exception:
        return []
    return []

def save_routes(routes: List[Dict[str, str]]):
    _ensure_data_dir()
    tmp = ROUTES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(routes, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ROUTES_FILE)

def load_dedupe() -> Dict[str, Any]:
    _ensure_data_dir()
    if not os.path.exists(DEDUPE_FILE):
        return {"last_seen_hashes": {}}
    try:
        with open(DEDUPE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("last_seen_hashes"), dict):
            return d
    except Exception:
        pass
    return {"last_seen_hashes": {}}

def save_dedupe(d: Dict[str, Any]):
    _ensure_data_dir()
    tmp = DEDUPE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DEDUPE_FILE)

async def send_to_thread_webhook(session: aiohttp.ClientSession, base_webhook_url: str, thread_id: str, embed: dict):
    url = base_webhook_url.split("?", 1)[0]
    post_url = f"{url}?wait=true&thread_id={thread_id}"
    async with session.post(post_url, json={"embeds": [embed]}) as r:
        if r.status not in (200, 204):
            try:
                data = await r.json()
            except Exception:
                data = await r.text()
            raise RuntimeError(f"Webhook post failed: {r.status} {data}")

def _hash_line(s: str) -> str:
    return str(abs(hash(s)))

async def _poll_gamelog() -> str:
    return await rcon_command("GetGameLog", timeout=10.0)

async def _tribelogs_loop():
    routes = load_routes()
    print("Tribe routes loaded:", [r["tribe"] for r in routes])

    dedupe = load_dedupe()
    last_seen_hashes: Dict[str, str] = dedupe.get("last_seen_hashes", {})

    last_activity_ts: Dict[str, float] = {r["tribe"]: time.time() for r in routes}
    first_run_seeded = False

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                routes = load_routes()  # dynamic reload
                for r in routes:
                    last_activity_ts.setdefault(r["tribe"], time.time())
                    last_seen_hashes.setdefault(r["tribe"], "")

                text = await _poll_gamelog()
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()] if text else []

                if not first_run_seeded:
                    if lines:
                        for r in routes:
                            tribe = r["tribe"]
                            for ln in reversed(lines):
                                if _line_mentions_tribe(ln, tribe):
                                    last_seen_hashes[tribe] = _hash_line(ln)
                                    break
                        dedupe["last_seen_hashes"] = last_seen_hashes
                        save_dedupe(dedupe)
                    print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")
                    first_run_seeded = True
                    await asyncio.sleep(GAMELOG_POLL_SECONDS)
                    continue

                # ✅ NEW (minimal): write latest in-game timestamp from ANY line
                for ln in reversed(lines):
                    dt = _extract_daytime(ln)
                    if dt:
                        d, h, m, s = dt
                        write_last_ingame_time(d, h, m, s, source="GetGameLog")
                        break

                for r in routes:
                    tribe = r["tribe"]
                    webhook = r["webhook"]
                    thread_id = r["thread_id"]

                    tribe_lines = [ln for ln in lines if _line_mentions_tribe(ln, tribe)]

                    if not tribe_lines:
                        if (time.time() - last_activity_ts.get(tribe, 0)) >= HEARTBEAT_IDLE_SECONDS:
                            embed = {"description": "Heartbeat: no new logs since last (still polling).", "color": 0x95A5A6}
                            await send_to_thread_webhook(session, webhook, thread_id, embed)
                            last_activity_ts[tribe] = time.time()
                        continue

                    last_hash = last_seen_hashes.get(tribe, "")
                    new_batch: List[str] = []
                    for ln in reversed(tribe_lines):
                        if _hash_line(ln) == last_hash and last_hash:
                            break
                        new_batch.append(ln)
                    new_batch.reverse()

                    if not new_batch:
                        if (time.time() - last_activity_ts.get(tribe, 0)) >= HEARTBEAT_IDLE_SECONDS:
                            embed = {"description": "Heartbeat: no new logs since last (still polling).", "color": 0x95A5A6}
                            await send_to_thread_webhook(session, webhook, thread_id, embed)
                            last_activity_ts[tribe] = time.time()
                        continue

                    new_batch = new_batch[-MAX_FORWARD_PER_TRIBE_PER_POLL:]

                    for ln in new_batch:
                        embed = {"description": ln, "color": pick_color(ln)}
                        await send_to_thread_webhook(session, webhook, thread_id, embed)
                        last_activity_ts[tribe] = time.time()

                    last_seen_hashes[tribe] = _hash_line(new_batch[-1])
                    dedupe["last_seen_hashes"] = last_seen_hashes
                    save_dedupe(dedupe)

            except Exception as e:
                print(f"TribeLogs loop error: {e}")

            await asyncio.sleep(GAMELOG_POLL_SECONDS)

def run_tribelogs_loop(client: discord.Client):
    return _tribelogs_loop()

def setup_tribelog_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int = ADMIN_ROLE_ID_DEFAULT):
    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(name="linktribelog", guild=guild_obj)
    @app_commands.describe(
        tribe="Tribe name as it appears in logs",
        webhook="Discord webhook URL for that tribe",
        thread_id="Forum thread ID to post into"
    )
    async def linktribelog(i: discord.Interaction, tribe: str, webhook: str, thread_id: str):
        if not any(r.id == int(admin_role_id) for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission.", ephemeral=True)
            return

        tribe = tribe.strip()
        webhook = webhook.strip()
        thread_id = str(thread_id).strip()

        if not tribe or not webhook or not thread_id:
            await i.response.send_message("❌ Missing tribe/webhook/thread_id.", ephemeral=True)
            return

        routes = load_routes()

        updated = False
        for r in routes:
            if r["tribe"].lower() == tribe.lower():
                r["tribe"] = tribe
                r["webhook"] = webhook
                r["thread_id"] = thread_id
                updated = True
                break

        if not updated:
            routes.append({"tribe": tribe, "webhook": webhook, "thread_id": thread_id})

        save_routes(routes)
        await i.response.send_message(f"✅ Linked tribe route saved for **{tribe}**.", ephemeral=True)
        print("Linked tribe route:", {"tribe": tribe, "webhook": webhook, "thread_id": thread_id})