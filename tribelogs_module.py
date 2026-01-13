import os
import re
import time
import json
import asyncio
import aiohttp
import discord
from discord import app_commands

# =========================
# CONFIG (env)
# =========================
DATA_DIR = os.getenv("DATA_DIR", "/data")  # mount your Railway volume here
ROUTES_FILE = os.getenv("TRIBE_ROUTES_FILE") or os.path.join(DATA_DIR, "tribe_routes.json")

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = os.getenv("RCON_PORT")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

TRIBELOG_POLL_SECONDS = int(os.getenv("TRIBELOG_POLL_SECONDS", "15"))
HEARTBEAT_MINUTES = int(os.getenv("TRIBELOG_HEARTBEAT_MINUTES", "60"))

# If you want to allow GetGameLog to return big output, keep timeout a bit higher:
RCON_TIMEOUT = float(os.getenv("RCON_TIMEOUT", "8.0"))

# =========================
# INTERNAL STATE
# =========================
_routes = []  # list[dict]: {"tribe": str, "webhook": str, "thread_id": str}
_routes_lock = asyncio.Lock()

# Dedupe (in-memory)
_seen = set()
_seen_queue = []  # keeps order for trimming
SEEN_MAX = 5000

_first_run_seeded = False
_last_activity_ts_by_tribe = {}  # tribe -> unix ts
_last_heartbeat_ts_by_tribe = {}  # tribe -> unix ts

# =========================
# UTIL: ensure data dir
# =========================
def _ensure_data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        # if no volume, still try local
        pass

def _load_routes_from_disk():
    global _routes
    _ensure_data_dir()
    if not os.path.exists(ROUTES_FILE):
        _routes = []
        return
    try:
        with open(ROUTES_FILE, "r", encoding="utf-8") as f:
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
            _routes = cleaned
        else:
            _routes = []
    except Exception:
        _routes = []

def _save_routes_to_disk():
    _ensure_data_dir()
    tmp = ROUTES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_routes, f, indent=2, ensure_ascii=False)
    os.replace(tmp, ROUTES_FILE)

# =========================
# RCON (minimal source)
# =========================
def _rcon_make_packet(req_id: int, ptype: int, body: str) -> bytes:
    # IMPORTANT: use utf-8 replace to avoid crashes on unusual chars
    data = body.encode("utf-8", errors="replace") + b"\x00"
    packet = (
        req_id.to_bytes(4, "little", signed=True)
        + ptype.to_bytes(4, "little", signed=True)
        + data
        + b"\x00"
    )
    size = len(packet)
    return size.to_bytes(4, "little", signed=True) + packet

async def rcon_command(command: str, timeout: float = RCON_TIMEOUT) -> str:
    if not (RCON_HOST and RCON_PORT and RCON_PASSWORD):
        raise RuntimeError("RCON env vars missing (RCON_HOST/RCON_PORT/RCON_PASSWORD)")

    port = int(RCON_PORT)

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(RCON_HOST, port), timeout=timeout
    )
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

            # decode, but keep it resilient
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

# =========================
# PARSING / CLEANING
# =========================

# Removes RichColor tags: <RichColor Color="1, 0, 1, 1">
_RICHCOLOR_TAG = re.compile(r"<\s*/?\s*RichColor\b[^>]*>", re.IGNORECASE)

# Pull only "Day X, HH:MM:SS: ..." anywhere in a line
_DAYLINE = re.compile(r"(Day\s+\d+,\s+\d{1,2}:\d{2}:\d{2}:\s+.*)$")

def _strip_trailing_noise(s: str) -> str:
    # Remove the weird extra endings the game sometimes appends
    s = s.strip()

    # Common endings: "!)", ")!", "!" at end, "'))" etc.
    # We only normalize light-touch to not destroy legit text.
    if s.endswith("!)"):
        s = s[:-2] + ")"
    if s.endswith(")!"):
        s = s[:-1]
    if s.endswith("!'"):
        s = s[:-1]
    if s.endswith("!"):
        s = s[:-1]

    # Sometimes logs end with double ))
    while s.endswith("))"):
        s = s[:-1]

    return s.strip()

def clean_gamelog_line(raw: str) -> str | None:
    """
    Returns only: "Day X, HH:MM:SS: Who ... What ..."
    Or None if it doesn't contain a Day/time segment.
    """
    if not raw:
        return None

    line = raw.strip()
    if not line:
        return None

    # remove RichColor tags
    line = _RICHCOLOR_TAG.sub("", line).strip()

    # find Day/time segment anywhere in line and keep only that
    m = _DAYLINE.search(line)
    if not m:
        return None

    out = m.group(1).strip()
    out = _strip_trailing_noise(out)
    return out if out else None

