import os
import json
import time
import re
import asyncio
import hashlib
import aiohttp
import discord
from discord import app_commands

# =========================
# ENV
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "0") or "0")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

TRIBE_ROUTES_ENV = os.getenv("TRIBE_ROUTES", "")

# =========================
# CONSTANTS
# =========================
ADMIN_ROLE_ID = 1439069787207766076  # Discord Admin role

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "10"))
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "60"))

STATE_FILE = "tribe_routes.json"
DEDUPE_FILE = "dedupe_state.json"

MAX_SEND_PER_POLL = int(os.getenv("MAX_SEND_PER_POLL", "25"))  # avoid webhook rate limits

# =========================
# VALIDATION
# =========================
missing = []
for k in ["DISCORD_TOKEN", "GUILD_ID", "RCON_HOST", "RCON_PORT", "RCON_PASSWORD"]:
    if not os.getenv(k):
        missing.append(k)
if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

# =========================
# DISCORD SETUP
# =========================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =========================
# STORAGE
# =========================
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

# routes: list[{tribe, webhook_url, thread_id}]
routes = load_json(STATE_FILE, [])

# seed from env if present (only if file is empty)
if (not routes) and TRIBE_ROUTES_ENV.strip():
    try:
        parsed = json.loads(TRIBE_ROUTES_ENV)
        if isinstance(parsed, list):
            routes = parsed
            save_json(STATE_FILE, routes)
    except Exception as e:
        raise RuntimeError(f"TRIBE_ROUTES is not valid JSON: {e}")

if not routes:
    print("⚠️ No tribe routes configured yet. Use /linktribelog to add one.")

print("Routing tribes:", ", ".join(r.get("tribe", "?") for r in routes) or "(none)")

# dedupe: dict key -> last_hash
dedupe = load_json(DEDUPE_FILE, {})

# heartbeat: only send if no activity
last_activity_ts = {}  # tribe -> epoch seconds

