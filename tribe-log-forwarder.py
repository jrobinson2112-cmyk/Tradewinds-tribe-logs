import os
import time
import json
import asyncio
import aiohttp
import discord
from discord import app_commands
import re
import hashlib
from typing import Optional, Dict, Any, List, Tuple

# =====================
# ENV
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

TIME_WEBHOOK_URL = os.getenv("TIME_WEBHOOK_URL")  # time webhook
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")  # players webhook

NITRADO_TOKEN = os.getenv("NITRADO_TOKEN")
NITRADO_SERVICE_ID = os.getenv("NITRADO_SERVICE_ID")

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = os.getenv("RCON_PORT")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

TRIBE_ROUTES_RAW = os.getenv("TRIBE_ROUTES")  # JSON array

required_keys = [
    "DISCORD_TOKEN",
    "TIME_WEBHOOK_URL",
    "PLAYERS_WEBHOOK_URL",
    "NITRADO_TOKEN",
    "NITRADO_SERVICE_ID",
    "RCON_HOST",
    "RCON_PORT",
    "RCON_PASSWORD",
    "TRIBE_ROUTES",
]

missing = [k for k in required_keys if not os.getenv(k)]
if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

RCON_PORT = int(RCON_PORT)

try:
    TRIBE_ROUTES = json.loads(TRIBE_ROUTES_RAW)
except Exception:
    TRIBE_ROUTES = None

if not isinstance(TRIBE_ROUTES, list) or len(TRIBE_ROUTES) < 1:
    raise RuntimeError("TRIBE_ROUTES must be a JSON array with at least one route.")

for i, route in enumerate(TRIBE_ROUTES):
    if not isinstance(route, dict):
        raise RuntimeError(f"TRIBE_ROUTES[{i}] must be an object.")
    if not route.get("tribe") or not route.get("webhook"):
        raise RuntimeError(f"TRIBE_ROUTES[{i}] must include 'tribe' and 'webhook'.")
    # thread_id optional, but recommended for forum webhooks
    if "thread_id" in route and route["thread_id"] is not None:
        route["thread_id"] = str(route["thread_id"])

print("Routing tribes:", ", ".join(r["tribe"] for r in TRIBE_ROUTES))

# =====================
# CONSTANTS / SETTINGS
# =====================
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076
STATUS_VC_ID = 1456615806887657606
ANNOUNCE_CHANNEL_ID = 1430388267446042666
PLAYER_CAP = 42

# Your current time model SPMs (keep as-is)
DAY_SPM = 4.7666667
NIGHT_SPM = 4.045
SUNRISE = 5 * 60 + 30
SUNSET  = 17 * 60 + 30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

# Embed colors for tribe logs
COLOR_RED = 0xE74C3C        # killed / died / death / destroyed
COLOR_YELLOW = 0xF1C40F     # demolished AND unclaimed
COLOR_PURPLE = 0x9B59B6     # claimed
COLOR_GREEN = 0x2ECC71      # tamed
COLOR_LIGHTBLUE = 0x5DADE2  # alliance
COLOR_WHITE = 0xFFFFFF      # anything else (froze etc.)

STATE_FILE = "state.json"
DEDUP_FILE = "dedupe.json"

# Polling
STATUS_POLL_SECONDS = 15
GETGAMELOG_POLL_SECONDS = 10  # logs bot cadence

# VC rename rate-limit (prevents Discord 429s)
VC_EDIT_MIN_SECONDS = 300  # 5 minutes
_last_vc_edit_ts = 0.0
_last_vc_name = None

# Time webhook updates only on round 10 minutes
TIME_UPDATE_STEP_MINUTES = 10

# Sync clock from GetGameLog
GAMELOG_SYNC_SECONDS = 120          # how often to attempt auto-sync
SYNC_DRIFT_MINUTES = 2              # only correct if drift >= this many in-game minutes
SYNC_COOLDOWN_SECONDS = 600         # don't resync more than once per 10 minutes

# Heartbeat for tribe threads/webhooks
HEARTBEAT_IDLE_SECONDS = 60 * 60    # 60 minutes
HEARTBEAT_TEXT = "⏱️ No new logs since last check."

# Prevent spam if backlog exists: on first run, do NOT forward history (you can change)
FORWARD_BACKLOG_ON_BOOT = False
MAX_SEND_PER_POLL_PER_TRIBE = 10

