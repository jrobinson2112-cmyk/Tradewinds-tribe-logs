import os
import json
import time
import asyncio
import aiohttp
import re
from urllib.parse import urlparse, parse_qs, urlunparse

import discord
from discord import app_commands


# =========================
# ENV (RCON)
# =========================
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "0") or 0)
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

# Data dir (mount your Railway volume here)
DATA_DIR = os.getenv("DATA_DIR", "/data")
ROUTES_FILE = os.path.join(DATA_DIR, "tribelog_routes.json")

# Polling
POLL_SECONDS = float(os.getenv("TRIBELOG_POLL_SECONDS", "10"))
HEARTBEAT_IDLE_SECONDS = 60 * 60  # 60 mins only if no activity


# =========================
# LOG PARSING
# =========================
DAYTIME_RE = re.compile(r"Day\s+(\d+),\s+(\d{1,2}):(\d{2}):(\d{2})\s*:\s*(.*)$", re.IGNORECASE)

# Color rules you requested
def classify_color(text_lower: str) -> int:
    # Red - Killed / Died / Death / Destroyed
    if any(k in text_lower for k in (" killed", "killed ", " died", "died ", " death", "destroyed")):
        return 0xE74C3C  # red
    # Yellow - Demolished OR Unclaimed
    if any(k in text_lower for k in ("demolished", " unclaimed", "unclaimed ")):
        return 0xF1C40F  # yellow
    # Purple - Claimed
    if any(k in text_lower for k in (" claimed", "claiming ")):
        return 0x9B59B6  # purple
    # Green - Tamed
    if any(k in text_lower for k in (" tamed", "taming ")):
        return 0x2ECC71  # green
    # Light blue - Alliance
    if "alliance" in text_lower:
        return 0x5DADE2  # light blue
    # White - anything else (e.g. froze)
    return 0xFFFFFF  # white


def strip_richcolor(s: str) -> str:
    # Removes leading <RichColor ...> and trailing </> style garbage
    s = re.sub(r"<\s*RichColor[^>]*>", "", s, flags=re.IGNORECASE)
    s = s.replace("</>", "")
    return s.strip()


def clean_trailing_punct(s: str) -> str:
    # Removes weird trailing artifacts like "!>)", "!)", ">)"
    while s.endswith(("!>)", "!)", ">)", ")")):
        s = s[:-1].rstrip()
    return s


def simplify_message(msg: str) -> str:
    """
    Output format exactly:
    Day XXX, HH:MM:SS - Who action 'Thing - Lvl N'
    (Keeps special characters as-is; discord will display what it receives.)
    """
    msg = strip_richcolor(msg)
    msg = clean_trailing_punct(msg)
    msg = msg.replace("\\u00d8", "Ø")  # safety: if something double-escaped
    msg = msg.strip()

    # Remove any remaining "Tribe X, ID Y:" prefix if present
    msg = re.sub(r"^Tribe\s+.*?,\s*ID\s*\d+\s*:\s*", "", msg, flags=re.IGNORECASE)

    # Try to shorten " - Lvl 150 (Roan Pinto)" -> " - Lvl 150"
    # Keep the quoted name as you asked (example: 'Roan Pinto - Lvl 150')
    # If it’s already fine, leave it.
    msg = re.sub(r"(\- Lvl\s*\d+)\s*\([^)]*\)", r"\1", msg)

    return msg


def parse_gamelog_lines(gamelog_text: str):
    """
    Yields tuples:
      (day:int, hh:int, mm:int, ss:int, message:str, raw_line:str)
    """
    if not gamelog_text:
        return
    for raw in gamelog_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = DAYTIME_RE.search(line)
        if not m:
            continue
        day = int(m.group(1))
        hh = int(m.group(2))
        mm = int(m.group(3))
        ss = int(m.group(4))
        msg = m.group(5).strip()
        yield day, hh, mm, ss, msg, line


# =========================
# ROUTES PERSISTENCE
# =========================
def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