def color_for_message(msg: str) -> int:
    """
    Embed color rules (your spec):
    Red    - Killed / Died / Death / Destroyed
    Yellow - Demolished AND Unclaimed
    Purple - Claimed
    Green  - Tamed
    Light blue - Alliance
    White  - Anything else (Froze etc.)
    """
    t = msg.lower()

    red_keys = [" killed", " died", " death", " destroyed", " was killed", " starved to death"]
    yellow_keys = [" demolished", " unclaimed"]
    purple_keys = [" claimed"]
    green_keys = [" tamed"]
    blue_keys = [" alliance"]

    if any(k in t for k in red_keys):
        return 0xE74C3C  # red
    if any(k in t for k in yellow_keys):
        return 0xF1C40F  # yellow
    # IMPORTANT: check "unclaimed" before "claimed" (because "unclaimed" contains claimed)
    if "claimed" in t:
        return 0x9B59B6  # purple
    if any(k in t for k in green_keys):
        return 0x2ECC71  # green
    if any(k in t for k in blue_keys):
        return 0x5DADE2  # light blue
    return 0xFFFFFF  # white

def _route_matches(route_tribe: str, line: str) -> bool:
    """
    Match tribe in the line. Many lines contain "(TribeName)" at end or "Tribe X," etc.
    We'll match case-insensitive either:
      - "(Valkyrie)" or " Tribe Valkyrie" or "Tribe: Valkyrie" or "Tribe Valkyrie,"
    """
    tribe = route_tribe.strip()
    if not tribe:
        return False
    low = line.lower()
    tlow = tribe.lower()

    if f"({tlow})" in low:
        return True
    if f" tribe {tlow}" in low:
        return True
    if f"tribe {tlow}," in low:
        return True
    if f"tribe: {tlow}" in low:
        return True

    # fallback: if the tribe name appears anywhere (last resort)
    return tlow in low

# =========================
# DISCORD WEBHOOK POSTING
# =========================
def _build_thread_webhook_url(base_webhook: str, thread_id: str | None) -> str:
    """
    Always post with wait=true and thread_id if provided.
    Strip any existing query params from webhook to avoid conflicts.
    """
    if not base_webhook:
        return base_webhook

    # strip query
    webhook = base_webhook.split("?", 1)[0].strip()

    params = ["wait=true"]
    if thread_id:
        params.append(f"thread_id={thread_id}")
    return webhook + "?" + "&".join(params)

async def post_log_embed(session: aiohttp.ClientSession, route: dict, clean_line: str):
    tribe = route["tribe"]
    webhook = route["webhook"]
    thread_id = (route.get("thread_id") or "").strip() or None

    url = _build_thread_webhook_url(webhook, thread_id)

    embed = {
        "description": clean_line,
        "color": color_for_message(clean_line),
        "footer": {"text": f"Tribe: {tribe}"},
    }

    payload = {"embeds": [embed]}
    async with session.post(url, json=payload) as r:
        if r.status >= 400:
            txt = await r.text()
            raise RuntimeError(f"Discord webhook error {r.status}: {txt}")

# =========================
# DEDUPE
# =========================
def _dedupe_key(route_tribe: str, clean_line: str) -> str:
    # include tribe so same line across tribes doesn't collide
    return f"{route_tribe}||{clean_line}"

def _mark_seen(key: str):
    if key in _seen:
        return
    _seen.add(key)
    _seen_queue.append(key)
    if len(_seen_queue) > SEEN_MAX:
        old = _seen_queue.pop(0)
        _seen.discard(old)

# =========================
# HEARTBEAT
# =========================
async def _maybe_send_heartbeat(session: aiohttp.ClientSession, route: dict):
    tribe = route["tribe"]
    now = time.time()
    last_activity = _last_activity_ts_by_tribe.get(tribe, 0)
    last_hb = _last_heartbeat_ts_by_tribe.get(tribe, 0)

    # Only if no activity for HEARTBEAT_MINUTES and haven't sent heartbeat recently
    if now - last_activity < HEARTBEAT_MINUTES * 60:
        return
    if now - last_hb < HEARTBEAT_MINUTES * 60:
        return

    webhook = route["webhook"]
    thread_id = (route.get("thread_id") or "").strip() or None
    url = _build_thread_webhook_url(webhook, thread_id)

    payload = {
        "content": f"⏱️ No new logs since last check. (Tribe: **{tribe}**)"
    }
    async with session.post(url, json=payload) as r:
        # don't crash on heartbeat failure
        _last_heartbeat_ts_by_tribe[tribe] = now