# =====================
# DISCORD SETUP
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =====================
# STATE FILES
# =====================
def load_json(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None

def save_json(path: str, obj):
    with open(path, "w") as f:
        json.dump(obj, f)

state = load_json(STATE_FILE)  # {"epoch","year","day","hour","minute"} etc.

# Dedupe: per tribe -> set of hashes (stored as list on disk)
dedupe = load_json(DEDUP_FILE) or {}
# runtime: last activity time per tribe route key
last_activity_ts: Dict[str, float] = {}
# runtime: last heartbeat time per tribe route key
last_heartbeat_ts: Dict[str, float] = {}
# runtime: first run marker for tribe forwarder
_first_gamelog_poll = True

# webhook message ids for upsert embeds
message_ids = {
    "time": None,
    "players": None,
}

last_announced_day = None
_last_sync_ts = 0.0

# =====================
# TIME LOGIC
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
    """
    Returns:
      minute_of_day, day, year, seconds_into_current_minute, cur_spm
    """
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

def seconds_until_next_round_step(minute_of_day: int, day: int, year: int, seconds_into_minute: float, step: int):
    m = minute_of_day
    mod = m % step
    minutes_to_boundary = (step - mod) if mod != 0 else step

    cur_spm = spm(m)
    remaining_in_current_minute = max(0.0, cur_spm - seconds_into_minute)
    total = remaining_in_current_minute

    m2 = m
    d2, y2 = day, year
    for _ in range(minutes_to_boundary - 1):
        m2, d2, y2 = _advance_one_minute(m2, d2, y2)
        total += spm(m2)

    return max(0.5, total)

# =====================
# NITRADO STATUS (COUNT)
# =====================
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

# =====================
# RCON (robust-ish decode)
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

def _decode_best_effort(b: bytes) -> str:
    # Try UTF-8 strict; if fails, fall back to latin-1 to preserve Ø/extended chars
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1", errors="replace")

async def rcon_command(command: str, timeout: float = 6.0) -> str:
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
            if i + size > len(data) or size < 10:
                break
            pkt = data[i:i+size]
            i += size
            body = pkt[8:-2]
            txt = _decode_best_effort(body)
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

# =====================
# WEBHOOK HELPERS
# =====================
async def upsert_webhook_embed(session: aiohttp.ClientSession, url: str, key: str, embed: dict):
    mid = message_ids.get(key)
    if mid:
        async with session.patch(f"{url}/messages/{mid}", json={"embeds": [embed]}) as r:
            if r.status == 404:
                message_ids[key] = None
                return await upsert_webhook_embed(session, url, key, embed)
        return

    async with session.post(url + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json()
        message_ids[key] = data.get("id")

def _route_key(route: dict) -> str:
    # stable identity for heartbeats/dedupe buckets
    return f"{route.get('tribe','')}|{route.get('webhook','')}|{route.get('thread_id','')}"

async def send_webhook_message(session: aiohttp.ClientSession, route: dict, content: Optional[str] = None, embed: Optional[dict] = None):
    """
    Posts to webhook. If it's a forum webhook, Discord requires thread_id OR thread_name.
    We'll use thread_id if provided.
    """
    url = route["webhook"]
    params = {"wait": "true"}

    if route.get("thread_id"):
        # Correct param name is thread_id (not Thread)
        params["thread_id"] = str(route["thread_id"])

    payload: Dict[str, Any] = {}
    if content is not None:
        payload["content"] = content
    if embed is not None:
        payload["embeds"] = [embed]

    async with session.post(url, params=params, json=payload) as r:
        if r.status >= 400:
            try:
                txt = await r.text()
            except Exception:
                txt = "(no body)"
            raise RuntimeError(f"Discord webhook error {r.status}: {txt}")

# =====================
# PLAYERS / VC
# =====================
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

    await upsert_webhook_embed(session, PLAYERS_WEBHOOK_URL, "players", embed)
    return emoji, count, online

# =====================
# GAMELOG PARSING (time + tribe forwarding)
# =====================
# Finds the in-game stamp anywhere in a line:
_DAYTIME_RE = re.compile(r"Day\s+(\d+),\s+(\d{1,2}):(\d{2}):(\d{2})\s*:")

# Removes richcolor tags like <RichColor Color="1, 1, 0, 1">
_RICHCOLOR_RE = re.compile(r"<\s*RichColor\b[^>]*>\s*", re.IGNORECASE)

def color_for_line(clean: str) -> int:
    low = clean.lower()

    # red: killed / died / death / destroyed
    if ("killed" in low) or ("died" in low) or ("death" in low) or ("destroyed" in low) or ("starved to death" in low):
        return COLOR_RED

    # yellow: demolished + unclaimed
    if ("demolished" in low) or ("unclaimed" in low):
        return COLOR_YELLOW

    # purple: claimed (NOT unclaimed, handled above)
    if ("claimed" in low):
        return COLOR_PURPLE

    # green: tamed
    if ("tamed" in low) or ("taming" in low):
        return COLOR_GREEN

    # light blue: alliance
    if ("alliance" in low):
        return COLOR_LIGHTBLUE

    # white: anything else (froze etc.)
    return COLOR_WHITE

def extract_daytime(line: str) -> Optional[Tuple[int,int,int,int]]:
    m = _DAYTIME_RE.search(line)
    if not m:
        return None
    d = int(m.group(1))
    h = int(m.group(2))
    mi = int(m.group(3))
    s = int(m.group(4))
    return d,h,mi,s

def clean_to_day_time_who_what(line: str) -> Optional[str]:
    """
    Converts any line containing:
      Day X, HH:MM:SS: <message>
    into:
      Day X, HH:MM:SS - <message>
    Strips RichColor tags + trims trailing noise like !)' etc.
    Ensures we output ONLY: Day, Time, Who, What.
    """
    if not line:
        return None

    # Remove RichColor tags anywhere
    line = _RICHCOLOR_RE.sub("", line)

    # If line has extra prefix like "2026....: Tribe ...: Day X, HH:MM:SS: ..."
    dt = extract_daytime(line)
    if not dt:
        return None

    # Keep from "Day ..."
    idx = line.lower().find("day ")
    if idx >= 0:
        line = line[idx:].strip()

    # Turn first ": " after HH:MM:SS into " - "
    # pattern: Day X, HH:MM:SS: message
    line = re.sub(r"(Day\s+\d+,\s+\d{1,2}:\d{2}:\d{2})\s*:\s*", r"\1 - ", line, count=1)

    # Remove trailing junk like </>)  or !)) or !' or ') etc.
    line = line.strip()

    # Remove any lingering markup fragments
    line = line.replace("</>", "").replace("</>)", "").replace("</)", "").replace("<//>", "")

    # Trim trailing punctuation combos but keep closing parenthesis if it's part of creature name etc.
    # We'll remove only repeated trailing ! and quotes.
    line = re.sub(r"[!]+[)]*$", "", line).strip()
    line = re.sub(r"[']+[)]*$", "", line).strip()
    line = re.sub(r"[!']+$", "", line).strip()

    return line

def hash_line(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

def ensure_dedupe_bucket(route_key: str):
    if route_key not in dedupe or not isinstance(dedupe.get(route_key), list):
        dedupe[route_key] = []

def dedupe_has(route_key: str, h: str) -> bool:
    ensure_dedupe_bucket(route_key)
    return h in dedupe[route_key]

def dedupe_add(route_key: str, h: str):
    ensure_dedupe_bucket(route_key)
    dedupe[route_key].append(h)
    # keep last N
    if len(dedupe[route_key]) > 5000:
        dedupe[route_key] = dedupe[route_key][-5000:]

def apply_gamelog_sync(parsed_day: int, parsed_hour: int, parsed_minute: int) -> Tuple[bool, str]:
    global state
    if not state:
        return False, "No state set"

    details = calculate_time_details()
    if not details:
        return False, "No calculated time details"

    cur_minute_of_day, cur_day, cur_year, seconds_into_minute, cur_spm = details
    target_minute_of_day = parsed_hour * 60 + parsed_minute

    # day wrap handling (same-year assumption, good enough for drift correction)
    day_diff = parsed_day - cur_day
    if day_diff > 180:
        day_diff -= 365
    elif day_diff < -180:
        day_diff += 365

    minute_diff = (day_diff * 1440) + (target_minute_of_day - cur_minute_of_day)

    # clamp to a sane range
    while minute_diff > 720:
        minute_diff -= 1440
    while minute_diff < -720:
        minute_diff += 1440

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {minute_diff} min < threshold"

    # shift epoch so our calculated time becomes the parsed time "now"
    real_seconds_shift = minute_diff * spm(cur_minute_of_day)
    state["epoch"] = float(state["epoch"]) - real_seconds_shift
    state["day"] = int(parsed_day)
    state["hour"] = int(parsed_hour)
    state["minute"] = int(parsed_minute)
    save_json(STATE_FILE, state)

    return True, f"Synced (drift {minute_diff} min)"

# =====================
# LOOPS
# =====================
async def time_loop():
    global last_announced_day
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            details = calculate_time_details()
            if not details:
                await asyncio.sleep(5)
                continue

            minute_of_day, day, year, seconds_into_minute, cur_spm = details

            if (minute_of_day % TIME_UPDATE_STEP_MINUTES) == 0:
                embed = build_time_embed(minute_of_day, day, year)
                await upsert_webhook_embed(session, TIME_WEBHOOK_URL, "time", embed)

                absolute_day = year * 365 + day
                if last_announced_day is None:
                    last_announced_day = absolute_day
                elif absolute_day > last_announced_day:
                    ch = client.get_channel(ANNOUNCE_CHANNEL_ID)
                    if ch:
                        await ch.send(f"📅 **New Solunaris Day** — Day **{day}**, Year **{year}**")
                    last_announced_day = absolute_day

            sleep_for = seconds_until_next_round_step(
                minute_of_day, day, year, seconds_into_minute, TIME_UPDATE_STEP_MINUTES
            )
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

async def gamelog_poll_and_forward_loop():
    """
    Single owner of GetGameLog:
      - forward tribe logs to their route webhook/thread
      - update last seen in-game Day/time for syncing
      - send heartbeat if idle
    """
    global _first_gamelog_poll
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                text = await rcon_command("GetGameLog", timeout=10.0)
                if not text:
                    # no output - still heartbeat if needed
                    await maybe_send_heartbeats(session)
                    await asyncio.sleep(GETGAMELOG_POLL_SECONDS)
                    continue

                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

                # First boot: optionally seed dedupe from current buffer to avoid spam
                if _first_gamelog_poll:
                    if not FORWARD_BACKLOG_ON_BOOT:
                        for route in TRIBE_ROUTES:
                            rk = _route_key(route)
                            for ln in lines:
                                cleaned = clean_to_day_time_who_what(ln)
                                if not cleaned:
                                    continue
                                if route["tribe"].lower() not in ln.lower():
                                    continue
                                dedupe_add(rk, hash_line(cleaned))
                        save_json(DEDUP_FILE, dedupe)
                        print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")
                    else:
                        print("First run: backlog forwarding enabled.")
                    _first_gamelog_poll = False

                # Forward new tribe lines
                for route in TRIBE_ROUTES:
                    rk = _route_key(route)
                    sent = 0

                    # Walk from oldest to newest (stable)
                    for ln in lines:
                        if route["tribe"].lower() not in ln.lower():
                            continue
                        cleaned = clean_to_day_time_who_what(ln)
                        if not cleaned:
                            continue

                        h = hash_line(cleaned)
                        if dedupe_has(rk, h):
                            continue

                        # Build embed
                        embed = {
                            "description": cleaned,
                            "color": color_for_line(cleaned),
                        }

                        # Send
                        await send_webhook_message(session, route, embed=embed)

                        dedupe_add(rk, h)
                        last_activity_ts[rk] = time.time()
                        sent += 1
                        if sent >= MAX_SEND_PER_POLL_PER_TRIBE:
                            break

                save_json(DEDUP_FILE, dedupe)

                # Heartbeats
                await maybe_send_heartbeats(session)

            except Exception as e:
                print(f"[GetGameLog loop] Error: {e}")

            await asyncio.sleep(GETGAMELOG_POLL_SECONDS)

async def maybe_send_heartbeats(session: aiohttp.ClientSession):
    now = time.time()
    for route in TRIBE_ROUTES:
        rk = _route_key(route)
        last_act = last_activity_ts.get(rk, 0.0)
        last_hb = last_heartbeat_ts.get(rk, 0.0)

        # only heartbeat if idle >= 60 mins
        if (now - last_act) >= HEARTBEAT_IDLE_SECONDS:
            # and not more often than every 60 mins
            if (now - last_hb) >= HEARTBEAT_IDLE_SECONDS:
                try:
                    await send_webhook_message(session, route, content=f"{HEARTBEAT_TEXT} (Tribe: {route['tribe']})")
                    last_heartbeat_ts[rk] = now
                    print(f"Heartbeat sent for {route['tribe']}")
                except Exception as e:
                    print(f"Heartbeat error ({route['tribe']}): {e}")

async def gamelog_sync_loop():
    """
    Uses GetGameLog output (same RCON owner) to auto-correct time drift.
    NOTE: We do NOT call GetGameLog here. We piggyback on polling by re-calling
    GetGameLog at a lower frequency to reduce load, but still within same bot.
    """
    global _last_sync_ts
    await client.wait_until_ready()

    while True:
        try:
            if not state:
                await asyncio.sleep(GAMELOG_SYNC_SECONDS)
                continue

            now = time.time()
            if (now - _last_sync_ts) < SYNC_COOLDOWN_SECONDS:
                await asyncio.sleep(GAMELOG_SYNC_SECONDS)
                continue

            log_text = await rcon_command("GetGameLog", timeout=10.0)
            if not log_text:
                print("GameLog sync: GetGameLog returned empty output.")
                await asyncio.sleep(GAMELOG_SYNC_SECONDS)
                continue

            # Find most recent parsable Day/time line from the bottom (accept ANY line)
            lines = [ln.strip() for ln in log_text.splitlines() if ln.strip()]
            parsed = None
            for ln in reversed(lines):
                dt = extract_daytime(ln)
                if dt:
                    parsed = dt
                    break

            if not parsed:
                print("GameLog sync: No parsable 'Day X, HH:MM:SS:' line found.")
                await asyncio.sleep(GAMELOG_SYNC_SECONDS)
                continue

            d, h, mi, s = parsed
            changed, msg = apply_gamelog_sync(d, h, mi)
            print("GameLog sync:", msg)
            if changed:
                _last_sync_ts = time.time()

        except Exception as e:
            print(f"GameLog sync error: {e}")

        await asyncio.sleep(GAMELOG_SYNC_SECONDS)

# =====================
# COMMANDS
# =====================
@tree.command(name="settime", guild=discord.Object(id=GUILD_ID))
async def settime(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
    if not any(r.id == ADMIN_ROLE_ID for r in i.user.roles):
        await i.response.send_message("❌ No permission", ephemeral=True)
        return

    if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await i.response.send_message("❌ Invalid values.", ephemeral=True)
        return

    global state
    state = {
        "epoch": time.time(),
        "year": int(year),
        "day": int(day),
        "hour": int(hour),
        "minute": int(minute),
    }
    save_json(STATE_FILE, state)
    await i.response.send_message("✅ Time set", ephemeral=True)

@tree.command(name="status", guild=discord.Object(id=GUILD_ID))
async def status(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        emoji, count, online = await update_players_embed(session)
    await i.followup.send(f"{emoji} **Solunaris** — {count}/{PLAYER_CAP} players", ephemeral=True)

@tree.command(name="sync", guild=discord.Object(id=GUILD_ID))
async def sync_time(i: discord.Interaction):
    """Force an immediate GetGameLog poll + time sync."""
    await i.response.defer(ephemeral=True)

    if not state:
        await i.followup.send("❌ Time not set. Use /settime first.", ephemeral=True)
        return

    try:
        log_text = await rcon_command("GetGameLog", timeout=10.0)
        if not log_text:
            await i.followup.send("❌ GetGameLog returned empty output.", ephemeral=True)
            return

        lines = [ln.strip() for ln in log_text.splitlines() if ln.strip()]
        parsed = None
        for ln in reversed(lines):
            dt = extract_daytime(ln)
            if dt:
                parsed = dt
                break

        if not parsed:
            await i.followup.send("❌ No Day/Time found in GetGameLog.", ephemeral=True)
            return

        d, h, mi, s = parsed
        changed, msg = apply_gamelog_sync(d, h, mi)
        if changed:
            await i.followup.send(f"✅ {msg} (Day {d} {h:02d}:{mi:02d})", ephemeral=True)
        else:
            await i.followup.send(f"ℹ️ {msg} (Day {d} {h:02d}:{mi:02d})", ephemeral=True)

    except Exception as e:
        await i.followup.send(f"❌ Sync error: {e}", ephemeral=True)

@tree.command(name="debuggamelog", guild=discord.Object(id=GUILD_ID))
async def debuggamelog(i: discord.Interaction):
    """Show whether GetGameLog is returning data and the last few lines."""
    await i.response.defer(ephemeral=True)
    try:
        log_text = await rcon_command("GetGameLog", timeout=10.0)
        if not log_text:
            await i.followup.send("❌ GetGameLog returned empty output.", ephemeral=True)
            return

        lines = [ln.strip() for ln in log_text.splitlines() if ln.strip()]
        tail = "\n".join(lines[-10:])
        await i.followup.send(f"```text\n{tail}\n```", ephemeral=True)

    except Exception as e:
        await i.followup.send(f"❌ Debug error: {e}", ephemeral=True)

# =====================
# START
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    client.loop.create_task(time_loop())
    client.loop.create_task(status_loop())
    client.loop.create_task(gamelog_poll_and_forward_loop())
    client.loop.create_task(gamelog_sync_loop())
    print("✅ Combined Tradewinds bot online")

client.run(DISCORD_TOKEN)