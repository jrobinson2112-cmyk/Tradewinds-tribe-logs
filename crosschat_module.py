import os
import json
import time
import asyncio
import socket
import discord

# =========================
# ENV / CONFIG
# =========================

# Discord channel where cross-chat lives
CROSSCHAT_CHANNEL_ID = int(os.getenv("CROSSCHAT_CHANNEL_ID", "1448575647285776444"))

# How often to poll GetChat (seconds)
CROSSCHAT_POLL_SECONDS = float(os.getenv("CROSSCHAT_POLL_SECONDS", "5"))

# Max length to send into game chat (ASA has limits; keep safe)
MAX_INGAME_LEN = int(os.getenv("CROSSCHAT_MAX_INGAME_LEN", "180"))

# Optional: multi-map RCON endpoints.
# If not set, we fall back to single-server env vars: RCON_HOST/RCON_PORT/RCON_PASSWORD
#
# Format example:
# CROSSCHAT_SERVERS='[
#   {"name":"Solunaris","host":"1.2.3.4","port":27020,"password":"xxxx"},
#   {"name":"Midgar","host":"5.6.7.8","port":27020,"password":"yyyy"}
# ]'
CROSSCHAT_SERVERS_JSON = os.getenv("CROSSCHAT_SERVERS", "").strip()

# Single-server fallback (if CROSSCHAT_SERVERS not provided)
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = os.getenv("RCON_PORT")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

# =========================
# INTERNAL STATE
# =========================
_servers = []
_last_seen_per_server = {}  # name -> set/hash window
_running = False


# =========================
# RCON (minimal, self-contained)
# =========================

def _rcon_make_packet(req_id: int, ptype: int, body: str) -> bytes:
    data = body.encode("utf-8", errors="ignore") + b"\x00"
    pkt = (
        req_id.to_bytes(4, "little", signed=True)
        + ptype.to_bytes(4, "little", signed=True)
        + data
        + b"\x00"
    )
    size = len(pkt)
    return size.to_bytes(4, "little", signed=True) + pkt


async def _rcon_command_host(host: str, port: int, password: str, command: str, timeout: float = 6.0) -> str:
    reader = writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)

        # auth
        writer.write(_rcon_make_packet(1, 3, password))
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
            txt = body.decode("utf-8", errors="ignore")
            if txt:
                out.append(txt)

        return "".join(out).strip()
    finally:
        try:
            if writer:
                writer.close()
                await writer.wait_closed()
        except Exception:
            pass


def _load_servers():
    global _servers

    if CROSSCHAT_SERVERS_JSON:
        try:
            arr = json.loads(CROSSCHAT_SERVERS_JSON)
            if not isinstance(arr, list) or not arr:
                raise ValueError("CROSSCHAT_SERVERS must be a non-empty JSON array")
            norm = []
            for s in arr:
                if not isinstance(s, dict):
                    continue
                name = str(s.get("name", "")).strip() or "Server"
                host = str(s.get("host", "")).strip()
                port = int(s.get("port", 0) or 0)
                pw = str(s.get("password", "")).strip()
                if host and port and pw:
                    norm.append({"name": name, "host": host, "port": port, "password": pw})
            if not norm:
                raise ValueError("No valid server entries in CROSSCHAT_SERVERS")
            _servers = norm
            return
        except Exception as e:
            print(f"[crosschat] Invalid CROSSCHAT_SERVERS JSON: {e} (falling back to single-server env vars)")

    # Fallback single server env
    if not (RCON_HOST and RCON_PORT and RCON_PASSWORD):
        _servers = []
        return

    _servers = [{
        "name": "Solunaris",
        "host": RCON_HOST,
        "port": int(RCON_PORT),
        "password": RCON_PASSWORD,
    }]


# =========================
# PARSING / DEDUPE
# =========================

def _normalize_lines(text: str):
    if not text:
        return []
    # keep non-empty
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _dedupe_key(server_name: str, line: str) -> str:
    # stable dedupe key
    return f"{server_name}|{line}".lower()


def _remember(server_name: str, key: str, window: int = 300):
    """
    Keep a rolling window set to avoid duplicate chat spam.
    """
    s = _last_seen_per_server.get(server_name)
    if s is None:
        s = []
        _last_seen_per_server[server_name] = s

    s.append((time.time(), key))

    # prune
    cutoff = time.time() - window
    while s and s[0][0] < cutoff:
        s.pop(0)


def _seen_recently(server_name: str, key: str, window: int = 300) -> bool:
    s = _last_seen_per_server.get(server_name)
    if not s:
        return False
    cutoff = time.time() - window
    for ts, k in s:
        if ts < cutoff:
            continue
        if k == key:
            return True
    return False


# =========================
# DISCORD -> GAME
# =========================

def _sanitize_ingame(text: str) -> str:
    # remove newlines, trim
    t = " ".join(text.split())
    # remove @everyone/@here spam
    t = t.replace("@everyone", "everyone").replace("@here", "here")
    if len(t) > MAX_INGAME_LEN:
        t = t[:MAX_INGAME_LEN - 1] + "…"
    return t


async def on_discord_message(message: discord.Message):
    """
    Call this from main.py's on_message event.
    """
    if not _servers:
        return
    if message.author.bot:
        return
    if message.channel.id != CROSSCHAT_CHANNEL_ID:
        return

    content = (message.content or "").strip()
    if not content:
        return

    author = message.author.display_name
    out = _sanitize_ingame(f"[Discord] {author}: {content}")

    # Broadcast to all maps
    for srv in _servers:
        try:
            # ASA supports ServerChat for global chat
            await _rcon_command_host(srv["host"], srv["port"], srv["password"], f"ServerChat {out}", timeout=6.0)
        except Exception as e:
            print(f"[crosschat] ServerChat error for {srv['name']}: {e}")


# =========================
# GAME -> DISCORD
# =========================

async def _poll_getchat_and_forward(client: discord.Client, srv: dict):
    ch = client.get_channel(CROSSCHAT_CHANNEL_ID)
    if not ch:
        return

    try:
        raw = await _rcon_command_host(srv["host"], srv["port"], srv["password"], "GetChat", timeout=8.0)
    except Exception as e:
        print(f"[crosschat] GetChat error for {srv['name']}: {e}")
        return

    lines = _normalize_lines(raw)
    if not lines:
        return

    # Forward each new line (with dedupe)
    for ln in lines:
        key = _dedupe_key(srv["name"], ln)
        if _seen_recently(srv["name"], key):
            continue

        _remember(srv["name"], key)

        # Prefix with map/server name
        try:
            await ch.send(f"**[{srv['name']}]** {ln}")
        except Exception as e:
            print(f"[crosschat] Discord send error: {e}")


async def run_crosschat_loop(client: discord.Client):
    """
    Main polling loop (game -> discord).
    This module DOES NOT register discord listeners (no add_listener).
    You MUST call on_discord_message() from main.py's on_message.
    """
    global _running
    if _running:
        return
    _running = True

    _load_servers()
    if not _servers:
        print("[crosschat] ❌ No RCON servers configured (set CROSSCHAT_SERVERS or RCON_HOST/RCON_PORT/RCON_PASSWORD).")
        return

    print(f"[crosschat] ✅ running (channel_id={CROSSCHAT_CHANNEL_ID}, poll={CROSSCHAT_POLL_SECONDS}s, servers={len(_servers)})")

    await client.wait_until_ready()

    while True:
        for srv in _servers:
            await _poll_getchat_and_forward(client, srv)
        await asyncio.sleep(CROSSCHAT_POLL_SECONDS)