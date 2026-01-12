import os
import time
import json
import asyncio
import hashlib
import re
from collections import deque
from typing import Optional, Dict, Any, List

import aiohttp
import discord
from discord import app_commands

# =====================
# ENV VARS (REQUIRED)
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Time + players webhooks (can be normal channel webhook OR forum thread webhook)
TIME_WEBHOOK_URL = os.getenv("TIME_WEBHOOK_URL")          # <-- rename from WEBHOOK_URL
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")

# RCON
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = os.getenv("RCON_PORT")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

missing = []
for k in ["DISCORD_TOKEN", "TIME_WEBHOOK_URL", "PLAYERS_WEBHOOK_URL", "RCON_HOST", "RCON_PORT", "RCON_PASSWORD"]:
    if not os.getenv(k):
        missing.append(k)
if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

RCON_PORT = int(RCON_PORT)

# =====================
# CONSTANTS
# =====================
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076
STATUS_VC_ID = 1456615806887657606
PLAYER_CAP = 42

# Day/night model (your current settings)
DAY_SPM = 4.7666667
NIGHT_SPM = 4.045
SUNRISE = 5 * 60 + 30
SUNSET  = 17 * 60 + 30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

STATE_FILE = "state.json"
ROUTES_FILE = "tribe_routes.json"

# Poll intervals
PLAYERS_POLL_SECONDS = 15
TRIBELOG_POLL_SECONDS = 10

# VC rename rate-limit
VC_EDIT_MIN_SECONDS = 300
_last_vc_edit_ts = 0.0
_last_vc_name = None

# Time webhook update step
TIME_UPDATE_STEP_MINUTES = 10

# Heartbeat for tribe threads: only if NO activity for 60 mins
TRIBE_HEARTBEAT_SECONDS = 60 * 60

# Dedupe memory for tribe logs
SEEN_MAX = 3000

# =====================
# DISCORD CLIENT
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =====================
# STATE / STORAGE
# =====================
def load_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

state = load_json(STATE_FILE)

def load_routes() -> List[dict]:
    data = load_json(ROUTES_FILE)
    if not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("routes", [])

def save_routes(routes: List[dict]):
    save_json(ROUTES_FILE, {"routes": routes})

tribe_routes: List[dict] = load_routes()

# Track webhook message IDs so we "edit" instead of spamming for time/players
message_ids = {"time": None, "players": None}

# Tribe-log dedupe per tribe route
_seen_by_route: Dict[str, set] = {}
_seenq_by_route: Dict[str, deque] = {}
_last_activity_ts_by_route: Dict[str, float] = {}

# =====================
# HELPERS
# =====================
def is_day(minute_of_day: int) -> bool:
    return SUNRISE <= minute_of_day < SUNSET

def spm(minute_of_day: int) -> float:
    return DAY_SPM if is_day(minute_of_day) else NIGHT_SPM

def _advance_one_minute(minute_of_day: int, day: int, year: int):
    minute_of_day += 1
    if minute_of_day >= 1440:
        minute_of_day = 0
        day += 1
        if day > 365:
            day = 1
            year += 1
    return minute_of_day, day, year

def calculate_time_details():
    if not state:
        return None

    elapsed = float(time.time() - state["epoch"])
    minute_of_day = int(state["hour"]) * 60 + int(state["minute"])
    day = int(state["day"])
    year = int(state["year"])

    remaining = elapsed
    while True:
        cur_spm = spm(minute_of_day)
        if remaining >= cur_spm:
            remaining -= cur_spm
            minute_of_day, day, year = _advance_one_minute(minute_of_day, day, year)
            continue
        seconds_into_current_minute = remaining
        return minute_of_day, day, year, seconds_into_current_minute, cur_spm

def build_time_embed(minute_of_day: int, day: int, year: int):
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    emoji = "☀️" if is_day(minute_of_day) else "🌙"
    color = DAY_COLOR if is_day(minute_of_day) else NIGHT_COLOR
    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    return {"title": title, "color": color}

def seconds_until_next_round_step(minute_of_day: int, seconds_into_minute: float, step: int):
    m = minute_of_day
    mod = m % step
    minutes_to_boundary = (step - mod) if mod != 0 else step

    remaining_in_current_minute = max(0.0, spm(m) - seconds_into_minute)
    total = remaining_in_current_minute

    m2 = m
    d2 = 1
    y2 = 1
    for _ in range(minutes_to_boundary - 1):
        m2, d2, y2 = _advance_one_minute(m2, d2, y2)
        total += spm(m2)

    return max(0.5, total)

