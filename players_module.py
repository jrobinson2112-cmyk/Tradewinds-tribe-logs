import os
import time
import json
import asyncio
import aiohttp

# =========================
# ENV
# =========================
RCON_HOST = os.getenv("RCON_HOST", "")
RCON_PORT = int(os.getenv("RCON_PORT", "0") or "0")
RCON_PASSWORD = os.getenv("RCON_PASSWORD", "")

PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL", "")  # REQUIRED
PLAYER_CAP = int(os.getenv("PLAYER_CAP", "42") or "42")

# Poll interval
PLAYERS_POLL_SECONDS = float(os.getenv("PLAYERS_POLL_SECONDS", "60") or "60")

# Persistent storage (Railway Volume mount)
DATA_DIR = os.getenv("DATA_DIR", "/data")
STATE_FILE = os.path.join(DATA_DIR, "players_state.json")

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
    if not PLAYERS_WEBHOOK_URL:
        missing.append("PLAYERS_WEBHOOK_URL")
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

# =========================
# STATE
# =========================
def load_state():
    _ensure_data_dir()
    if not os.path.exists(STATE_FILE):
        return {"message_id": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return {"message_id": None}
        return {"message_id": d.get("message_id")}
    except Exception:
        return {"message_id": None}

def save_state(s):
    _ensure_data_dir()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

_state = load_state()

# =========================
# RCON
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
            txt = body.decode("utf-8", errors="replace")
            if txt.count("\ufffd") > 2:
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

def parse_listplayers(output: str):
    """
    Typical output lines:
      1. PlayerName, SteamID/EOS...
    """
    players = []
    if not output:
        return players

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue

        # strip "1. "
        if ". " in line:
            line = line.split(". ", 1)[1]

        # take name before comma if present
        if "," in line:
            name = line.split(",", 1)[0].strip()
        else:
            name = line.strip()

        low = name.lower()
        if name and low not in ("executing", "listplayers", "done"):
            players.append(name)

    return players

# =========================
# DISCORD WEBHOOK UPSERT
# =========================
async def _webhook_post(session: aiohttp.ClientSession, embed: dict) -> str | None:
    url = PLAYERS_WEBHOOK_URL
    joiner = "&" if "?" in url else "?"
    url = f"{url}{joiner}wait=true"

    async with session.post(url, json={"embeds": [embed]}) as r:
        try:
            data = await r.json()
        except Exception:
            data = None

        if r.status not in (200, 204):
            raise RuntimeError(f"Webhook post failed: {r.status} {data}")

        if isinstance(data, dict) and "id" in data:
            return str(data["id"])
        return None

async def _webhook_patch(session: aiohttp.ClientSession, message_id: str, embed: dict) -> None:
    url = f"{PLAYERS_WEBHOOK_URL}/messages/{message_id}"
    joiner = "&" if "?" in url else "?"
    url = f"{url}{joiner}wait=true"

    async with session.patch(url, json={"embeds": [embed]}) as r:
        if r.status in (200, 204):
            return
        try:
            data = await r.json()
        except Exception:
            data = None
        raise RuntimeError(f"Webhook patch failed: {r.status} {data}")

async def upsert_players_embed(session: aiohttp.ClientSession, embed: dict):
    """
    Create once (store id), then edit forever.
    If edit fails (message deleted), recreate.
    """
    mid = _state.get("message_id")
    if mid:
        try:
            await _webhook_patch(session, mid, embed)
            return
        except Exception:
            _state["message_id"] = None
            save_state(_state)

    new_id = await _webhook_post(session, embed)
    _state["message_id"] = new_id
    save_state(_state)

# =========================
# EMBED BUILD
# =========================
def build_players_embed(names: list[str], cap: int) -> dict:
    count = len(names)
    lines = [f"{idx+1:02d}) {n}" for idx, n in enumerate(names[:50])]
    desc = f"**{count}/{cap}** online\n\n" + ("\n".join(lines) if lines else "*(No players returned.)*")

    return {
        "title": "Online Players",
        "description": desc,
        "color": 0x2ECC71,
        "footer": {"text": f"Last update: {time.strftime('%H:%M:%S')}"}
    }

# =========================
# LOOP
# =========================
async def run_players_loop():
    _ensure_env()

    # simple backoff for RCON issues
    backoff = 0.0

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                out = await rcon_command("ListPlayers", timeout=10.0)
                names = parse_listplayers(out)
                embed = build_players_embed(names, PLAYER_CAP)
                await upsert_players_embed(session, embed)

                backoff = 0.0  # reset on success

            except Exception as e:
                # don’t crash loop; back off a bit
                print(f"Players loop error: {e}")
                backoff = min(300.0, backoff + 15.0)  # up to 5 min

            await asyncio.sleep(PLAYERS_POLL_SECONDS + backoff)