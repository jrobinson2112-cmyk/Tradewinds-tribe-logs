import os
import re
import json
import time
import hashlib
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import discord

# =========================
# ENV
# =========================
CROSSCHAT_CHANNEL_ID = int(os.getenv("CROSSCHAT_CHANNEL_ID", "0") or "0")

# Polling cadence for GetChat per map
CROSSCHAT_POLL_SECONDS = float(os.getenv("CROSSCHAT_POLL_SECONDS", "5") or "5")

# How many recent chat lines to keep in memory per map (dedupe window)
CROSSCHAT_DEDUPE_MAX = int(os.getenv("CROSSCHAT_DEDUPE_MAX", "300") or "300")

# Where to persist dedupe + last seen hashes (mount your Railway volume to match this)
DATA_DIR = os.getenv("DATA_DIR", "/data")
STATE_PATH = os.path.join(DATA_DIR, "crosschat_state.json")

# Prefix used when the bridge sends messages into game (we ignore these on the way back)
BRIDGE_PREFIX = os.getenv("CROSSCHAT_BRIDGE_PREFIX", "[Discord]")

# Optional: if True, also mirror Discord messages into Discord (usually False)
ECHO_DISCORD_TO_DISCORD = os.getenv("CROSSCHAT_ECHO_DISCORD_TO_DISCORD", "false").lower() == "true"

# =========================
# MAP CONFIG
# =========================
# Provide maps via JSON:
# CROSSCHAT_MAPS_JSON='[
#   {"name":"Midgar","host":"1.2.3.4","port":27020,"password":"xxxx"},
#   {"name":"Solunaris","host":"5.6.7.8","port":27020,"password":"yyyy"}
# ]'
CROSSCHAT_MAPS_JSON = os.getenv("CROSSCHAT_MAPS_JSON", "").strip()

# If you prefer individual variables instead of JSON, you can do:
# CROSSCHAT_MAP_1_NAME, _HOST, _PORT, _PASSWORD etc.
def _load_maps_from_env() -> List[Dict[str, Any]]:
    maps: List[Dict[str, Any]] = []

    if CROSSCHAT_MAPS_JSON:
        try:
            maps = json.loads(CROSSCHAT_MAPS_JSON)
            if not isinstance(maps, list):
                raise ValueError("CROSSCHAT_MAPS_JSON must be a JSON list")
        except Exception as e:
            raise RuntimeError(f"Invalid CROSSCHAT_MAPS_JSON: {e}")

    # Fallback: CROSSCHAT_MAP_1_..., CROSSCHAT_MAP_2_...
    if not maps:
        for i in range(1, 11):
            name = os.getenv(f"CROSSCHAT_MAP_{i}_NAME", "").strip()
            host = os.getenv(f"CROSSCHAT_MAP_{i}_HOST", "").strip()
            port = os.getenv(f"CROSSCHAT_MAP_{i}_PORT", "").strip()
            password = os.getenv(f"CROSSCHAT_MAP_{i}_PASSWORD", "").strip()
            if not name or not host or not port or not password:
                continue
            maps.append({"name": name, "host": host, "port": int(port), "password": password})

    return maps


@dataclass
class MapConfig:
    name: str
    host: str
    port: int
    password: str


# =========================
# STATE
# =========================
_state: Dict[str, Any] = {
    "maps": {}  # map_name -> {"seen": [hashes...]}
}

def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)

def _load_state():
    global _state
    _ensure_data_dir()
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                _state = json.load(f)
        except Exception:
            _state = {"maps": {}}
    if "maps" not in _state:
        _state["maps"] = {}

def _save_state():
    _ensure_data_dir()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_state, f)
    os.replace(tmp, STATE_PATH)

def _get_seen_list(map_name: str) -> List[str]:
    m = _state["maps"].setdefault(map_name, {})
    seen = m.setdefault("seen", [])
    if not isinstance(seen, list):
        seen = []
        m["seen"] = seen
    return seen

def _hash_line(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "ignore")).hexdigest()[:16]

