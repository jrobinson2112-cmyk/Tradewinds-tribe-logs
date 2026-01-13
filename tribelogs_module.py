import os
import re
import json
import time
import asyncio
import aiohttp
import discord
from discord import app_commands
from urllib.parse import urlparse, urlunparse, parse_qs

# =========================
# ENV / CONFIG
# =========================
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "27020"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

POLL_SECONDS = float(os.getenv("POLL_SECONDS", "10"))
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "60"))

# If 1, sends backlog on boot. If 0 (default), seeds dedupe and starts live.
BACKLOG_ON_START = os.getenv("BACKLOG_ON_START", "0").strip() == "1"

# Persist routes here (ensure your Railway volume includes /data)
ROUTES_FILE = os.getenv("TRIBE_ROUTES_FILE", "/data/tribe_routes.json")

# Optional initial routes via env (JSON array). Can be empty.
TRIBE_ROUTES_ENV = os.getenv("TRIBE_ROUTES", "").strip()

required = ["RCON_HOST", "RCON_PORT", "RCON_PASSWORD"]
missing = [k for k in required if not os.getenv(k)]
if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

# =========================
# DISCORD EMBED COLORS
# =========================
COLOR_RED = 0xE74C3C        # killed/died/death/destroyed
COLOR_YELLOW = 0xF1C40F     # demolished + unclaimed
COLOR_PURPLE = 0x9B59B6     # claimed
COLOR_GREEN = 0x2ECC71      # tamed
COLOR_LIGHTBLUE = 0x5DADE2  # alliance
COLOR_WHITE = 0xFFFFFF      # everything else (froze etc)
COLOR_HEARTBEAT = 0x95A5A6

# =========================
# PARSING HELPERS
# =========================
DAYTIME_RE = re.compile(
    r"Day\s+(?P<day>\d+),\s+(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})\s*:\s*(?P<rest>.+)$",
    re.IGNORECASE,
)
RICH_TAG_RE = re.compile(r"<\/?RichColor[^>]*>", re.IGNORECASE)

def strip_richcolor(s: str) -> str:
    s = RICH_TAG_RE.sub("", s)
    s = s.replace("</>)", "").replace("</)", "").replace("</>", "")
    s = re.sub(r"[!>\)\]]+$", "", s).strip()
    return s

def extract_tribe_line(raw_line: str, tribe_name: str):
    if not raw_line:
        return None
    line = raw_line.strip()
    if not line:
        return None

    if f"Tribe {tribe_name}".lower() not in line.lower():
        return None

    cleaned = strip_richcolor(line)

    m = DAYTIME_RE.search(cleaned)
    if not m:
        return None

    day = int(m.group("day"))
    hh = int(m.group("h"))
    mm = int(m.group("m"))
    ss = int(m.group("s"))
    who_what = m.group("rest").strip()
    return day, hh, mm, ss, who_what

def classify_color(text: str) -> int:
    t = text.lower()
    if any(x in t for x in ["killed", "died", "death", "destroyed"]):
        return COLOR_RED
    if "demolished" in t:
        return COLOR_YELLOW
    if "unclaimed" in t:
        return COLOR_YELLOW
    if "claimed" in t:
        return COLOR_PURPLE
    if "tamed" in t or "taming" in t:
        return COLOR_GREEN
    if "alliance" in t:
        return COLOR_LIGHTBLUE
    return COLOR_WHITE

def format_short(day: int, hh: int, mm: int, ss: int, who_what: str) -> str:
    return f"Day {day}, {hh:02d}:{mm:02d}:{ss:02d} - {who_what}"

# =========================
# WEBHOOK HELPERS
# =========================
def normalize_webhook_base(url: str) -> str:
    p = urlparse(url.strip())
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))

def extract_thread_id_from_url(url: str) -> str | None:
    p = urlparse(url.strip())
    q = parse_qs(p.query)
    if "Thread" in q and q["Thread"]:
        return q["Thread"][0]
    if "thread_id" in q and q["thread_id"]:
        return q["thread_id"][0]
    return None

def is_valid_webhook(url: str) -> bool:
    return isinstance(url, str) and ("discord.com/api/webhooks/" in url)

async def webhook_post(session: aiohttp.ClientSession, webhook_base: str, thread_id: str | None, embed: dict) -> bool:
    """
    Discord webhooks may return:
      - 204 No Content (common)
      - 200 OK with JSON message object (when wait=true)
      - 201 Created
    Treat all as success.
    """
    params = "wait=true"
    if thread_id:
        params += f"&thread_id={thread_id}"
    url = f"{webhook_base}?{params}"

    payload = {"embeds": [embed]}

    for _ in range(6):
        async with session.post(url, json=payload) as r:
            if r.status in (200, 201, 204):
                return True

            # parse for rate limit
            try:
                data = await r.json()
            except Exception:
                data = await r.text()

            if r.status == 429 and isinstance(data, dict) and "retry_after" in data:
                await asyncio.sleep(float(data["retry_after"]) + 0.05)
                continue

            raise RuntimeError(f"Webhook post failed: {r.status} {data}")

    return False

