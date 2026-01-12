import os
import time
import json
import asyncio
import aiohttp
import discord
from discord import app_commands
import re
from typing import Any, Dict, List, Optional, Tuple

# =========================================================
# ENV
# =========================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Time + players (same as your Tradewinds time bot)
WEBHOOK_URL = os.getenv("WEBHOOK_URL")                 # time webhook
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL") # players webhook
NITRADO_TOKEN = os.getenv("NITRADO_TOKEN")
NITRADO_SERVICE_ID = os.getenv("NITRADO_SERVICE_ID")

# RCON (used for ListPlayers + GetGameLog)
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = os.getenv("RCON_PORT")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

# Tribe routing (JSON)
# Example value (single line):
# [{"tribe":"Valkyrie","webhook":"https://discord.com/api/webhooks/.../....","thread_id":"1459805053379547199"}]
TRIBE_ROUTES_RAW = os.getenv("TRIBE_ROUTES", "[]")

REQUIRED_ENV = [
    "DISCORD_TOKEN",
    "WEBHOOK_URL",
    "PLAYERS_WEBHOOK_URL",
    "NITRADO_TOKEN",
    "NITRADO_SERVICE_ID",
    "RCON_HOST",
    "RCON_PORT",
    "RCON_PASSWORD",
    "TRIBE_ROUTES",
]

missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

RCON_PORT = int(RCON_PORT)

# =========================================================
# CONSTANTS (your server)
# =========================================================
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

STATUS_VC_ID = 1456615806887657606
ANNOUNCE_CHANNEL_ID = 1430388267446042666
PLAYER_CAP = 42

# Time model (keep as-is)
DAY_SPM = 4.7666667
NIGHT_SPM = 4.045
SUNRISE = 5 * 60 + 30
SUNSET  = 17 * 60 + 30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

STATE_FILE = "state.json"

# Poll intervals
STATUS_POLL_SECONDS = 15
TIME_UPDATE_STEP_MINUTES = 10

# VC rename rate-limit
VC_EDIT_MIN_SECONDS = 300
_last_vc_edit_ts = 0.0
_last_vc_name = None

# Tribe log polling + heartbeat
TRIBE_POLL_SECONDS = 10
TRIBE_HEARTBEAT_MINUTES = 60  # only if NO activity
MAX_SEND_PER_POLL = 10

# Persist routes from /linktribelog
ROUTES_FILE = "tribe_routes.json"

# =========================================================
# DISCORD SETUP
# =========================================================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =========================================================
# SHARED STATE
# =========================================================
message_ids = {"time": None, "players": None}
last_announced_day = None

# Tribe forwarder state (dedupe + heartbeat)
_seen_line_hashes: Dict[str, float] = {}  # key -> ts
_last_activity_ts_by_tribe: Dict[str, float] = {}
_last_heartbeat_ts_by_tribe: Dict[str, float] = {}

# =========================================================
# HELPERS: JSON + STATE
# =========================================================
def load_state() -> Optional[dict]:
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(s: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f)

state = load_state()