def load_routes():
    _ensure_data_dir()
    if not os.path.exists(ROUTES_FILE):
        return []
    try:
        with open(ROUTES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []

def save_routes(routes):
    _ensure_data_dir()
    with open(ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(routes, f, ensure_ascii=False, indent=2)


def normalize_discord_webhook(webhook_input: str, thread_id_input: str | None):
    """
    Accepts:
      - plain webhook url
      - webhook url with ?Thread=123 or ?thread_id=123 (extracts thread id)
    Returns: (clean_webhook_url, thread_id_or_none)
    """
    webhook_input = (webhook_input or "").strip()

    # If someone pasted ONLY digits, it's not a webhook
    if webhook_input.isdigit():
        raise ValueError("Webhook must be a full Discord webhook URL, not an ID.")

    if not webhook_input.startswith("https://discord.com/api/webhooks/") and not webhook_input.startswith("https://discordapp.com/api/webhooks/"):
        raise ValueError("Webhook must start with https://discord.com/api/webhooks/")

    parsed = urlparse(webhook_input)
    qs = parse_qs(parsed.query)

    # Extract thread id from query if present
    extracted_thread = None
    if "thread_id" in qs and qs["thread_id"]:
        extracted_thread = qs["thread_id"][0]
    if "Thread" in qs and qs["Thread"]:
        extracted_thread = qs["Thread"][0]

    # Prefer explicit thread_id input if provided
    thread_id = (thread_id_input or "").strip() or extracted_thread

    # Clean webhook URL: remove query and fragment
    clean = parsed._replace(query="", fragment="")
    clean_webhook = urlunparse(clean)

    # Validate thread_id if provided
    if thread_id:
        if not thread_id.isdigit():
            raise ValueError("thread_id must be numeric (Discord thread ID).")

    return clean_webhook, thread_id


# =========================
# RCON (minimal)
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
        _ = await asyncio.wait_for(reader.read(4096), timeout=timeout)

        # cmd
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
# DISCORD WEBHOOK SEND
# =========================
async def post_to_webhook(session: aiohttp.ClientSession, webhook_url: str, thread_id: str | None, content: str, color: int):
    payload = {
        "embeds": [
            {"description": content, "color": color}
        ]
    }

    # Forum threads: must specify thread_id in query
    url = webhook_url
    if thread_id:
        url = f"{webhook_url}?wait=true&thread_id={thread_id}"
    else:
        url = f"{webhook_url}?wait=true"

    async with session.post(url, json=payload) as r:
        if r.status >= 300:
            try:
                data = await r.json()
            except Exception:
                data = await r.text()
            raise RuntimeError(f"Webhook post failed: {r.status} {data}")


# =========================
# COMMANDS
# =========================
def setup_tribelog_commands(tree: app_commands.CommandTree, guild_id: int, admin_role_id: int):
    guild_obj = discord.Object(id=guild_id)

    @tree.command(name="linktribelog", guild=guild_obj)
    async def linktribelog(i: discord.Interaction, tribe: str, webhook: str, thread_id: str = ""):
        # Role gate
        if not hasattr(i.user, "roles") or not any(r.id == admin_role_id for r in i.user.roles):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        try:
            clean_webhook, clean_thread_id = normalize_discord_webhook(webhook, thread_id)
        except Exception as e:
            await i.response.send_message(f"❌ {e}", ephemeral=True)
            return

        routes = load_routes()

        # upsert by tribe
        tribe_clean = tribe.strip()
        updated = False
        for r in routes:
            if str(r.get("tribe", "")).lower() == tribe_clean.lower():
                r["tribe"] = tribe_clean
                r["webhook"] = clean_webhook
                r["thread_id"] = clean_thread_id or ""
                updated = True
                break

        if not updated:
            routes.append({"tribe": tribe_clean, "webhook": clean_webhook, "thread_id": clean_thread_id or ""})

        save_routes(routes)
        await i.response.send_message(
            f"✅ Linked **{tribe_clean}** → webhook saved"
            + (f" (thread_id={clean_thread_id})" if clean_thread_id else ""),
            ephemeral=True
        )

    @tree.command(name="listtribelogs", guild=guild_obj)
    async def listtribelogs(i: discord.Interaction):
        routes = load_routes()
        if not routes:
            await i.response.send_message("No tribe routes saved.", ephemeral=True)
            return
        lines = []
        for r in routes:
            lines.append(f"- **{r.get('tribe')}** (thread_id={r.get('thread_id') or 'none'})")
        await i.response.send_message("\n".join(lines), ephemeral=True)


# =========================
# MAIN LOOP
# =========================
_task = None

def run_tribelogs_loop():
    global _task
    if _task and not _task.done():
        return _task
    _task = asyncio.create_task(_tribelogs_loop())
    return _task


async def _tribelogs_loop():
    # Basic env validation
    if not (RCON_HOST and RCON_PORT and RCON_PASSWORD):
        print("❌ Missing RCON env vars. Need RCON_HOST, RCON_PORT, RCON_PASSWORD.")
        return

    last_sent_ts = time.time()
    last_seen_line = None  # crude dedupe anchor

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                routes = load_routes()
                if not routes:
                    # idle heartbeat only if no activity for 60 mins
                    if (time.time() - last_sent_ts) >= HEARTBEAT_IDLE_SECONDS:
                        print("Heartbeat (no routes): still polling.")
                        last_sent_ts = time.time()
                    await asyncio.sleep(POLL_SECONDS)
                    continue

                # Pull log
                text = await rcon_command("GetGameLog", timeout=10.0)

                # Find newest matching line for each tribe
                newest_for_tribe = {}  # tribe_lower -> (raw_line, day, hh, mm, ss, msg)
                for day, hh, mm, ss, msg, raw in parse_gamelog_lines(text):
                    msg2 = strip_richcolor(msg)
                    msg_lower = msg2.lower()
                    for route in routes:
                        tribe = str(route.get("tribe", "")).strip()
                        if not tribe:
                            continue
                        if tribe.lower() in msg_lower:
                            newest_for_tribe[tribe.lower()] = (raw, day, hh, mm, ss, msg2)

                sent_any = False

                # Send each tribe's newest line (if it's new)
                for route in routes:
                    tribe = str(route.get("tribe", "")).strip()
                    if not tribe:
                        continue

                    hit = newest_for_tribe.get(tribe.lower())
                    if not hit:
                        continue

                    raw, day, hh, mm, ss, msg2 = hit
                    if raw == last_seen_line:
                        continue

                    clean_webhook = str(route.get("webhook", "")).strip()
                    thread_id = str(route.get("thread_id", "")).strip() or None

                    # HARD GUARD: if webhook isn't a real url, skip and print clear error
                    if not clean_webhook.startswith("https://discord.com/api/webhooks/") and not clean_webhook.startswith("https://discordapp.com/api/webhooks/"):
                        print(f"❌ Route webhook invalid for tribe '{tribe}': {clean_webhook}")
                        continue

                    # Output: Day/Time - Who and What (no extra prefix)
                    simplified = simplify_message(msg2)
                    content = f"Day {day}, {hh:02d}:{mm:02d}:{ss:02d} - {simplified}"

                    color = classify_color(content.lower())

                    await post_to_webhook(session, clean_webhook, thread_id, content, color)
                    last_seen_line = raw
                    last_sent_ts = time.time()
                    sent_any = True
                    print(f"Sent: {tribe} -> {content}")

                # Heartbeat only if no activity for 60 mins
                if not sent_any and (time.time() - last_sent_ts) >= HEARTBEAT_IDLE_SECONDS:
                    print("Heartbeat: no new logs since last (still polling).")
                    last_sent_ts = time.time()

            except Exception as e:
                # This is the line you're seeing — now it will include the *real* webhook URL if it fails
                print(f"GetGameLog/forward error: {e}")

            await asyncio.sleep(POLL_SECONDS)