def role_ok(member: discord.Member) -> bool:
    return any(r.id == ADMIN_ROLE_ID for r in getattr(member, "roles", []))

# =====================
# RCON (Minimal)
# =====================
def _rcon_make_packet(req_id: int, ptype: int, body: str) -> bytes:
    data = body.encode("utf-8", errors="ignore") + b"\x00"
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

        # command
        writer.write(_rcon_make_packet(2, 2, command))
        await writer.drain()

        chunks = []
        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                part = await asyncio.wait_for(reader.read(4096), timeout=0.3)
            except asyncio.TimeoutError:
                break
            if not part:
                break
            chunks.append(part)

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
            txt = body.decode("utf-8", errors="replace")  # <-- preserve special chars better than "ignore"
            if txt:
                out.append(txt)

        return "".join(out).strip()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

def parse_listplayers(output: str):
    players = []
    if not output:
        return players
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if ". " in line:
            line = line.split(". ", 1)[1]
        name = line.split(",", 1)[0].strip() if "," in line else line.strip()
        if name and name.lower() not in ("executing", "listplayers", "done"):
            players.append(name)
    return players

# =====================
# WEBHOOK POSTING
# =====================
def with_thread_param(webhook_url: str, thread_id: Optional[str]) -> str:
    if not thread_id:
        return webhook_url
    # Discord expects thread_id=...
    if "thread_id=" in webhook_url:
        return webhook_url
    joiner = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{joiner}thread_id={thread_id}"

async def webhook_post(session: aiohttp.ClientSession, webhook_url: str, payload: dict):
    # retry basic 429
    async with session.post(webhook_url, json=payload) as r:
        if r.status == 429:
            data = await r.json()
            await asyncio.sleep(float(data.get("retry_after", 1.0)))
            return await webhook_post(session, webhook_url, payload)
        if r.status >= 300:
            txt = await r.text()
            raise RuntimeError(f"Discord webhook error {r.status}: {txt}")
        return await r.json(content_type=None)

async def upsert_webhook(session: aiohttp.ClientSession, webhook_url: str, key: str, embed: dict):
    # For time/players we edit one message to avoid spam
    mid = message_ids.get(key)
    if mid:
        async with session.patch(f"{webhook_url}/messages/{mid}", json={"embeds": [embed]}) as r:
            if r.status == 404:
                message_ids[key] = None
                return await upsert_webhook(session, webhook_url, key, embed)
            if r.status >= 300:
                txt = await r.text()
                print(f"Webhook edit failed {r.status}: {txt}")
        return

    data = await webhook_post(session, webhook_url + "?wait=true", {"embeds": [embed]})
    if isinstance(data, dict) and "id" in data:
        message_ids[key] = data["id"]
    else:
        # Don't crash loops if Discord replies weirdly
        print(f"Webhook post unexpected response: {data}")

# =====================
# TRIBE LOG PARSING + COLORS
# =====================
# Strip ARK rich color tags etc.
RICH_TAG_RE = re.compile(r"<\/?RichColor[^>]*>", re.IGNORECASE)
XMLISH_RE = re.compile(r"<[^>]+>")  # catch leftovers

# Extract only: Day X, HH:MM:SS - WHO ACTION...
DAYTIME_RE = re.compile(r"Day\s+(\d+),\s+(\d{1,2}):(\d{2}):(\d{2})\s*:\s*(.+)$")

def clean_text(s: str) -> str:
    s = RICH_TAG_RE.sub("", s)
    s = XMLISH_RE.sub("", s)
    s = s.replace("</>)", "").replace("!)", "").replace("!>)", "").replace("'>)", "'")
    return s.strip()

def simplify_line(line: str) -> Optional[str]:
    # Find the "Day ..., HH:MM:SS: ..." portion, then convert ": " into " - "
    m = DAYTIME_RE.search(line)
    if not m:
        return None
    day = m.group(1)
    hh = m.group(2).zfill(2)
    mm = m.group(3)
    ss = m.group(4)
    rest = clean_text(m.group(5))

    # remove tribe prefix if present in rest
    # e.g. "Tribe Valkyrie, ID ...: Day ...: Sir Magnus froze ..."
    # by the time we get here, rest is after the Day/time already, so this is usually "Sir Magnus ..."
    return f"Day {day}, {hh}:{mm}:{ss} - {rest}"