def _dedupe_add(map_name: str, line_hash: str) -> bool:
    """Returns True if NEW, False if already seen."""
    seen = _get_seen_list(map_name)
    if line_hash in seen:
        return False
    seen.append(line_hash)
    # trim
    if len(seen) > CROSSCHAT_DEDUPE_MAX:
        del seen[: len(seen) - CROSSCHAT_DEDUPE_MAX]
    return True


# =========================
# PARSING GetChat
# =========================
# ASA GetChat format varies. We accept many shapes.
# Common patterns we support:
#  - "PlayerName: message"
#  - "[something] PlayerName: message"
#  - "PlayerName (Tribe): message"
#
# We also ignore lines that look like system spam, and any containing BRIDGE_PREFIX
_CHAT_LINE_RE = re.compile(
    r"""
    ^(?:\[[^\]]+\]\s*)?            # optional [prefix]
    (?P<player>[^:]{2,64})         # player up to colon
    \s*:\s*
    (?P<msg>.+?)\s*$
    """,
    re.VERBOSE,
)

def parse_getchat_output(raw: str) -> List[Tuple[str, str]]:
    """
    Returns list of (player, msg) newest-last.
    """
    if not raw:
        return []

    # Split lines, keep non-empty
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    out: List[Tuple[str, str]] = []

    for ln in lines:
        # Ignore bridge echoes (Discord->Game coming back)
        if BRIDGE_PREFIX in ln:
            continue

        # Some servers echo "Server:" or "ADMIN:" etc - ignore if no player:
        m = _CHAT_LINE_RE.match(ln)
        if not m:
            continue

        player = m.group("player").strip()
        msg = m.group("msg").strip()

        # Basic cleanup
        if not player or not msg:
            continue

        # Ignore obvious non-chat junk
        if player.lower() in ("server", "admin", "system"):
            continue

        out.append((player, msg))

    return out


# =========================
# RCON CALL WRAPPER
# =========================
async def _call_rcon(rcon_command: Callable[..., Any], host: str, port: int, password: str, cmd: str) -> str:
    """
    Supports:
      - sync rcon_command(host, port, password, cmd) -> str
      - async rcon_command(host, port, password, cmd) -> str
    """
    try:
        res = rcon_command(host, port, password, cmd)
        if asyncio.iscoroutine(res):
            res = await res
        return res if isinstance(res, str) else str(res)
    except Exception as e:
        raise RuntimeError(str(e))


# =========================
# DISCORD -> GAME
# =========================
def _sanitize_for_game(s: str) -> str:
    # Remove newlines, excessive spaces, and Discord mentions formatting
    s = s.replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", " ", s).strip()
    # Prevent @everyone / @here spam in game
    s = s.replace("@everyone", "everyone").replace("@here", "here")
    return s[:180]  # keep it safe-ish for in-game chat limits

async def relay_discord_to_all_maps(
    rcon_command: Callable[..., Any],
    maps: List[MapConfig],
    author_name: str,
    content: str,
):
    if not content.strip():
        return
    clean = _sanitize_for_game(content)
    if not clean:
        return

    msg = f"{BRIDGE_PREFIX} {author_name}: {clean}"

    # Use ServerChat (preferred), fallback to Broadcast if needed
    for mc in maps:
        try:
            await _call_rcon(rcon_command, mc.host, mc.port, mc.password, f"admincheat ServerChat {msg}")
        except Exception:
            # fallback
            try:
                await _call_rcon(rcon_command, mc.host, mc.port, mc.password, f"admincheat Broadcast {msg}")
            except Exception as e2:
                print(f"[crosschat] RCON send failed for {mc.name}: {e2}")


# =========================
# GAME -> DISCORD
# =========================
async def relay_game_to_discord(
    client: discord.Client,
    channel_id: int,
    map_name: str,
    player: str,
    msg: str,
):
    ch = client.get_channel(channel_id)
    if ch is None:
        try:
            ch = await client.fetch_channel(channel_id)
        except Exception:
            return
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        return

    # Format: smooth fluent, map-tagged, but clean
    # Example: 🗺️ Midgar | Einar: hello
    text = f"🗺️ **{map_name}** | **{player}**: {msg}"
    await ch.send(text)