# =========================
# RCON (minimal)
# =========================
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
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout
    )
    try:
        # auth
        writer.write(_rcon_make_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        _ = await asyncio.wait_for(reader.read(4096), timeout=timeout)

        # cmd
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

# =========================
# LOG PARSING
# =========================
DAYLINE_RE = re.compile(r"(Day\s+\d+,\s+\d{1,2}:\d{2}:\d{2}:\s*)(.*)")

# remove RichColor and similar tags
RICH_TAG_RE = re.compile(r"<\/?RichColor[^>]*>", re.IGNORECASE)

# also strip stray markup brackets
ANGLE_GARBAGE_RE = re.compile(r"</?[^>]+>")

def clean_text(s: str) -> str:
    s = s.replace("\r", "").strip()

    # strip RichColor tags
    s = RICH_TAG_RE.sub("", s).strip()

    # strip any other angle bracket tags
    s = ANGLE_GARBAGE_RE.sub("", s).strip()

    # remove trailing junk like "!)" or "'))" etc
    s = s.rstrip("! )>'\"")

    # collapse extra spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s

def extract_day_line(raw_line: str) -> str | None:
    """
    Return ONLY: "Day X, HH:MM:SS: <who/what...>"
    """
    raw_line = raw_line.strip()
    m = DAYLINE_RE.search(raw_line)
    if not m:
        return None
    prefix = m.group(1).strip()  # "Day 233, 17:45:33:"
    rest = m.group(2).strip()
    out = f"{prefix} {rest}"
    return clean_text(out)

def classify_color(text: str) -> int:
    lower = text.lower()

    # Red - Killed / Died / Death / Destroyed
    if any(w in lower for w in [" killed", " was killed", " died", " death", " destroyed", " starved to death"]):
        return 0xE74C3C  # red

    # Yellow - Demolished (and unclaimed per your latest change)
    if "demolish" in lower or "unclaimed" in lower:
        return 0xF1C40F  # yellow

    # Purple - Claimed
    if "claimed" in lower:
        return 0x9B59B6  # purple

    # Green - Tamed
    if "tamed" in lower or "taming" in lower:
        return 0x2ECC71  # green

    # Light blue - Alliance
    if "alliance" in lower:
        return 0x5DADE2  # light blue

    # White - anything else (eg froze)
    return 0xFFFFFF

def hash_line(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8", errors="ignore")).hexdigest()

# =========================
# DISCORD WEBHOOK SEND
# =========================
async def send_to_webhook(webhook_url: str, thread_id: str | None, content: str, color: int):
    payload = {
        "embeds": [
            {
                "description": content,
                "color": color,
            }
        ]
    }

    params = {}
    if thread_id:
        params["thread_id"] = thread_id

    async with aiohttp.ClientSession() as session:
        async with session.post(webhook_url, params=params, json=payload) as r:
            if r.status >= 400:
                try:
                    txt = await r.text()
                except Exception:
                    txt = "<no body>"
                raise RuntimeError(f"Discord webhook error {r.status}: {txt}")

# =========================
# ROUTES HELPERS
# =========================
def get_route_for_tribe(tribe_name: str):
    for r in routes:
        if str(r.get("tribe", "")).lower() == tribe_name.lower():
            return r
    return None

def upsert_route(tribe: str, webhook_url: str, thread_id: str | None):
    existing = get_route_for_tribe(tribe)
    if existing:
        existing["webhook_url"] = webhook_url
        existing["thread_id"] = thread_id
    else:
        routes.append({"tribe": tribe, "webhook_url": webhook_url, "thread_id": thread_id})
    save_json(STATE_FILE, routes)

# =========================
# MAIN POLLER
# =========================
async def poll_once():
    """
    Pull GetGameLog, then for each route:
    - filter to lines containing "Tribe <name>" OR "(<name>)" OR plain "<name>" (fallback)
    - extract Day line and send only new ones
    """
    global dedupe

    text = await rcon_command("GetGameLog", timeout=10.0)
    if not text:
        return  # don't mark activity, just no data

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return

    # We walk from bottom to top and pick newest lines first
    lines_rev = list(reversed(lines))

    sent_count_total = 0

    for route in routes:
        tribe = str(route.get("tribe", "")).strip()
        if not tribe:
            continue

        webhook_url = str(route.get("webhook_url", "")).strip()
        thread_id = str(route.get("thread_id", "")).strip() or None
        if not webhook_url:
            continue

        # find matching lines
        matched = []
        tribe_lower = tribe.lower()

        for ln in lines_rev:
            lnl = ln.lower()
            # matches many ASA log formats:
            # "Tribe Valkyrie," OR "(Valkyrie)" OR "Tribe: Valkyrie" etc
            if (f"tribe {tribe_lower}" in lnl) or (f"({tribe_lower})" in lnl) or (tribe_lower in lnl):
                extracted = extract_day_line(ln)
                if extracted:
                    matched.append(extracted)
            if len(matched) >= 200:
                break

        if not matched:
            continue

        # Send newest-first, but dedupe
        key = f"tribe:{tribe_lower}"
        last_hashes = set(dedupe.get(key, [])) if isinstance(dedupe.get(key), list) else set()

        to_send = []
        for entry in matched:
            h = hash_line(entry)
            if h not in last_hashes:
                to_send.append((entry, h))
            if len(to_send) >= MAX_SEND_PER_POLL:
                break

        if not to_send:
            continue

        # send oldest->newest for readability
        to_send.reverse()

        for entry, h in to_send:
            color = classify_color(entry)
            await send_to_webhook(webhook_url, thread_id, entry, color)
            last_activity_ts[tribe_lower] = time.time()
            last_hashes.add(h)
            sent_count_total += 1

            # keep dedupe list bounded
            if len(last_hashes) > 500:
                last_hashes = set(list(last_hashes)[-400:])

        dedupe[key] = list(last_hashes)

    if sent_count_total:
        save_json(DEDUPE_FILE, dedupe)

async def heartbeat_loop():
    """
    Every HEARTBEAT_MINUTES, if a tribe has had no activity since last heartbeat window,
    send: "No new logs since last check. (Tribe: X)"
    """
    while True:
        await asyncio.sleep(HEARTBEAT_MINUTES * 60)

        now = time.time()
        for route in routes:
            tribe = str(route.get("tribe", "")).strip()
            if not tribe:
                continue
            tribe_lower = tribe.lower()
            last = last_activity_ts.get(tribe_lower, 0)

            # Only send heartbeat if no activity in the last window
            if last and (now - last) < (HEARTBEAT_MINUTES * 60):
                continue

            webhook_url = str(route.get("webhook_url", "")).strip()
            thread_id = str(route.get("thread_id", "")).strip() or None
            if not webhook_url:
                continue

            msg = f"⏱️ No new logs since last check. (Tribe: {tribe})"
            try:
                await send_to_webhook(webhook_url, thread_id, msg, 0x95A5A6)
                print(f"Heartbeat sent for {tribe}")
            except Exception as e:
                print(f"Heartbeat error for {tribe}: {e}")

async def main_loop():
    while True:
        try:
            if routes:
                await poll_once()
        except Exception as e:
            print(f"Poll error: {e}")
        await asyncio.sleep(POLL_SECONDS)

# =========================
# COMMANDS
# =========================
def user_is_admin(member: discord.Member) -> bool:
    return any(getattr(r, "id", None) == ADMIN_ROLE_ID for r in getattr(member, "roles", []))

@tree.command(name="linktribelog", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    tribe="Exact tribe name (case-insensitive). Example: Valkyrie",
    webhook_url="Discord webhook URL (WITHOUT ?thread_id).",
    thread_id="Forum thread ID (optional but required for forum webhooks).",
)
async def linktribelog(
    i: discord.Interaction,
    tribe: str,
    webhook_url: str,
    thread_id: str = ""
):
    if not isinstance(i.user, discord.Member) or not user_is_admin(i.user):
        await i.response.send_message("❌ No permission (Discord Admin only).", ephemeral=True)
        return

    tribe = tribe.strip()
    webhook_url = webhook_url.strip()
    thread_id = thread_id.strip() or None

    if not tribe or not webhook_url:
        await i.response.send_message("❌ tribe and webhook_url are required.", ephemeral=True)
        return

    # store
    upsert_route(tribe, webhook_url, thread_id)

    await i.response.send_message(
        f"✅ Linked tribe **{tribe}** → webhook saved"
        + (f" (thread_id={thread_id})" if thread_id else ""),
        ephemeral=True
    )

@tree.command(name="routes", guild=discord.Object(id=GUILD_ID))
async def routes_cmd(i: discord.Interaction):
    if not isinstance(i.user, discord.Member) or not user_is_admin(i.user):
        await i.response.send_message("❌ No permission (Discord Admin only).", ephemeral=True)
        return

    if not routes:
        await i.response.send_message("No routes configured.", ephemeral=True)
        return

    lines = []
    for r in routes:
        lines.append(f"- {r.get('tribe')} | thread_id={r.get('thread_id') or 'none'}")
    await i.response.send_message("\n".join(lines), ephemeral=True)

# =========================
# STARTUP (IMPORTANT PART)
# =========================
@client.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)

    # ✅ STEP 1: FORCE drop cached slash commands + double sync
    tree.clear_commands(guild=guild)
    await tree.sync(guild=guild)
    await tree.sync(guild=guild)

    client.loop.create_task(main_loop())
    client.loop.create_task(heartbeat_loop())

    print("✅ Combined Tradewinds bot online")

client.run(DISCORD_TOKEN)