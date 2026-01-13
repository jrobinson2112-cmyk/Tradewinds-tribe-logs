# tribelogs_module.py
# RCON GetGameLog -> per-tribe Discord webhook (forum thread supported)
# Persists /linktribelog routes to tribe_routes.json so they survive restarts/redeploys.
#
# Requirements: discord.py 2.x, aiohttp
# ENV required by this module:
#   DISCORD_TOKEN
#   GUILD_ID                  (your server/guild id)
#   ADMIN_ROLE_ID             (Discord Admin role id)
#   RCON_HOST
#   RCON_PORT
#   RCON_PASSWORD
#
# Optional:
#   TRIBE_ROUTES_FILE         default: tribe_routes.json
#   TRIBELOG_POLL_SECONDS     default: 10
#   TRIBELOG_HEARTBEAT_MIN    default: 60  (send heartbeat only if no activity)
#
# Notes:
# - This module is designed to be imported and started from your main bot file.
# - It adds /linktribelog and /unlinktribelog slash commands (admin-only).
# - It polls GetGameLog and forwards ONLY matching tribe lines to the configured route.
# - It strips <RichColor ...> tags and cleans trailing junk like </>) and extra !) etc.
# - It formats output to: "Day XXX, HH:MM:SS - Who action" (keeps special chars as best as Discord allows).
# - Discord webhooks to forum channels: we use thread_id (sent as ?thread_id=...).
#
# Usage in main.py:
#   from tribelogs_module import setup_tribelogs
#   setup_tribelogs(client, tree)
#   (then client.run)

from __future__ import annotations

import os
import re
import json
import time
import asyncio
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands


# =====================
# ENV / CONFIG
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = int(os.getenv("GUILD_ID", "1430388266393276509"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "1439069787207766076"))

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "27020"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

TRIBE_ROUTES_FILE = os.getenv("TRIBE_ROUTES_FILE", "tribe_routes.json")
TRIBELOG_POLL_SECONDS = float(os.getenv("TRIBELOG_POLL_SECONDS", "10"))
TRIBELOG_HEARTBEAT_MIN = int(os.getenv("TRIBELOG_HEARTBEAT_MIN", "60"))

# How many lines from GetGameLog to scan each poll (from the bottom).
# Helps avoid huge scans if GetGameLog is large.
SCAN_TAIL_LINES = int(os.getenv("TRIBELOG_SCAN_TAIL_LINES", "400"))

# Safety: cap messages per poll per tribe to avoid rate limits.
MAX_SEND_PER_POLL_PER_TRIBE = int(os.getenv("TRIBELOG_MAX_SEND_PER_POLL_PER_TRIBE", "6"))

# Dedupe memory size (per tribe). Keeps last N hashes.
DEDUP_MAX = int(os.getenv("TRIBELOG_DEDUP_MAX", "2000"))


def _require_env() -> None:
    missing = []
    for k in ["DISCORD_TOKEN", "RCON_HOST", "RCON_PORT", "RCON_PASSWORD", "GUILD_ID", "ADMIN_ROLE_ID"]:
        if not os.getenv(k) and k not in ("GUILD_ID", "ADMIN_ROLE_ID"):
            missing.append(k)
    # DISCORD_TOKEN / RCON_HOST / RCON_PORT / RCON_PASSWORD must exist
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not RCON_HOST:
        missing.append("RCON_HOST")
    if not os.getenv("RCON_PORT"):
        # allow default but still helpful if missing
        pass
    if not RCON_PASSWORD:
        missing.append("RCON_PASSWORD")

    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(sorted(set(missing))))


# =====================
# PERSISTENCE (routes)
# =====================
def load_tribe_routes() -> List[Dict[str, str]]:
    if not os.path.exists(TRIBE_ROUTES_FILE):
        return []
    try:
        with open(TRIBE_ROUTES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            # normalize
            out: List[Dict[str, str]] = []
            for item in data:
                if isinstance(item, dict) and "tribe" in item and "webhook" in item and "thread_id" in item:
                    out.append(
                        {
                            "tribe": str(item["tribe"]),
                            "webhook": str(item["webhook"]),
                            "thread_id": str(item["thread_id"]),
                        }
                    )
            return out
        return []
    except Exception:
        return []


def save_tribe_routes(routes: List[Dict[str, str]]) -> None:
    with open(TRIBE_ROUTES_FILE, "w", encoding="utf-8") as f:
        json.dump(routes, f, indent=2, ensure_ascii=False)


# =====================
# RCON (minimal Source RCON)
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


async def rcon_command(command: str, timeout: float = 8.0) -> str:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout)
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

        chunks: List[bytes] = []
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
        out: List[str] = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i : i + 4], "little", signed=True)
            i += 4
            if i + size > len(data) or size < 10:
                break
            pkt = data[i : i + size]
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


