import os
import json
import time
import asyncio
import aiohttp

# =====================
# ENV
# =====================
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")  # normal webhook (NOT forum-only)
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "0") or 0)
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

PLAYER_CAP = int(os.getenv("PLAYER_CAP", "42") or 42)
STATUS_POLL_SECONDS = float(os.getenv("STATUS_POLL_SECONDS", "15") or 15)

DATA_DIR = os.getenv("DATA_DIR", "/data")
STATE_PATH = os.path.join(DATA_DIR, "players_state.json")


# =====================
# STATE (persist message id)
# =====================
def _ensure_data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass

def load_state():
    _ensure_data_dir()
    if not os.path.exists(STATE_PATH):
        return {"message_id": None}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {"message_id": None}
    except Exception:
        return {"message_id": None}

def save_state(state: dict):
    _ensure_data_dir()
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


# =====================
# RCON (minimal)
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


def parse_listplayers(output: str):
    players = []
    if not output:
        return players

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue

        # Common outputs include "1. Name, SteamID" etc.
        if ". " in line:
            line = line.split(". ", 1)[1]

        if "," in line:
            name = line.split(",", 1)[0].strip()
        else:
            name = line.strip()

        bad = {"executing", "listplayers", "done"}
        if name and name.lower() not in bad:
            players.append(name)

    return players


# =====================
# Webhook upsert (edit if exists)
# =====================
async def upsert_webhook(session: aiohttp.ClientSession, embed: dict, state: dict):
    if not PLAYERS_WEBHOOK_URL:
        raise RuntimeError("PLAYERS_WEBHOOK_URL is missing")

    mid = state.get("message_id")

    # Try edit existing message
    if mid:
        async with session.patch(
            f"{PLAYERS_WEBHOOK_URL}/messages/{mid}",
            json={"embeds": [embed]},
        ) as r:
            if r.status == 404:
                # message id invalid (deleted or different webhook) -> recreate
                state["message_id"] = None
                save_state(state)
            elif r.status >= 300:
                try:
                    data = await r.json()
                except Exception:
                    data = await r.text()
                raise RuntimeError(f"Webhook edit failed: {r.status} {data}")
            else:
                return

    # Create new message
    async with session.post(
        PLAYERS_WEBHOOK_URL + "?wait=true",
        json={"embeds": [embed]},
    ) as r:
        if r.status >= 300:
            try:
                data = await r.json()
            except Exception:
                data = await r.text()
            raise RuntimeError(f"Webhook post failed: {r.status} {data}")

        data = await r.json()
        # Discord returns {"id": "..."} on success
        if "id" not in data:
            raise RuntimeError(f"Webhook post succeeded but no id returned: {data}")

        state["message_id"] = data["id"]
        save_state(state)


def build_players_embed(names: list[str]):
    count = len(names)
    lines = [f"{idx+1:02d}) {n}" for idx, n in enumerate(names[:50])]
    desc = f"**{count}/{PLAYER_CAP}** online\n\n" + ("\n".join(lines) if lines else "*No players returned.*")

    return {
        "title": "Online Players",
        "description": desc,
        "color": 0x2ECC71,
        "footer": {"text": f"Last update: {time.strftime('%H:%M:%S')}"},
    }


# =====================
# Public entrypoint
# =====================
_task = None

def run_players_loop():
    global _task
    if _task and not _task.done():
        return _task
    _task = asyncio.create_task(_players_loop())
    return _task


async def _players_loop():
    # Hard env check
    if not (RCON_HOST and RCON_PORT and RCON_PASSWORD):
        print("❌ Players loop missing RCON env vars (RCON_HOST/RCON_PORT/RCON_PASSWORD).")
        return

    state = load_state()

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                out = await rcon_command("ListPlayers", timeout=8.0)
                names = parse_listplayers(out)

                embed = build_players_embed(names)
                await upsert_webhook(session, embed, state)

            except Exception as e:
                # IMPORTANT: never let the loop die
                print(f"Players loop error: {e}")

            await asyncio.sleep(STATUS_POLL_SECONDS)