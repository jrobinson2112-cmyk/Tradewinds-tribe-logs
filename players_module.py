import os
import time
import json
import asyncio
import aiohttp

# =====================
# ENV
# =====================
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "0") or 0)
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

PLAYER_CAP = int(os.getenv("PLAYER_CAP", "42") or 42)
PLAYERS_POLL_SECONDS = float(os.getenv("PLAYERS_POLL_SECONDS", "30") or 30)

DATA_DIR = os.getenv("DATA_DIR", "/data")
STATE_PATH = os.path.join(DATA_DIR, "players_state.json")

# =====================
# VALIDATION
# =====================
def _require_env():
    missing = []
    if not PLAYERS_WEBHOOK_URL:
        missing.append("PLAYERS_WEBHOOK_URL")
    if not RCON_HOST:
        missing.append("RCON_HOST")
    if not RCON_PORT:
        missing.append("RCON_PORT")
    if not RCON_PASSWORD:
        missing.append("RCON_PASSWORD")
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


# =====================
# STATE (persist message id across restarts)
# =====================
def _ensure_data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        # If the platform doesn't allow it, we'll still run without persistence.
        pass

def _load_state():
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"message_id": None}

def _save_state(state: dict):
    try:
        _ensure_data_dir()
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass

_state = _load_state()


# =====================
# RCON (Source RCON minimal)
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

async def rcon_command(command: str, timeout: float = 6.0) -> str:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout
    )
    try:
        # Auth
        writer.write(_rcon_make_packet(1, 3, RCON_PASSWORD))
        await writer.drain()

        raw = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        if len(raw) < 12:
            raise RuntimeError("RCON auth failed (short response)")

        # Command
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


def parse_listplayers(output: str):
    """
    ASA often returns:
      1. Name, SteamID/ID
      2. Name, ...
    We'll extract the name part.
    """
    players = []
    if not output:
        return players

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue

        # remove leading "1. "
        if ". " in line:
            line = line.split(". ", 1)[1]

        # keep part before comma
        if "," in line:
            name = line.split(",", 1)[0].strip()
        else:
            name = line.strip()

        if name and name.lower() not in ("executing", "listplayers", "done"):
            players.append(name)

    return players


# =====================
# DISCORD WEBHOOK UPSERT
# =====================
async def _webhook_request(session: aiohttp.ClientSession, method: str, url: str, payload: dict):
    async with session.request(method, url, json=payload) as r:
        # Discord can return 204 for some webhook ops, or json for others
        if r.status in (200, 201):
            return await r.json()
        if r.status == 204:
            return {}
        # try read json error
        try:
            data = await r.json()
        except Exception:
            data = {"error": await r.text()}
        raise RuntimeError(f"Webhook {method} failed: {r.status} {data}")

async def upsert_webhook_embed(session: aiohttp.ClientSession, embed: dict):
    """
    Tries to PATCH existing message if we have message_id.
    If missing/invalid -> POST a new one with wait=true and save its id.
    """
    mid = _state.get("message_id")

    # Patch existing
    if mid:
        try:
            await _webhook_request(
                session,
                "PATCH",
                f"{PLAYERS_WEBHOOK_URL}/messages/{mid}",
                {"embeds": [embed]},
            )
            return
        except Exception as e:
            # Lost message / wrong id / permissions => reset and fall back to POST
            print(f"Players webhook patch failed, will recreate message: {e}")
            _state["message_id"] = None
            _save_state(_state)

    # Post new
    data = await _webhook_request(
        session,
        "POST",
        PLAYERS_WEBHOOK_URL + "?wait=true",
        {"embeds": [embed]},
    )
    # Discord returns the created message JSON containing "id"
    if "id" in data:
        _state["message_id"] = data["id"]
        _save_state(_state)


# =====================
# BUILD EMBED
# =====================
def build_players_embed(names: list[str], online_ok: bool, err: str | None):
    count = len(names)
    emoji = "🟢" if online_ok else "🔴"

    if names:
        lines = [f"{i+1:02d}) {n}" for i, n in enumerate(names[:50])]
        desc = f"**{count}/{PLAYER_CAP}** online\n\n" + "\n".join(lines)
    else:
        if err:
            desc = f"**0/{PLAYER_CAP}** online\n\n*(RCON error: {err})*"
        else:
            desc = f"**0/{PLAYER_CAP}** online\n\n*(No player list returned.)*"

    return {
        "title": "Online Players",
        "description": desc,
        "color": 0x2ECC71 if online_ok else 0xE74C3C,
        "footer": {"text": f"Last update: {time.strftime('%H:%M:%S')}"}
    }


# =====================
# MAIN LOOP
# =====================
async def run_players_loop(client=None):
    """
    Starts a forever loop that updates the PLAYERS_WEBHOOK_URL embed.
    client is optional; if provided, we'll wait for readiness.
    """
    _require_env()
    _ensure_data_dir()

    if client is not None:
        try:
            await client.wait_until_ready()
        except Exception:
            # Even if client wait fails, continue polling RCON + webhook
            pass

    print("✅ players_module loop running (RCON -> webhook embed)")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                out = await rcon_command("ListPlayers", timeout=7.0)
                names = parse_listplayers(out)

                embed = build_players_embed(names, online_ok=True, err=None)
                await upsert_webhook_embed(session, embed)

            except Exception as e:
                # Post an error embed but keep looping
                err = str(e)
                print(f"Players loop error: {err}")
                try:
                    embed = build_players_embed([], online_ok=False, err=err)
                    await upsert_webhook_embed(session, embed)
                except Exception as inner:
                    print(f"Players webhook error: {inner}")

            await asyncio.sleep(PLAYERS_POLL_SECONDS)