# =====================
# LOG PARSING / CLEANUP
# =====================

# Examples in your logs:
# 2026.01.10_08.24.46: Tribe Valkyrie, ID 123...: Day 216, 18:13:36: Einar froze ...
# sometimes embedded RichColor:
# ...: <RichColor Color="1, 0, 1, 1">Sir Magnus claimed 'Roan Pinto ...'!</>)
#
# We want final: "Day 216, 18:13:36 - Einar froze Juvenile Pyraxis - Lvl 165"
#
# For simplicity: extract:
#   Day X, HH:MM:SS:
#   then remainder after that colon
# Then clean RichColor tags, remove extra trailing junk, and trim.
_DAYTIME_RE = re.compile(r"(Day\s+\d+,\s+\d{1,2}:\d{2}:\d{2})\s*:\s*(.+)$", re.IGNORECASE)
_RICHCOLOR_RE = re.compile(r"<\s*/?\s*RichColor[^>]*>", re.IGNORECASE)
# remove typical weird endings from ark log lines
_TRAIL_JUNK_RE = re.compile(r"(\<\/\>\)|\!\)\)|\!\>\)|\)\)|\>\)|\!\)|\>\>|\)\>)\s*$")


def _clean_text(s: str) -> str:
    s = s.strip()

    # Remove RichColor tags or other UE markup tags
    s = _RICHCOLOR_RE.sub("", s)

    # Some logs have other <> tags; strip any remaining simple tags
    s = re.sub(r"<[^>]+>", "", s)

    # Normalize whitespace
    s = re.sub(r"\s+", " ", s).strip()

    # Remove trailing junk like </>) or !) etc
    s = _TRAIL_JUNK_RE.sub("", s).strip()

    # Strip unmatched trailing punctuation often left behind
    s = s.rstrip(" )>")

    return s.strip()


def extract_day_time_who_action(line: str) -> Optional[str]:
    """
    Returns formatted string: "Day X, HH:MM:SS - Who action"
    If no Day/Time found, returns None.
    """
    line = line.strip()
    if not line:
        return None

    m = _DAYTIME_RE.search(line)
    if not m:
        return None

    daytime = m.group(1).strip()
    rest = m.group(2).strip()

    rest = _clean_text(rest)

    # We want just "Who and what" – remove leading "Tribe X, ID ...:" if present
    # Often rest starts like: "Einar froze ..." already, but sometimes it might include "Tribe ..."
    rest = re.sub(r"^Tribe\s+[^:]+:\s*", "", rest, flags=re.IGNORECASE).strip()

    # Remove leading server timestamp prefix if it leaked into the "rest"
    rest = re.sub(r"^\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}:\s*", "", rest).strip()

    # Remove quotes around tames/claims if you want; you said keep: Who and action.
    # We'll keep the content but remove surrounding quotes to look cleaner.
    rest = rest.replace("“", '"').replace("”", '"')

    # Remove trailing "'!" patterns leaving clean end
    rest = rest.rstrip("!").strip()

    # Final output
    return f"{daytime} - {rest}"


# =====================
# COLOR ROUTING
# =====================
# You wanted:
# Red - Killed / Died / Death / Destroyed
# Yellow - Demolished + Unclaimed
# Purple - Claimed
# Green - Tamed
# Light blue - Alliance
# White - Anything else (Froze)
COLOR_RED = 0xE74C3C
COLOR_YELLOW = 0xF1C40F
COLOR_PURPLE = 0x9B59B6
COLOR_GREEN = 0x2ECC71
COLOR_LIGHTBLUE = 0x5DADEC
COLOR_WHITE = 0xFFFFFF


def choose_color(text: str) -> int:
    t = text.lower()

    if any(k in t for k in ["killed", "died", "death", "destroyed"]):
        return COLOR_RED
    if "demolished" in t or "unclaimed" in t:
        return COLOR_YELLOW
    if "claimed" in t:
        return COLOR_PURPLE
    if "tamed" in t or "taming" in t:
        return COLOR_GREEN
    if "alliance" in t:
        return COLOR_LIGHTBLUE
    return COLOR_WHITE


# =====================
# WEBHOOK POSTING (thread support)
# =====================
async def post_webhook_embed(
    session: aiohttp.ClientSession,
    webhook_url: str,
    thread_id: str,
    description: str,
    color: int,
) -> None:
    # For forum thread webhooks, pass thread_id in query.
    # Also accept user-provided webhook that already contains ?thread_id= or ?Thread=
    url = webhook_url
    if "thread_id=" not in url.lower():
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}thread_id={thread_id}"

    payload = {"embeds": [{"description": description, "color": color}]}

    async with session.post(url, json=payload) as r:
        if r.status == 204:
            return
        data = None
        try:
            data = await r.json()
        except Exception:
            txt = await r.text()
            data = {"text": txt}

        # Handle rate limit politely
        if r.status == 429 and isinstance(data, dict) and "retry_after" in data:
            await asyncio.sleep(float(data.get("retry_after", 1.0)))
            return

        # Hard error
        raise RuntimeError(f"Discord webhook error {r.status}: {data}")