def _load_routes_from_disk() -> List[dict]:
    if not os.path.exists(ROUTES_FILE):
        return []
    try:
        with open(ROUTES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def _save_routes_to_disk(routes: List[dict]) -> None:
    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(routes, f)

def parse_routes(raw: str) -> List[dict]:
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        out = []
        for r in data:
            if not isinstance(r, dict):
                continue
            tribe = str(r.get("tribe", "")).strip()
            webhook = str(r.get("webhook", "")).strip()
            thread_id = r.get("thread_id")
            thread_id = str(thread_id).strip() if thread_id is not None else None
            if tribe and webhook:
                out.append({"tribe": tribe, "webhook": webhook, "thread_id": thread_id})
        return out
    except Exception:
        return []

# Start with env routes, then merge disk routes (disk wins for same tribe)
_routes_env = parse_routes(TRIBE_ROUTES_RAW)
_routes_disk = _load_routes_from_disk()
_routes_by_tribe: Dict[str, dict] = {r["tribe"].lower(): r for r in _routes_env}
for r in _routes_disk:
    if isinstance(r, dict) and r.get("tribe") and r.get("webhook"):
        _routes_by_tribe[str(r["tribe"]).lower()] = r

def get_routes() -> List[dict]:
    return list(_routes_by_tribe.values())

def upsert_route(route: dict) -> None:
    _routes_by_tribe[str(route["tribe"]).lower()] = route
    _save_routes_to_disk(get_routes())

# =========================================================
# TIME LOGIC (unchanged)
# =========================================================
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
    d2, y2 = 0, 0  # unused
    for _ in range(minutes_to_boundary - 1):
        m2, d2, y2 = _advance_one_minute(m2, d2, y2)
        total += spm(m2)

    return max(0.5, total)

# =========================================================
# NITRADO STATUS
# =========================================================
async def get_server_status(session: aiohttp.ClientSession):
    headers = {"Authorization": f"Bearer {NITRADO_TOKEN}"}
    url = f"https://api.nitrado.net/services/{NITRADO_SERVICE_ID}/gameservers"

    async with session.get(url, headers=headers) as r:
        data = await r.json()

    gs = data["data"]["gameserver"]
    status = str(gs.get("status", "")).lower()
    online = status in ("started", "running", "online")
    players = int(gs.get("query", {}).get("player_current", 0) or 0)
    return online, players

# =========================================================
# RCON (minimal)
# =========================================================
def _rcon_make_packet(req_id: int, ptype: int, body: str) -> bytes:
    data = body.encode("utf-8", errors="replace") + b"\x00"
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
        writer.write(_rcon_make_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        _ = await asyncio.wait_for(reader.read(4096), timeout=timeout)

        writer.write(_rcon_make_packet(2, 2, command))
        await writer.drain()

        chunks = []
        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                part = await asyncio.wait_for(reader.read(4096), timeout=0.4)
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
        if "," in line:
            name = line.split(",", 1)[0].strip()
        else:
            name = line.strip()
        if name and name.lower() not in ("executing", "listplayers", "done"):
            players.append(name)
    return players

# =========================================================
# WEBHOOK HELPERS
# =========================================================
async def upsert_webhook(session: aiohttp.ClientSession, url: str, key: str, embed: dict):
    mid = message_ids.get(key)
    if mid:
        async with session.patch(f"{url}/messages/{mid}", json={"embeds": [embed]}) as r:
            if r.status == 404:
                message_ids[key] = None
                return await upsert_webhook(session, url, key, embed)
        return

    async with session.post(url + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json()
        message_ids[key] = data["id"]

def webhook_with_thread(webhook: str, thread_id: Optional[str]) -> str:
    # IMPORTANT: forum webhooks must use thread_id query param
    if thread_id:
        joiner = "&" if "?" in webhook else "?"
        return f"{webhook}{joiner}wait=true&thread_id={thread_id}"
    else:
        joiner = "&" if "?" in webhook else "?"
        return f"{webhook}{joiner}wait=true"

async def send_log_embed(session: aiohttp.ClientSession, webhook: str, thread_id: Optional[str], title: str, color: int):
    url = webhook_with_thread(webhook, thread_id)
    payload = {"embeds": [{"description": title, "color": color}]}
    async with session.post(url, json=payload) as r:
        if r.status >= 300:
            try:
                txt = await r.text()
            except Exception:
                txt = ""
            print(f"Discord webhook error {r.status}: {txt[:300]}")

# =========================================================
# PLAYERS EMBED (unchanged)
# =========================================================
async def update_players_embed(session: aiohttp.ClientSession):
    online_nitrado, nitrado_count = await get_server_status(session)

    names = []
    rcon_ok = True
    rcon_err = None
    try:
        out = await rcon_command("ListPlayers", timeout=6.0)
        names = parse_listplayers(out)
    except Exception as e:
        rcon_ok = False
        rcon_err = str(e)

    online = online_nitrado or rcon_ok
    count = len(names) if names else nitrado_count
    emoji = "🟢" if online else "🔴"

    if names:
        lines = [f"{idx+1:02d}) {n}" for idx, n in enumerate(names[:50])]
        desc = f"**{count}/{PLAYER_CAP}** online\n\n" + "\n".join(lines)
    else:
        if not rcon_ok:
            desc = f"**{count}/{PLAYER_CAP}** online\n\n*(Could not fetch player names via RCON: {rcon_err})*"
        else:
            desc = f"**{count}/{PLAYER_CAP}** online\n\n*(No player list returned.)*"

    embed = {
        "title": "Online Players",
        "description": desc,
        "color": 0x2ECC71 if online else 0xE74C3C,
        "footer": {"text": f"Last update: {time.strftime('%H:%M:%S')}"}
    }
    await upsert_webhook(session, PLAYERS_WEBHOOK_URL, "players", embed)
    return emoji, count, online

# =========================================================
# TRIBE LOG PARSING (Day, Time, Who and What)
# =========================================================
DAYLINE_RE = re.compile(r"(Day\s+\d+,\s+\d{1,2}:\d{2}:\d{2})\s*:\s*(.+)$", re.IGNORECASE)
RICHCOLOR_RE = re.compile(r"<\s*RichColor[^>]*>", re.IGNORECASE)

def clean_line(s: str) -> str:
    s = s.replace("\u200b", "").strip()
    s = RICHCOLOR_RE.sub("", s)               # remove <RichColor ...>
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip("!")                         # remove trailing !!! / !
    s = s.rstrip(")") if s.endswith("))") else s
    return s

def format_day_time_who_what(line: str) -> Optional[str]:
    line = clean_line(line)
    m = DAYLINE_RE.search(line)
    if not m:
        return None
    day_time = m.group(1).strip()
    rest = m.group(2).strip()
    # exactly: "Day X, HH:MM:SS - Who What"
    return f"{day_time} - {rest}"

def pick_color(msg: str) -> int:
    t = msg.lower()
    # Red - Killed / Died / Death / Destroyed
    if any(k in t for k in [" killed", " was killed", " died", " death", " destroyed", " starved to death"]):
        return 0xE74C3C
    # Yellow - Demolished + Unclaimed
    if "demolish" in t or "unclaim" in t:
        return 0xF1C40F
    # Purple - Claimed
    if " claimed" in t:
        return 0x9B59B6
    # Green - Tamed
    if "tamed" in t:
        return 0x2ECC71
    # Light blue - Alliance
    if "alliance" in t:
        return 0x5DADE2
    # White - Anything else (e.g. Froze)
    return 0xFFFFFF

def line_hash_key(tribe: str, formatted: str) -> str:
    # stable dedupe key
    return f"{tribe.lower()}::{formatted}"

# =========================================================
# LOOPS
# =========================================================
async def time_loop():
    global last_announced_day
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            details = calculate_time_details()
            if not details:
                await asyncio.sleep(5)
                continue

            minute_of_day, day, year, seconds_into_minute, _cur_spm = details

            if (minute_of_day % TIME_UPDATE_STEP_MINUTES) == 0:
                embed = build_time_embed(minute_of_day, day, year)
                await upsert_webhook(session, WEBHOOK_URL, "time", embed)

                absolute_day = year * 365 + day
                if last_announced_day is None:
                    last_announced_day = absolute_day
                elif absolute_day > last_announced_day:
                    ch = client.get_channel(ANNOUNCE_CHANNEL_ID)
                    if ch:
                        await ch.send(f"📅 **New Solunaris Day** — Day **{day}**, Year **{year}**")
                    last_announced_day = absolute_day

            sleep_for = seconds_until_next_round_step(minute_of_day, seconds_into_minute, TIME_UPDATE_STEP_MINUTES)
            await asyncio.sleep(sleep_for)

async def status_loop():
    global _last_vc_edit_ts, _last_vc_name
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            emoji, count, online = await update_players_embed(session)

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

            await asyncio.sleep(STATUS_POLL_SECONDS)

async def tribe_logs_loop():
    await client.wait_until_ready()
    routes = get_routes()
    print("Routing tribes:", ", ".join(r["tribe"] for r in routes) if routes else "(none)")

    # seed dedupe once to avoid backlog spam
    try:
        seed = await rcon_command("GetGameLog", timeout=10.0)
        if seed:
            for r in get_routes():
                tribe = r["tribe"]
                for ln in seed.splitlines():
                    if tribe.lower() not in ln.lower():
                        continue
                    formatted = format_day_time_who_what(ln)
                    if formatted:
                        _seen_line_hashes[line_hash_key(tribe, formatted)] = time.time()
        print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")
    except Exception as e:
        print(f"Seed dedupe error: {e}")

    async with aiohttp.ClientSession() as session:
        while True:
            routes = get_routes()

            try:
                text = await rcon_command("GetGameLog", timeout=10.0)
            except Exception as e:
                print(f"GetGameLog error: {e}")
                await asyncio.sleep(TRIBE_POLL_SECONDS)
                continue

            now = time.time()

            sent_any_for: Dict[str, bool] = {}
            if text:
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                # process newest first
                for r in routes:
                    tribe = r["tribe"]
                    webhook = r["webhook"]
                    thread_id = r.get("thread_id")

                    sent = 0
                    for ln in reversed(lines):
                        if tribe.lower() not in ln.lower():
                            continue
                        formatted = format_day_time_who_what(ln)
                        if not formatted:
                            continue

                        key = line_hash_key(tribe, formatted)
                        if key in _seen_line_hashes:
                            continue

                        color = pick_color(formatted)
                        await send_log_embed(session, webhook, thread_id, formatted, color)
                        _seen_line_hashes[key] = now
                        _last_activity_ts_by_tribe[tribe.lower()] = now
                        sent_any_for[tribe.lower()] = True
                        sent += 1

                        if sent >= MAX_SEND_PER_POLL:
                            break

            # Heartbeat only if no activity for 60 minutes
            hb_interval = TRIBE_HEARTBEAT_MINUTES * 60
            for r in routes:
                tribe = r["tribe"]
                webhook = r["webhook"]
                thread_id = r.get("thread_id")

                tkey = tribe.lower()
                last_act = _last_activity_ts_by_tribe.get(tkey, 0.0)
                last_hb = _last_heartbeat_ts_by_tribe.get(tkey, 0.0)

                if sent_any_for.get(tkey):
                    continue

                if (now - last_act) >= hb_interval and (now - last_hb) >= hb_interval:
                    await send_log_embed(
                        session,
                        webhook,
                        thread_id,
                        f"⏱️ No new logs since last check. (Tribe: {tribe})",
                        0x95A5A6
                    )
                    _last_heartbeat_ts_by_tribe[tkey] = now
                    print(f"Heartbeat sent for {tribe}")

            # keep dedupe map from growing forever
            # drop entries older than 24h
            cutoff = now - 24 * 3600
            for k, ts in list(_seen_line_hashes.items()):
                if ts < cutoff:
                    _seen_line_hashes.pop(k, None)

            await asyncio.sleep(TRIBE_POLL_SECONDS)

# =========================================================
# PERMISSIONS
# =========================================================
def is_admin(interaction: discord.Interaction) -> bool:
    try:
        return any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles)
    except Exception:
        return False

# =========================================================
# COMMANDS
# =========================================================
@tree.command(name="settime", guild=discord.Object(id=GUILD_ID))
async def settime(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
    if not is_admin(i):
        await i.response.send_message("❌ No permission", ephemeral=True)
        return

    if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await i.response.send_message("❌ Invalid values.", ephemeral=True)
        return

    global state
    state = {"epoch": time.time(), "year": int(year), "day": int(day), "hour": int(hour), "minute": int(minute)}
    save_state(state)
    await i.response.send_message("✅ Time set", ephemeral=True)

@tree.command(name="status", guild=discord.Object(id=GUILD_ID))
async def status(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        emoji, count, _online = await update_players_embed(session)
    await i.followup.send(f"{emoji} **Solunaris** — {count}/{PLAYER_CAP} players", ephemeral=True)

@tree.command(name="linktribelog", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    tribe="Exact tribe name as it appears in logs (e.g. Valkyrie)",
    webhook="Discord webhook URL (forum parent webhook URL)",
    thread_id="Forum thread ID (required for forum channels)"
)
async def linktribelog(i: discord.Interaction, tribe: str, webhook: str, thread_id: str):
    if not is_admin(i):
        await i.response.send_message("❌ You must be a Discord Admin to use this.", ephemeral=True)
        return

    tribe = tribe.strip()
    webhook = webhook.strip()
    thread_id = thread_id.strip()

    if not tribe or not webhook or not thread_id:
        await i.response.send_message("❌ Missing tribe/webhook/thread_id", ephemeral=True)
        return

    route = {"tribe": tribe, "webhook": webhook, "thread_id": thread_id}
    upsert_route(route)

    await i.response.send_message(
        f"✅ Linked tribe **{tribe}** → forum thread **{thread_id}**",
        ephemeral=True
    )
    print(f"Linked tribe route: {route}")

# =========================================================
# START + IMPORTANT: HARD SYNC STEP (THIS IS THE FIX)
# =========================================================
@client.event
async def on_ready():
    print("🔁 Syncing application commands...")
    guild = discord.Object(id=GUILD_ID)

    try:
        synced = await tree.sync(guild=guild)
        print(f"✅ Synced {len(synced)} guild commands")
        for cmd in synced:
            print(f"  ↳ /{cmd.name}")
    except Exception as e:
        print(f"❌ Command sync failed: {e}")

    # Start loops once
    client.loop.create_task(time_loop())
    client.loop.create_task(status_loop())
    client.loop.create_task(tribe_logs_loop())

    print("✅ Combined Tradewinds bot online")

client.run(DISCORD_TOKEN)