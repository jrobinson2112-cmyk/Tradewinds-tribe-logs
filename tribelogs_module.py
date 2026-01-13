import os
import re
import json
import time
import asyncio
import hashlib
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import aiohttp
import discord
from discord import app_commands


# =========================
# CONFIG / ENV
# =========================
RCON_HOST = os.getenv("RCON_HOST", "")
RCON_PORT = int(os.getenv("RCON_PORT", "0") or "0")
RCON_PASSWORD = os.getenv("RCON_PASSWORD", "")

# Polling cadence for GetGameLog
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "15") or "15")

# Heartbeat: only if NO activity for this many minutes (edit-in-place)
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "60") or "60")
HEARTBEAT_COLOR = 0x95A5A6  # grey-ish

# Discord embed limits (safe chunk)
MAX_EMBED_DESC = 3500

# Persistent storage (use your Railway Volume mount as /data)
DATA_DIR = os.getenv("DATA_DIR", "/data")
ROUTES_FILE = os.path.join(DATA_DIR, "tribe_routes.json")
STATE_FILE = os.path.join(DATA_DIR, "tribe_state.json")

# Admin role required for /linktribelog
DEFAULT_ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "1439069787207766076") or "1439069787207766076")


# =========================
# VALIDATION
# =========================
def _ensure_env():
    missing = []
    if not RCON_HOST:
        missing.append("RCON_HOST")
    if not RCON_PORT:
        missing.append("RCON_PORT")
    if not RCON_PASSWORD:
        missing.append("RCON_PASSWORD")
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


# =========================
# RCON (Source-like)
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


async def rcon_command(command: str, timeout: float = 8.0) -> str:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout
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

        # parse packets
        out = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i:i+4], "little", signed=True)
            i += 4
            if size < 10 or i + size > len(data):
                break
            pkt = data[i:i+size]
            i += size
            body = pkt[8:-2]  # skip id+type, strip nulls

            # decode robustly: try utf-8, fallback latin-1 if lots of replacement chars
            txt = body.decode("utf-8", errors="replace")
            if txt.count("\ufffd") > 2:  # many replacement chars
                txt = body.decode("latin-1", errors="replace")
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
# PERSISTENCE
# =========================
def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_routes() -> list[dict]:
    _ensure_data_dir()
    if not os.path.exists(ROUTES_FILE):
        return []
    try:
        with open(ROUTES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            # normalize
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
        return []
    except Exception:
        return []


def save_routes(routes: list[dict]) -> None:
    _ensure_data_dir()
    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(routes, f, ensure_ascii=False, indent=2)


def load_state() -> dict:
    _ensure_data_dir()
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state: dict) -> None:
    _ensure_data_dir()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# =========================
# DISCORD WEBHOOK HELPERS
# =========================
def _clean_webhook_and_thread(webhook_url: str, thread_id: str | None) -> tuple[str, str]:
    """
    Accepts a Discord webhook URL that may contain ?Thread=... or ?thread_id=...
    Returns (base_webhook_url_without_thread_query, thread_id).
    """
    webhook_url = webhook_url.strip()

    p = urlparse(webhook_url)
    q = parse_qs(p.query)

    # allow Thread= or thread_id=
    url_thread = None
    if "Thread" in q and q["Thread"]:
        url_thread = q["Thread"][0]
    if "thread_id" in q and q["thread_id"]:
        url_thread = q["thread_id"][0]

    final_thread = (thread_id or "").strip() or (url_thread or "").strip()
    if not final_thread:
        raise ValueError("Missing thread_id (provide it or include ?Thread=... in webhook URL).")

    # strip thread params from URL
    for k in ["Thread", "thread_id"]:
        if k in q:
            del q[k]
    new_query = urlencode({k: v[0] for k, v in q.items()}) if q else ""
    base = urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, p.fragment))

    # basic sanity
    if "discord.com/api/webhooks/" not in base:
        raise ValueError("That does not look like a Discord webhook URL.")
    if not final_thread.isdigit():
        raise ValueError("thread_id must be numeric.")

    return base, final_thread