# =========================
# MAIN LOOP
# =========================
async def _tribelog_poll_loop():
    global _first_run_seeded

    async with aiohttp.ClientSession() as session:
        backoff = 1.0
        while True:
            try:
                async with _routes_lock:
                    routes_snapshot = list(_routes)

                if not routes_snapshot:
                    await asyncio.sleep(TRIBELOG_POLL_SECONDS)
                    continue

                text = await rcon_command("GetGameLog", timeout=RCON_TIMEOUT)
                if not text.strip():
                    # still do heartbeat checks
                    for rt in routes_snapshot:
                        await _maybe_send_heartbeat(session, rt)
                    await asyncio.sleep(TRIBELOG_POLL_SECONDS)
                    continue

                # Split, clean, keep only lines that have Day/time
                raw_lines = [ln for ln in text.splitlines() if ln.strip()]
                cleaned = []
                for ln in raw_lines:
                    c = clean_gamelog_line(ln)
                    if c:
                        cleaned.append(c)

                if not cleaned:
                    for rt in routes_snapshot:
                        await _maybe_send_heartbeat(session, rt)
                    await asyncio.sleep(TRIBELOG_POLL_SECONDS)
                    continue

                # First run: seed dedupe with current output to avoid backlog spam
                if not _first_run_seeded:
                    for rt in routes_snapshot:
                        for c in cleaned:
                            if _route_matches(rt["tribe"], c):
                                _mark_seen(_dedupe_key(rt["tribe"], c))
                    _first_run_seeded = True
                    print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")
                    await asyncio.sleep(TRIBELOG_POLL_SECONDS)
                    continue

                # Process in chronological order
                sent_any = False
                for c in cleaned:
                    for rt in routes_snapshot:
                        if not _route_matches(rt["tribe"], c):
                            continue
                        key = _dedupe_key(rt["tribe"], c)
                        if key in _seen:
                            continue

                        await post_log_embed(session, rt, c)
                        _mark_seen(key)
                        sent_any = True
                        _last_activity_ts_by_tribe[rt["tribe"]] = time.time()

                # Heartbeats only when no activity
                if not sent_any:
                    for rt in routes_snapshot:
                        await _maybe_send_heartbeat(session, rt)

                backoff = 1.0  # reset backoff on success
                await asyncio.sleep(TRIBELOG_POLL_SECONDS)

            except Exception as e:
                print(f"GetGameLog/forward error: {e}")
                # backoff to avoid hammering RCON if it resets
                await asyncio.sleep(min(30.0, backoff))
                backoff = min(30.0, backoff * 2)

# =========================
# PUBLIC API (what main.py imports)
# =========================
def setup_tribelog_commands(
    tree: app_commands.CommandTree,
    guild_id: int,
    admin_role_id: int,
):
    """
    Registers /linktribelog, /listtribelogs, /unlinktribelog
    Commands are synced in main.py's on_ready (tree.sync).
    """

    @tree.command(name="linktribelog", guild=discord.Object(id=guild_id))
    @app_commands.describe(
        tribe="Tribe name exactly as in-game (e.g. Valkyrie)",
        webhook="Discord webhook URL for the forum/channel",
        thread_id="Forum thread id (required for forum webhooks)",
    )
    async def linktribelog(i: discord.Interaction, tribe: str, webhook: str, thread_id: str):
        # role-gated
        if not any(getattr(r, "id", None) == admin_role_id for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        tribe = (tribe or "").strip()
        webhook = (webhook or "").strip()
        thread_id = (thread_id or "").strip()

        if not tribe or not webhook or not thread_id:
            await i.response.send_message("❌ Provide tribe, webhook, and thread_id.", ephemeral=True)
            return

        async with _routes_lock:
            # replace if exists
            existing = next((r for r in _routes if r["tribe"].lower() == tribe.lower()), None)
            if existing:
                existing["tribe"] = tribe
                existing["webhook"] = webhook
                existing["thread_id"] = thread_id
            else:
                _routes.append({"tribe": tribe, "webhook": webhook, "thread_id": thread_id})
            _save_routes_to_disk()

        await i.response.send_message(f"✅ Linked tribe **{tribe}** to that webhook/thread.", ephemeral=True)
        print(f"Linked tribe route: { {'tribe': tribe, 'webhook': webhook, 'thread_id': thread_id} }")

    @tree.command(name="listtribelogs", guild=discord.Object(id=guild_id))
    async def listtribelogs(i: discord.Interaction):
        if not any(getattr(r, "id", None) == admin_role_id for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        async with _routes_lock:
            if not _routes:
                await i.response.send_message("No tribe routes linked.", ephemeral=True)
                return
            lines = []
            for r in _routes:
                lines.append(f"- **{r['tribe']}** → thread `{r.get('thread_id','')}`")
        await i.response.send_message("\n".join(lines), ephemeral=True)

    @tree.command(name="unlinktribelog", guild=discord.Object(id=guild_id))
    @app_commands.describe(tribe="Tribe name to remove")
    async def unlinktribelog(i: discord.Interaction, tribe: str):
        if not any(getattr(r, "id", None) == admin_role_id for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        tribe = (tribe or "").strip()
        if not tribe:
            await i.response.send_message("❌ Provide a tribe name.", ephemeral=True)
            return

        async with _routes_lock:
            before = len(_routes)
            _routes[:] = [r for r in _routes if r["tribe"].lower() != tribe.lower()]
            after = len(_routes)
            _save_routes_to_disk()

        if after < before:
            await i.response.send_message(f"✅ Unlinked **{tribe}**.", ephemeral=True)
        else:
            await i.response.send_message(f"ℹ️ No route found for **{tribe}**.", ephemeral=True)

def run_tribelogs_loop():
    """
    Starts the background polling task. Call once from main.py (after bot is ready).
    """
    _load_routes_from_disk()
    print(f"Tribe routes loaded: {[r['tribe'] for r in _routes]}")
    return asyncio.create_task(_tribelog_poll_loop())