async def post_heartbeat(
    session: aiohttp.ClientSession,
    route: Dict[str, str],
    minutes: int,
) -> None:
    desc = f"🫀 No new logs in the last {minutes} minutes. (Still polling)"
    await post_webhook_embed(
        session=session,
        webhook_url=route["webhook"],
        thread_id=route["thread_id"],
        description=desc,
        color=COLOR_WHITE,
    )


# =====================
# MODULE SETUP / LOOP
# =====================
TRIBE_ROUTES: List[Dict[str, str]] = []
_seen_hashes: Dict[str, List[int]] = {}  # tribe -> list of hashes (int)
_last_activity_ts: Dict[str, float] = {}  # tribe -> time.time()
_last_log_tail_fingerprint: Optional[int] = None  # dedupe GetGameLog repeats


def _hash_line(s: str) -> int:
    # fast stable hash; python hash is randomized per process so use crc32
    import zlib

    return zlib.crc32(s.encode("utf-8", errors="ignore"))


def _dedupe_check(tribe: str, h: int) -> bool:
    arr = _seen_hashes.setdefault(tribe, [])
    if h in arr:
        return False
    arr.append(h)
    if len(arr) > DEDUP_MAX:
        del arr[: len(arr) - DEDUP_MAX]
    return True


def _route_for_line(line: str) -> Optional[Dict[str, str]]:
    # Match if "Tribe <name>" appears anywhere (case-insensitive)
    # You can tighten this if needed.
    low = line.lower()
    for r in TRIBE_ROUTES:
        tribe = str(r.get("tribe", "")).strip()
        if not tribe:
            continue
        if tribe.lower() in low:
            return r
    return None


async def tribelog_poll_loop(client: discord.Client, tree: app_commands.CommandTree) -> None:
    await client.wait_until_ready()
    print(f"Tribelogs loop started. Polling every {TRIBELOG_POLL_SECONDS}s. Routes: {[r.get('tribe') for r in TRIBE_ROUTES]}")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if not TRIBE_ROUTES:
                    await asyncio.sleep(TRIBELOG_POLL_SECONDS)
                    continue

                text = await rcon_command("GetGameLog", timeout=10.0)
                if not text:
                    await asyncio.sleep(TRIBELOG_POLL_SECONDS)
                    continue

                lines_all = [ln for ln in text.splitlines() if ln.strip()]
                tail = lines_all[-SCAN_TAIL_LINES:] if len(lines_all) > SCAN_TAIL_LINES else lines_all

                # fingerprint tail to detect "no change" even if GetGameLog re-sends same content
                tail_fp = _hash_line("\n".join(tail))
                global _last_log_tail_fingerprint
                if _last_log_tail_fingerprint is not None and tail_fp == _last_log_tail_fingerprint:
                    # no change -> maybe heartbeat
                    now = time.time()
                    for r in TRIBE_ROUTES:
                        tribe = r["tribe"]
                        last = _last_activity_ts.get(tribe, 0.0)
                        if last > 0 and (now - last) >= (TRIBELOG_HEARTBEAT_MIN * 60):
                            try:
                                await post_heartbeat(session, r, TRIBELOG_HEARTBEAT_MIN)
                                _last_activity_ts[tribe] = now  # reset heartbeat timer
                                print(f"Heartbeat sent for {tribe}")
                            except Exception as e:
                                print(f"Heartbeat error for {tribe}: {e}")
                    await asyncio.sleep(TRIBELOG_POLL_SECONDS)
                    continue

                _last_log_tail_fingerprint = tail_fp

                sends_per_tribe: Dict[str, int] = {}

                # Scan tail and forward matching lines
                for raw in tail:
                    route = _route_for_line(raw)
                    if not route:
                        continue

                    tribe = route["tribe"]
                    # extract formatted
                    formatted = extract_day_time_who_action(raw)
                    if not formatted:
                        # if no Day/Time, ignore
                        continue

                    # dedupe per tribe
                    h = _hash_line(formatted)
                    if not _dedupe_check(tribe, h):
                        continue

                    # cap per poll
                    sends_per_tribe.setdefault(tribe, 0)
                    if sends_per_tribe[tribe] >= MAX_SEND_PER_POLL_PER_TRIBE:
                        continue

                    color = choose_color(formatted)
                    try:
                        await post_webhook_embed(
                            session=session,
                            webhook_url=route["webhook"],
                            thread_id=route["thread_id"],
                            description=formatted,
                            color=color,
                        )
                        sends_per_tribe[tribe] += 1
                        _last_activity_ts[tribe] = time.time()
                    except Exception as e:
                        print(f"Send error for {tribe}: {e}")

            except Exception as e:
                print(f"Tribelogs loop error: {e}")

            await asyncio.sleep(TRIBELOG_POLL_SECONDS)