async def webhook_send_embed(session: aiohttp.ClientSession, base_webhook: str, thread_id: str, embed: dict) -> str | None:
    """
    Posts an embed to a forum thread via webhook.
    Returns message id if available (when wait=true).
    """
    url = base_webhook
    joiner = "&" if "?" in url else "?"
    url = f"{url}{joiner}wait=true&thread_id={thread_id}"

    async with session.post(url, json={"embeds": [embed]}) as r:
        # Discord webhooks:
        # - 200 with JSON body when wait=true
        # - 204 no content if wait not set
        try:
            data = await r.json()
        except Exception:
            data = None

        if r.status not in (200, 204):
            raise RuntimeError(f"Webhook post failed: {r.status} {data}")

        if isinstance(data, dict) and "id" in data:
            return str(data["id"])
        return None


async def webhook_edit_embed(session: aiohttp.ClientSession, base_webhook: str, message_id: str, embed: dict) -> None:
    """
    Edits an existing webhook message (NO thread_id required for editing).
    """
    url = f"{base_webhook}/messages/{message_id}"
    joiner = "&" if "?" in url else "?"
    url = f"{url}{joiner}wait=true"

    async with session.patch(url, json={"embeds": [embed]}) as r:
        if r.status not in (200, 204):
            try:
                data = await r.json()
            except Exception:
                data = None
            # if deleted, caller can re-create
            raise RuntimeError(f"Webhook edit failed: {r.status} {data}")


# =========================
# GAMELOG PARSING
# =========================
# We only want the: "Day X, HH:MM:SS: ..." portion and after.
DAYLINE_RE = re.compile(r"(Day\s+\d+,\s+\d{1,2}:\d{2}:\d{2}\s*:.*)$", re.IGNORECASE)

def extract_dayline(raw_line: str) -> str | None:
    """
    Extracts the clean "Day X, HH:MM:SS: ..." substring from any noisy prefix.
    """
    if not raw_line:
        return None
    line = raw_line.strip()
    if not line:
        return None

    m = DAYLINE_RE.search(line)
    if not m:
        return None

    out = m.group(1).strip()

    # Remove any stray markup like <RichColor ...>
    out = re.sub(r"<\s*RichColor[^>]*>", "", out, flags=re.IGNORECASE).strip()
    return out


def line_mentions_tribe(raw_line: str, tribe: str) -> bool:
    """
    Determines whether a raw GetGameLog line is for a given tribe.
    Handles common formats:
      - "... Tribe Valkyrie ..."
      - "... (Valkyrie)!"
    """
    t = tribe.strip()
    if not t:
        return False
    low = raw_line.lower()

    # common prefix
    if f"tribe {t.lower()}" in low:
        return True

    # common end tag in UI-style lines: "(Valkyrie)"
    if f"({t.lower()})" in low:
        return True

    return False


def classify_color(clean_dayline: str) -> int:
    s = clean_dayline.lower()

    # Red: killed / died / death / destroyed
    if any(k in s for k in [" killed", " was killed", " died", " death", " destroyed", " starved to death"]):
        return 0xE74C3C  # red

    # Yellow: demolished OR unclaimed
    if "demolish" in s or "demolished" in s or "unclaimed" in s:
        return 0xF1C40F  # yellow

    # Purple: claimed (but NOT unclaimed)
    if "claimed" in s and "unclaimed" not in s:
        return 0x9B59B6  # purple

    # Green: tamed
    if "tamed" in s:
        return 0x2ECC71  # green

    # Light blue: alliance
    if "alliance" in s:
        return 0x5DADE2  # light blue

    # White: everything else (froze etc)
    return 0xFFFFFF