# =========================
# RCON (Minimal Source RCON)
# =========================
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

async def rcon_command(command: str, timeout: float = 10.0) -> str:
    last_err = None
    for attempt in range(3):
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout
            )
            try:
                writer.write(_rcon_make_packet(1, 3, RCON_PASSWORD))
                await writer.drain()
                await asyncio.wait_for(reader.read(4096), timeout=timeout)

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
        except Exception as e:
            last_err = e
            await asyncio.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"RCON error: {last_err}")

# =========================
# ROUTES PERSISTENCE
# =========================
def _ensure_routes_dir():
    d = os.path.dirname(ROUTES_FILE)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def load_routes() -> list[dict]:
    routes = []

    # persisted
    try:
        if os.path.exists(ROUTES_FILE):
            with open(ROUTES_FILE, "r", encoding="utf-8") as f:
                routes = json.load(f) or []
    except Exception:
        routes = []

    # env bootstrap if file empty
    if not routes and TRIBE_ROUTES_ENV:
        try:
            routes = json.loads(TRIBE_ROUTES_ENV)
        except Exception:
            routes = []

    out = []
    for r in routes:
        try:
            tribe = str(r.get("tribe", "")).strip()
            webhook_raw = str(r.get("webhook", "")).strip()
            thread_id = str(r.get("thread_id") or "").strip() or None

            if not thread_id:
                thread_id = extract_thread_id_from_url(webhook_raw)

            webhook = normalize_webhook_base(webhook_raw)

            if tribe and is_valid_webhook(webhook):
                out.append({"tribe": tribe, "webhook": webhook, "thread_id": thread_id})
            else:
                if tribe:
                    print(f"⚠ Skipping invalid route for {tribe}: webhook='{webhook_raw}' thread_id='{thread_id}'")
        except Exception:
            continue

    return out

def save_routes(routes: list[dict]):
    _ensure_routes_dir()
    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(routes, f, ensure_ascii=False, indent=2)

# =========================
# POLLING LOOP
# =========================
_routes: list[dict] = []
_seen: dict[str, set[str]] = {}
_last_activity: dict[str, float] = {}
_last_heartbeat: dict[str, float] = {}
_poll_task: asyncio.Task | None = None

def _fingerprint(s: str) -> str:
    return str(hash(s))

async def _poll_once(session: aiohttp.ClientSession) -> dict[str, int]:
    """
    Returns per-tribe counts of newly forwarded lines.
    """
    sent_counts: dict[str, int] = {}

    _routes_local = load_routes()
    if not _routes_local:
        return sent_counts

    text = await rcon_command("GetGameLog", timeout=12.0)
    lines = text.splitlines()

    for r in _routes_local:
        tribe = r["tribe"]
        webhook = r["webhook"]
        thread_id = r.get("thread_id")

        if not is_valid_webhook(webhook):
            continue

        _seen.setdefault(tribe, set())
        _last_activity.setdefault(tribe, time.time())
        _last_heartbeat.setdefault(tribe, time.time())

        new_msgs: list[tuple[str, int]] = []

        for ln in lines:
            parsed = extract_tribe_line(ln, tribe)
            if not parsed:
                continue

            day, hh, mm, ss, who_what = parsed
            msg = format_short(day, hh, mm, ss, who_what)
            fp = _fingerprint(msg)
            if fp in _seen[tribe]:
                continue

            _seen[tribe].add(fp)
            color = classify_color(who_what)
            new_msgs.append((msg, color))

        if new_msgs:
            for msg, color in new_msgs:
                embed = {"description": msg, "color": color}
                await webhook_post(session, webhook, thread_id, embed)
                await asyncio.sleep(0.35)

            _last_activity[tribe] = time.time()
            sent_counts[tribe] = len(new_msgs)
        else:
            sent_counts.setdefault(tribe, 0)

        # Heartbeat only if idle >= HEARTBEAT_MINUTES
        now = time.time()
        idle_for = now - _last_activity.get(tribe, now)
        hb_for = now - _last_heartbeat.get(tribe, now)

        if idle_for >= (HEARTBEAT_MINUTES * 60) and hb_for >= (HEARTBEAT_MINUTES * 60):
            embed = {"description": "Heartbeat: no new logs since last (still polling).", "color": COLOR_HEARTBEAT}
            await webhook_post(session, webhook, thread_id, embed)
            _last_heartbeat[tribe] = now
            print(f"Heartbeat sent for {tribe}")

    return sent_counts