def classify_color(text: str) -> int:
    t = text.lower()

    # Red - killed/died/death/destroyed
    if any(k in t for k in [" killed", "killed ", " died", "died ", " death", "destroyed"]):
        return 0xE74C3C

    # Yellow - demolished OR unclaimed
    if any(k in t for k in ["demolished", "unclaimed"]):
        return 0xF1C40F

    # Purple - claimed
    if "claimed" in t:
        return 0x9B59B6

    # Green - tamed
    if "tamed" in t or "taming" in t:
        return 0x2ECC71

    # Light blue - alliance
    if "alliance" in t:
        return 0x5DADE2

    # White - anything else (froze, etc.)
    return 0xFFFFFF

def make_embed(text: str) -> dict:
    return {"embeds": [{"description": text, "color": classify_color(text)}]}

def route_key(route: dict) -> str:
    # stable key to separate dedupe per route
    return f"{route.get('tribe','').strip().lower()}|{route.get('webhook','')}|{route.get('thread_id','')}"

def ensure_route_dedupe(route: dict):
    k = route_key(route)
    if k not in _seen_by_route:
        _seen_by_route[k] = set()
        _seenq_by_route[k] = deque(maxlen=SEEN_MAX)
        _last_activity_ts_by_route[k] = time.time()

def line_id(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()

def remember(route: dict, lid: str):
    k = route_key(route)
    seen = _seen_by_route[k]
    q = _seenq_by_route[k]
    if lid in seen:
        return
    seen.add(lid)
    q.append(lid)
    while len(seen) > SEEN_MAX:
        old = q.popleft()
        seen.discard(old)

# =====================
# LOOPS (WRAPPED SO THEY NEVER DIE)
# =====================
async def safe_loop(name: str, coro):
    while True:
        try:
            await coro()
        except Exception as e:
            print(f"[{name}] crashed: {e}")
            await asyncio.sleep(3)

async def time_loop_inner():
    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while True:
            details = calculate_time_details()
            if not details:
                await asyncio.sleep(5)
                continue

            minute_of_day, day, year, seconds_into_minute, _ = details

            if (minute_of_day % TIME_UPDATE_STEP_MINUTES) == 0:
                embed = build_time_embed(minute_of_day, day, year)
                await upsert_webhook(session, TIME_WEBHOOK_URL, "time", embed)

            sleep_for = seconds_until_next_round_step(minute_of_day, seconds_into_minute, TIME_UPDATE_STEP_MINUTES)
            await asyncio.sleep(sleep_for)

async def status_loop_inner():
    global _last_vc_edit_ts, _last_vc_name
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            # RCON only
            online = True
            try:
                out = await rcon_command("ListPlayers", timeout=8.0)
                names = parse_listplayers(out)
            except Exception as e:
                online = False
                names = []
                print(f"ListPlayers error: {e}")

            count = len(names)
            emoji = "🟢" if online else "🔴"

            desc = f"**{count}/{PLAYER_CAP}** online"
            if names:
                desc += "\n\n" + "\n".join([f"{i+1:02d}) {n}" for i, n in enumerate(names[:50])])

            embed = {
                "title": "Online Players",
                "description": desc,
                "color": 0x2ECC71 if online else 0xE74C3C,
                "footer": {"text": f"Last update: {time.strftime('%H:%M:%S')}"}
            }
            await upsert_webhook(session, PLAYERS_WEBHOOK_URL, "players", embed)

            vc = client.get_channel(STATUS_VC_ID)
            if vc:
                new_name = f"{emoji} Solunaris | {count}/{PLAYER_CAP}"
                now = time.time()
                if new_name != _last_vc_name and (now - _last_vc_edit_ts) >= VC_EDIT_MIN_SECONDS:
                    try:
                        await vc.edit(name=new_name)
                        _last_vc_name = new_name
                        _last_vc_edit_ts = now
                    except discord.HTTPException:
                        pass

            await asyncio.sleep(PLAYERS_POLL_SECONDS)

async def tribelog_loop_inner():
    await client.wait_until_ready()
    print("Tribe routes loaded:", [r.get("tribe") for r in tribe_routes])

    async with aiohttp.ClientSession() as session:
        while True:
            if not tribe_routes:
                await asyncio.sleep(TRIBELOG_POLL_SECONDS)
                continue

            # pull GetGameLog once per poll
            try:
                text = await rcon_command("GetGameLog", timeout=12.0)
            except Exception as e:
                print(f"GetGameLog error: {e}")
                await asyncio.sleep(TRIBELOG_POLL_SECONDS)
                continue

            lines = [ln for ln in text.splitlines() if ln.strip()]

            for route in tribe_routes:
                ensure_route_dedupe(route)
                tribe = (route.get("tribe") or "").strip()
                webhook = (route.get("webhook") or "").strip()
                thread_id = (route.get("thread_id") or "").strip() or None

                if not tribe or not webhook:
                    continue

                k = route_key(route)
                sent_any = False

                for ln in lines:
                    # filter by tribe name EXACT token
                    if tribe.lower() not in ln.lower():
                        continue

                    simplified = simplify_line(ln)
                    if not simplified:
                        continue

                    lid = line_id(tribe + "||" + simplified)
                    if lid in _seen_by_route[k]:
                        continue

                    # send (forum thread safe)
                    final_webhook = with_thread_param(webhook, thread_id)
                    payload = make_embed(simplified)

                    try:
                        await webhook_post(session, final_webhook, payload)
                    except Exception as e:
                        # DO NOT mark seen if send failed
                        print(f"Webhook send failed for {tribe}: {e}")
                        continue

                    remember(route, lid)
                    _last_activity_ts_by_route[k] = time.time()
                    sent_any = True

                # heartbeat: only if no activity for 60 mins
                if not sent_any:
                    last_ts = _last_activity_ts_by_route.get(k, time.time())
                    if (time.time() - last_ts) >= TRIBE_HEARTBEAT_SECONDS:
                        final_webhook = with_thread_param(webhook, thread_id)
                        try:
                            await webhook_post(session, final_webhook, make_embed("✅ Heartbeat: still polling (no new logs)."))
                            _last_activity_ts_by_route[k] = time.time()
                            print(f"Heartbeat sent for {tribe}")
                        except Exception as e:
                            print(f"Heartbeat failed for {tribe}: {e}")

            await asyncio.sleep(TRIBELOG_POLL_SECONDS)

# =====================
# COMMANDS
# =====================
@tree.command(name="settime", guild=discord.Object(id=GUILD_ID))
async def settime(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
    if not role_ok(i.user):
        await i.response.send_message("❌ No permission", ephemeral=True)
        return

    if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await i.response.send_message("❌ Invalid values.", ephemeral=True)
        return

    global state
    state = {"epoch": time.time(), "year": int(year), "day": int(day), "hour": int(hour), "minute": int(minute)}
    save_json(STATE_FILE, state)
    await i.response.send_message("✅ Time set", ephemeral=True)

@tree.command(name="linktribelog", guild=discord.Object(id=GUILD_ID))
async def linktribelog(i: discord.Interaction, tribe: str, webhook_url: str, thread_id: str):
    """
    Admin-only: add a tribe -> webhook -> forum thread mapping
    """
    if not role_ok(i.user):
        await i.response.send_message("❌ No permission", ephemeral=True)
        return

    route = {"tribe": tribe.strip(), "webhook": webhook_url.strip(), "thread_id": str(thread_id).strip()}
    tribe_routes.append(route)
    save_routes(tribe_routes)
    await i.response.send_message(f"✅ Linked tribe route:\n```json\n{json.dumps(route, ensure_ascii=False, indent=2)}\n```", ephemeral=True)

@tree.command(name="routes", guild=discord.Object(id=GUILD_ID))
async def routes_cmd(i: discord.Interaction):
    if not role_ok(i.user):
        await i.response.send_message("❌ No permission", ephemeral=True)
        return
    await i.response.send_message(f"```json\n{json.dumps(tribe_routes, ensure_ascii=False, indent=2)}\n```", ephemeral=True)

# =====================
# STARTUP
# =====================
@client.event
async def on_ready():
    # This is the critical part: if this doesn't run, slash commands won't appear.
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("✅ Solunaris bot online (RCON only) | commands synced to guild", GUILD_ID)

    # Start loops safely (they will auto-restart on crash)
    client.loop.create_task(safe_loop("time_loop", time_loop_inner))
    client.loop.create_task(safe_loop("status_loop", status_loop_inner))
    client.loop.create_task(safe_loop("tribelog_loop", tribelog_loop_inner))

client.run(DISCORD_TOKEN)