# =========================
# MAIN LOOP (GetChat polling)
# =========================
_maps: List[MapConfig] = []
_rcon_cmd: Optional[Callable[..., Any]] = None

def init_crosschat(rcon_command: Callable[..., Any]):
    global _maps, _rcon_cmd
    _load_state()

    maps_raw = _load_maps_from_env()
    if not maps_raw:
        raise RuntimeError("No maps configured for crosschat. Set CROSSCHAT_MAPS_JSON or CROSSCHAT_MAP_1_* vars.")

    _maps = [MapConfig(m["name"], m["host"], int(m["port"]), m["password"]) for m in maps_raw]
    _rcon_cmd = rcon_command

    if not CROSSCHAT_CHANNEL_ID:
        raise RuntimeError("Missing CROSSCHAT_CHANNEL_ID env var")

    print(f"[crosschat] ✅ Loaded maps: {[m.name for m in _maps]}")
    print(f"[crosschat] ✅ Channel ID: {CROSSCHAT_CHANNEL_ID} | Poll: {CROSSCHAT_POLL_SECONDS}s | Dedupe: {CROSSCHAT_DEDUPE_MAX}")

async def run_crosschat_loop(client: discord.Client, rcon_command: Callable[..., Any]):
    """
    Call once from main.py after login/ready.
    """
    init_crosschat(rcon_command)

    await client.wait_until_ready()

    # On boot, seed dedupe by reading GetChat once per map (avoid backspam)
    for mc in _maps:
        try:
            raw = await _call_rcon(rcon_command, mc.host, mc.port, mc.password, "admincheat GetChat")
            pairs = parse_getchat_output(raw)
            for p, m in pairs:
                h = _hash_line(f"{mc.name}|{p}|{m}")
                _dedupe_add(mc.name, h)
        except Exception as e:
            print(f"[crosschat] Seed failed for {mc.name}: {e}")

    _save_state()
    print("[crosschat] First run: seeded dedupe from current GetChat (no backlog spam).")

    while True:
        try:
            for mc in _maps:
                try:
                    raw = await _call_rcon(rcon_command, mc.host, mc.port, mc.password, "admincheat GetChat")
                    pairs = parse_getchat_output(raw)
                    if not pairs:
                        continue

                    # Send any NEW lines
                    for player, msg in pairs:
                        # Ignore messages we previously injected
                        if msg.startswith(BRIDGE_PREFIX) or BRIDGE_PREFIX in msg:
                            continue

                        h = _hash_line(f"{mc.name}|{player}|{msg}")
                        if not _dedupe_add(mc.name, h):
                            continue

                        await relay_game_to_discord(client, CROSSCHAT_CHANNEL_ID, mc.name, player, msg)

                    _save_state()

                except Exception as e_map:
                    print(f"[crosschat] GetChat error for {mc.name}: {e_map}")

        except Exception as e_loop:
            print(f"[crosschat] Loop error: {e_loop}")

        await asyncio.sleep(CROSSCHAT_POLL_SECONDS)


# =========================
# DISCORD MESSAGE HANDLER (call from main.py on_message)
# =========================
async def on_discord_message(message: discord.Message, rcon_command: Callable[..., Any]):
    """
    Relay Discord -> all maps.
    Call this from your main.py @client.event on_message
    """
    if message.author.bot:
        return
    if not message.guild:
        return
    if message.channel.id != CROSSCHAT_CHANNEL_ID:
        return

    content = (message.content or "").strip()
    if not content:
        return

    # Optional: ignore commands starting with /
    if content.startswith("/"):
        return

    # Ensure maps initialized (in case message comes early)
    global _maps
    if not _maps:
        try:
            init_crosschat(rcon_command)
        except Exception:
            return

    author_name = message.author.display_name

    if ECHO_DISCORD_TO_DISCORD:
        # (normally you don't want this)
        await message.channel.send(f"(bridge) sending to all maps…")

    await relay_discord_to_all_maps(rcon_command, _maps, author_name, content)