# =====================
# SLASH COMMANDS
# =====================
def _is_admin(i: discord.Interaction) -> bool:
    try:
        return any(r.id == ADMIN_ROLE_ID for r in i.user.roles)
    except Exception:
        return False


def setup_tribelogs(client: discord.Client, tree: app_commands.CommandTree) -> None:
    """
    Call this from your main bot file AFTER client/tree are created, BEFORE client.run().
    It registers slash commands and starts the polling task on_ready.
    """
    _require_env()

    global TRIBE_ROUTES
    TRIBE_ROUTES = load_tribe_routes()
    print("Tribe routes loaded:", [r.get("tribe") for r in TRIBE_ROUTES])

    @tree.command(name="linktribelog", guild=discord.Object(id=GUILD_ID))
    @app_commands.describe(
        tribe="Exact tribe text to match (e.g. 'Tribe Valkyrie')",
        webhook="Discord webhook URL for that tribe",
        thread_id="Forum thread ID to post into",
    )
    async def linktribelog(i: discord.Interaction, tribe: str, webhook: str, thread_id: str):
        if not _is_admin(i):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        tribe = tribe.strip()
        webhook = webhook.strip()
        thread_id = str(thread_id).strip()

        if not tribe or not webhook or not thread_id:
            await i.response.send_message("❌ Missing tribe/webhook/thread_id", ephemeral=True)
            return

        global TRIBE_ROUTES
        TRIBE_ROUTES = [r for r in TRIBE_ROUTES if str(r.get("tribe", "")).lower() != tribe.lower()]
        TRIBE_ROUTES.append({"tribe": tribe, "webhook": webhook, "thread_id": thread_id})
        save_tribe_routes(TRIBE_ROUTES)

        # reset dedupe for that tribe so you don't miss the next line
        _seen_hashes.pop(tribe, None)
        _last_activity_ts[tribe] = time.time()

        await i.response.send_message(f"✅ Saved route for **{tribe}**.", ephemeral=True)
        print("Linked tribe route:", {"tribe": tribe, "webhook": webhook, "thread_id": thread_id})

    @tree.command(name="unlinktribelog", guild=discord.Object(id=GUILD_ID))
    @app_commands.describe(tribe="Tribe name to remove")
    async def unlinktribelog(i: discord.Interaction, tribe: str):
        if not _is_admin(i):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return
        tribe = tribe.strip()
        global TRIBE_ROUTES
        before = len(TRIBE_ROUTES)
        TRIBE_ROUTES = [r for r in TRIBE_ROUTES if str(r.get("tribe", "")).lower() != tribe.lower()]
        save_tribe_routes(TRIBE_ROUTES)
        _seen_hashes.pop(tribe, None)
        _last_activity_ts.pop(tribe, None)
        removed = before - len(TRIBE_ROUTES)
        await i.response.send_message(f"✅ Removed {removed} route(s) for **{tribe}**.", ephemeral=True)

    @tree.command(name="listroutes", guild=discord.Object(id=GUILD_ID))
    async def listroutes(i: discord.Interaction):
        if not _is_admin(i):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return
        if not TRIBE_ROUTES:
            await i.response.send_message("No tribe routes configured yet.", ephemeral=True)
            return
        lines = []
        for r in TRIBE_ROUTES:
            lines.append(f"- **{r['tribe']}** → thread `{r['thread_id']}`")
        await i.response.send_message("\n".join(lines), ephemeral=True)

    @client.event
    async def on_ready():
        # start poll loop once
        if not getattr(client, "_tribelogs_started", False):
            client._tribelogs_started = True
            client.loop.create_task(tribelog_poll_loop(client, tree))
            print("✅ tribelogs_module online")


# If you want to run ONLY this module directly for testing:
# create a minimal client/tree and call setup_tribelogs().
if __name__ == "__main__":
    # Minimal test runner
    intents = discord.Intents.default()
    _client = discord.Client(intents=intents)
    _tree = app_commands.CommandTree(_client)

    setup_tribelogs(_client, _tree)

    @_client.event
    async def on_ready():
        await _tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"✅ Commands synced to guild {GUILD_ID}")

    _client.run(DISCORD_TOKEN)