async def _tribelogs_loop():
    global _routes

    _routes = load_routes()
    print("Tribe routes loaded:", [r["tribe"] for r in _routes] if _routes else "[] (use /linktribelog)")

    async with aiohttp.ClientSession() as session:
        # Seed dedupe to avoid backlog spam
        if not BACKLOG_ON_START and _routes:
            try:
                text = await rcon_command("GetGameLog", timeout=12.0)
                for r in _routes:
                    tribe = r["tribe"]
                    _seen.setdefault(tribe, set())
                    _last_activity.setdefault(tribe, time.time())
                    _last_heartbeat.setdefault(tribe, time.time())

                    for ln in text.splitlines():
                        parsed = extract_tribe_line(ln, tribe)
                        if not parsed:
                            continue
                        day, hh, mm, ss, who_what = parsed
                        msg = format_short(day, hh, mm, ss, who_what)
                        _seen[tribe].add(_fingerprint(msg))

                print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")
            except Exception as e:
                print(f"Seed dedupe failed: {e}")

        while True:
            try:
                await _poll_once(session)
            except Exception as e:
                print(f"GetGameLog/forward error: {e}")

            await asyncio.sleep(POLL_SECONDS)

def tribelogs_start_polling():
    global _poll_task
    if _poll_task and not _poll_task.done():
        return
    _poll_task = asyncio.create_task(_tribelogs_loop())

# =========================
# SLASH COMMANDS
# =========================
def setup_tribelog_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int):
    guild_obj = discord.Object(id=guild_id)

    @tree.command(
        name="linktribelog",
        guild=guild_obj,
        description="Link a tribe's logs to a webhook + forum thread"
    )
    async def linktribelog(i: discord.Interaction, tribe_name: str, webhook_url: str, thread_id: str):
        if not getattr(i.user, "roles", None) or admin_role_id not in [r.id for r in i.user.roles]:
            await i.response.send_message("❌ No permission.", ephemeral=True)
            return

        tribe_name = tribe_name.strip()
        webhook_url = webhook_url.strip()
        thread_id = thread_id.strip()

        if not tribe_name:
            await i.response.send_message("❌ Tribe name missing.", ephemeral=True)
            return
        if "discord.com/api/webhooks/" not in webhook_url:
            await i.response.send_message("❌ That doesn't look like a Discord webhook URL.", ephemeral=True)
            return
        if not thread_id.isdigit():
            await i.response.send_message("❌ thread_id must be digits.", ephemeral=True)
            return

        webhook_base = normalize_webhook_base(webhook_url)

        routes = load_routes()
        found = False
        for r in routes:
            if r["tribe"].lower() == tribe_name.lower():
                r["webhook"] = webhook_base
                r["thread_id"] = thread_id
                found = True
                break
        if not found:
            routes.append({"tribe": tribe_name, "webhook": webhook_base, "thread_id": thread_id})

        save_routes(routes)

        await i.response.send_message(
            f"✅ Linked **{tribe_name}** → forum thread `{thread_id}`\nWebhook stored: `{webhook_base}`",
            ephemeral=True
        )

    @tree.command(name="listtribelogs", guild=guild_obj, description="List linked tribe log routes")
    async def listtribelogs(i: discord.Interaction):
        routes = load_routes()
        if not routes:
            await i.response.send_message("No tribe routes saved yet. Use `/linktribelog`.", ephemeral=True)
            return

        lines = []
        for r in routes:
            lines.append(f"- **{r['tribe']}** → thread `{r.get('thread_id')}`")

        await i.response.send_message("\n".join(lines), ephemeral=True)

    @tree.command(name="pullnow", guild=guild_obj, description="Force an immediate GetGameLog poll and report matches")
    async def pullnow(i: discord.Interaction):
        if not getattr(i.user, "roles", None) or admin_role_id not in [r.id for r in i.user.roles]:
            await i.response.send_message("❌ No permission.", ephemeral=True)
            return

        await i.response.defer(ephemeral=True)

        async with aiohttp.ClientSession() as session:
            try:
                counts = await _poll_once(session)
            except Exception as e:
                await i.followup.send(f"❌ Poll failed: {e}", ephemeral=True)
                return

        if not counts:
            await i.followup.send("ℹ️ No routes loaded yet.", ephemeral=True)
            return

        summary = "\n".join([f"- {k}: {v} new" for k, v in counts.items()])
        await i.followup.send(f"✅ Poll complete:\n{summary}", ephemeral=True)