def hash_line(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()


# =========================
# ROUTES / STATE IN-MEM
# =========================
_routes: list[dict] = []
_state: dict = {}  # per-tribe: { "seen": [...], "last_activity_ts": float, "hb_last_sent_ts": float, "hb_msg_id": str }


def _get_or_init_tribe_state(tribe: str) -> dict:
    if tribe not in _state:
        _state[tribe] = {
            "seen": [],
            "last_activity_ts": 0.0,
            "hb_last_sent_ts": 0.0,
            "hb_msg_id": None,
        }
    # clean up types
    st = _state[tribe]
    if not isinstance(st.get("seen"), list):
        st["seen"] = []
    return st


def _persist_state():
    save_state(_state)


def _load_all():
    global _routes, _state
    _routes = load_routes()
    _state = load_state()


# =========================
# HEARTBEAT (EDIT-IN-PLACE)
# =========================
def _make_heartbeat_embed(tribe: str) -> dict:
    return {
        "description": "Heartbeat: no new logs since last (still polling).",
        "color": HEARTBEAT_COLOR,
        "footer": {"text": f"Tribe: {tribe} • {time.strftime('%Y-%m-%d %H:%M:%S')}"}
    }


async def maybe_send_heartbeat(session: aiohttp.ClientSession, route: dict) -> None:
    """
    Sends/edits heartbeat ONLY if no activity for HEARTBEAT_MINUTES
    and ONLY once per HEARTBEAT_MINUTES (persisted).
    """
    tribe = route["tribe"]
    st = _get_or_init_tribe_state(tribe)

    now = time.time()
    last_activity = float(st.get("last_activity_ts") or 0.0)
    last_sent = float(st.get("hb_last_sent_ts") or 0.0)

    # if there's been recent activity, do nothing (and also clear last_sent so next idle window counts fresh)
    if last_activity and (now - last_activity) < (HEARTBEAT_MINUTES * 60):
        return

    # must be idle long enough
    if last_activity and (now - last_activity) < (HEARTBEAT_MINUTES * 60):
        return

    # only once per window
    if last_sent and (now - last_sent) < (HEARTBEAT_MINUTES * 60):
        return

    embed = _make_heartbeat_embed(tribe)
    base_webhook = route["webhook"]
    thread_id = route["thread_id"]

    # edit-in-place if we have a message id
    hb_id = st.get("hb_msg_id")
    try:
        if hb_id:
            await webhook_edit_embed(session, base_webhook, str(hb_id), embed)
        else:
            mid = await webhook_send_embed(session, base_webhook, thread_id, embed)
            if mid:
                st["hb_msg_id"] = mid
    except Exception:
        # if edit fails (deleted), recreate once
        try:
            mid = await webhook_send_embed(session, base_webhook, thread_id, embed)
            if mid:
                st["hb_msg_id"] = mid
        except Exception as e2:
            print(f"Heartbeat error for {tribe}: {e2}")
            return

    st["hb_last_sent_ts"] = now
    _persist_state()
    print(f"Heartbeat OK (edited/posted) for {tribe}")


# =========================
# FORWARDING
# =========================
async def forward_new_logs(session: aiohttp.ClientSession, log_text: str) -> int:
    """
    Finds new tribe lines and forwards them. Returns number forwarded.
    """
    if not log_text:
        return 0

    forwarded = 0
    lines = [ln for ln in log_text.splitlines() if ln and ln.strip()]

    for route in _routes:
        tribe = route["tribe"]
        st = _get_or_init_tribe_state(tribe)
        seen = st["seen"]

        # We scan from top->bottom so ordering in Discord matches game log
        for raw in lines:
            if not line_mentions_tribe(raw, tribe):
                continue

            clean = extract_dayline(raw)
            if not clean:
                continue

            h = hash_line(f"{tribe}|{clean}")
            if h in seen:
                continue

            # keep only the last 500 seen per tribe
            seen.append(h)
            if len(seen) > 500:
                del seen[:100]

            color = classify_color(clean)
            embed = {
                "description": clean[:MAX_EMBED_DESC],
                "color": color,
            }

            try:
                await webhook_send_embed(session, route["webhook"], route["thread_id"], embed)
                forwarded += 1
                st["last_activity_ts"] = time.time()
                # reset heartbeat timer so it won't send immediately after activity
                st["hb_last_sent_ts"] = float(st.get("hb_last_sent_ts") or 0.0)
            except Exception as e:
                print(f"GetGameLog/forward error for {tribe}: {e}")

        _persist_state()

    return forwarded


# =========================
# INITIAL DEDUPE SEED
# =========================
async def seed_dedupe_from_current_gamelog() -> None:
    """
    On first boot, we don't want to spam backlog.
    We read current GetGameLog and mark matching lines as 'seen' so only NEW events forward.
    """
    try:
        log_text = await rcon_command("GetGameLog", timeout=10.0)
    except Exception as e:
        print(f"Seed dedupe: GetGameLog failed: {e}")
        return

    if not log_text:
        print("Seed dedupe: GetGameLog empty.")
        return

    lines = [ln for ln in log_text.splitlines() if ln and ln.strip()]
    for route in _routes:
        tribe = route["tribe"]
        st = _get_or_init_tribe_state(tribe)
        seen = st["seen"]

        for raw in lines:
            if not line_mentions_tribe(raw, tribe):
                continue
            clean = extract_dayline(raw)
            if not clean:
                continue
            h = hash_line(f"{tribe}|{clean}")
            if h not in seen:
                seen.append(h)

        if len(seen) > 500:
            seen[:] = seen[-500:]

    _persist_state()
    print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")


# =========================
# PUBLIC: LOOP STARTER
# =========================
async def run_tribelogs_loop() -> None:
    """
    Background loop: polls GetGameLog, forwards new tribe lines,
    and sends heartbeats (edit-in-place) only when idle.
    """
    _ensure_env()
    _load_all()

    print("Tribe routes loaded:", [r["tribe"] for r in _routes])

    # seed dedupe only if we have no seen hashes yet
    any_seen = False
    for r in _routes:
        st = _get_or_init_tribe_state(r["tribe"])
        if st.get("seen"):
            any_seen = True
            break

    if not any_seen and _routes:
        await seed_dedupe_from_current_gamelog()

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                _load_all()  # allow routes added while running

                # Poll GetGameLog once, reuse for all tribes
                log_text = await rcon_command("GetGameLog", timeout=10.0)

                # Forward new logs
                n = await forward_new_logs(session, log_text)

                # Heartbeats (only if idle for >= HEARTBEAT_MINUTES)
                if HEARTBEAT_MINUTES < 999999:
                    for route in _routes:
                        await maybe_send_heartbeat(session, route)

                if n > 0:
                    print(f"Forwarded {n} new logs.")
            except Exception as e:
                print(f"Tribelogs loop error: {e}")

            await asyncio.sleep(POLL_SECONDS)


# =========================
# SLASH COMMANDS
# =========================
def _has_admin_role(member: discord.Member, admin_role_id: int) -> bool:
    try:
        return any(r.id == admin_role_id for r in member.roles)
    except Exception:
        return False


def setup_tribelog_commands(
    tree: app_commands.CommandTree,
    guild_id: int,
    admin_role_id: int = DEFAULT_ADMIN_ROLE_ID,
) -> None:
    """
    Registers slash commands. Call in on_ready before syncing.
    """

    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(name="linktribelog", description="Link a tribe to a Discord forum thread via webhook", guild=guild_obj)
    @app_commands.describe(
        tribe="Exact tribe name as shown in logs (e.g. Valkyrie)",
        webhook_url="Discord webhook URL (can include ?Thread=... optionally)",
        thread_id="Discord forum thread ID (optional if webhook_url contains ?Thread=...)"
    )
    async def linktribelog(
        interaction: discord.Interaction,
        tribe: str,
        webhook_url: str,
        thread_id: str = "",
    ):
        if not isinstance(interaction.user, discord.Member) or not _has_admin_role(interaction.user, admin_role_id):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return

        tribe = (tribe or "").strip()
        webhook_url = (webhook_url or "").strip()
        thread_id = (thread_id or "").strip()

        if not tribe or not webhook_url:
            await interaction.response.send_message("❌ tribe and webhook_url are required.", ephemeral=True)
            return

        try:
            base_webhook, tid = _clean_webhook_and_thread(webhook_url, thread_id or None)
        except Exception as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        _load_all()

        # upsert route
        updated = False
        for r in _routes:
            if r["tribe"].lower() == tribe.lower():
                r["tribe"] = tribe
                r["webhook"] = base_webhook
                r["thread_id"] = tid
                updated = True
                break

        if not updated:
            _routes.append({"tribe": tribe, "webhook": base_webhook, "thread_id": tid})

        save_routes(_routes)

        # init state
        _get_or_init_tribe_state(tribe)
        _persist_state()

        await interaction.response.send_message(
            f"✅ Linked **{tribe}** → thread **{tid}** (saved; survives redeploys).",
            ephemeral=True
        )
        print(f"Linked tribe route: {{'tribe': '{tribe}', 'webhook': '{base_webhook}', 'thread_id': '{tid}'}}")

    @tree.command(name="listtribelogs", description="List currently linked tribe log routes", guild=guild_obj)
    async def listtribelogs(interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not _has_admin_role(interaction.user, admin_role_id):
            await interaction.response.send_message("❌ No permission.", ephemeral=True)
            return

        _load_all()
        if not _routes:
            await interaction.response.send_message("No tribe routes linked yet.", ephemeral=True)
            return

        lines = []
        for r in _routes:
            lines.append(f"- **{r['tribe']}** → thread `{r['thread_id']}`")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)