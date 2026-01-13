import os
import re
import json
import time
import html
import asyncio
import hashlib
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from rcon_client import safe_rcon
from config import ADMIN_ROLE_ID

# ============================================================
# ENV / SETTINGS
# ============================================================

TRIBE_ROUTES_ENV = os.getenv("TRIBE_ROUTES", "").strip()

GAMELOG_POLL_SECONDS = float(os.getenv("GAMELOG_POLL_SECONDS", "10"))
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "60"))

# If true, it will send the last N matching lines on startup (can be spammy).
BACKLOG_ON_START = os.getenv("TRIBE_BACKLOG_ON_START", "false").strip().lower() in ("1", "true", "yes", "y")
BACKLOG_LINES = int(os.getenv("TRIBE_BACKLOG_LINES", "50"))

ROUTES_FILE = "tribe_routes.json"  # persisted routes so /linktribelog survives restarts

# ============================================================
# REGEX / PARSING
# ============================================================

# Matches: Day 216, 17:42:24:
DAYTIME_RE = re.compile(r"Day\s+(\d+),\s+(\d{1,2}):(\d{2}):(\d{2})\s*:\s*(.*)", re.IGNORECASE)

# Remove RichColor tags and any other <...> tags
TAG_RE = re.compile(r"<[^>]+>")

# Some logs have big prefixes like:
# [2026.01.10-08.24.46:966][441]2026.01.10_08.24.46: Tribe Valkyrie, ID 123...: Day 216, 18:13:36: ...
# We'll just search for the Day... part anywhere in the line.


# ============================================================
# COLOR ROUTING (as requested)
# ============================================================

COLOR_RED = 0xE74C3C
COLOR_YELLOW = 0xF1C40F
COLOR_PURPLE = 0x9B59B6
COLOR_GREEN = 0x2ECC71
COLOR_LIGHT_BLUE = 0x5DADE2
COLOR_WHITE = 0xFFFFFF

def pick_color(text: str) -> int:
    t = text.lower()

    # Red - Killed / Died / Death / Destroyed
    if any(k in t for k in (" killed", "killed ", " died", "died ", " death", " destroyed", "destroyed ")):
        return COLOR_RED

    # Yellow - Demolished + Unclaimed
    if "demolished" in t or "unclaimed" in t:
        return COLOR_YELLOW

    # Purple - Claimed
    if "claimed" in t:
        return COLOR_PURPLE

    # Green - Tamed
    if "tamed" in t or "taming" in t:
        return COLOR_GREEN

    # Light blue - Alliance
    if "alliance" in t:
        return COLOR_LIGHT_BLUE

    # White - anything else (including Froze)
    return COLOR_WHITE


# ============================================================
# ROUTES LOADING / SAVING
# ============================================================

def _load_routes_from_file() -> List[Dict[str, str]]:
    if not os.path.exists(ROUTES_FILE):
        return []
    try:
        with open(ROUTES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
    except Exception:
        pass
    return []

def _save_routes_to_file(routes: List[Dict[str, str]]) -> None:
    try:
        with open(ROUTES_FILE, "w", encoding="utf-8") as f:
            json.dump(routes, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Failed to save routes file: {e}")

def load_routes() -> List[Dict[str, str]]:
    """
    Priority:
      1) TRIBE_ROUTES env (JSON array)
      2) tribe_routes.json file (created by /linktribelog)
    """
    routes: List[Dict[str, str]] = []

    if TRIBE_ROUTES_ENV:
        try:
            parsed = json.loads(TRIBE_ROUTES_ENV)
            if isinstance(parsed, list):
                routes = [r for r in parsed if isinstance(r, dict)]
        except Exception as e:
            print(f"TRIBE_ROUTES parse error: {e}")

    if not routes:
        routes = _load_routes_from_file()

    # Normalize
    norm = []
    for r in routes:
        tribe = str(r.get("tribe", "")).strip()
        webhook = str(r.get("webhook", "")).strip()
        thread_id = str(r.get("thread_id", "")).strip()
        if tribe and webhook:
            norm.append({"tribe": tribe, "webhook": webhook, "thread_id": thread_id})
    return norm


# ============================================================
# TEXT CLEANING / NORMALIZATION
# ============================================================

def _fix_mojibake(s: str) -> str:
    """
    Best-effort fix for weird character rendering.
    If the source bytes were latin-1 decoded incorrectly, this can help.
    Won't magically recover if the server already replaced it with '?'.
    """
    if not s:
        return s

    # Decode HTML entities like &Oslash;
    s2 = html.unescape(s)

    # Heuristic: if it looks like mojibake, try latin1->utf8 roundtrip
    if "Ã" in s2 or "�" in s2:
        try:
            s3 = s2.encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")
            if s3 and s3.count("�") < s2.count("�"):
                return s3
        except Exception:
            pass

    return s2

def clean_action_text(action: str) -> str:
    """
    - removes <RichColor ...> and other tags
    - strips odd trailing junk like </>) , !) , extra )))
    """
    if not action:
        return ""

    action = _fix_mojibake(action)

    # Remove tags like <RichColor Color="...">
    action = TAG_RE.sub("", action).strip()

    # Some lines start with extra quotes etc
    action = action.replace("\u200b", "").strip()

    # Remove repeated closing parentheses like '))' -> ')'
    while action.endswith("))"):
        action = action[:-1]

    # Remove trailing junk like </>) or !) or > or extra ) at the end
    action = re.sub(r"[>\s]*</\s*\)*\s*>*\s*$", "", action).strip()  # strips things like </>)
    action = re.sub(r"[!\s]*\)*\s*$", "", action).strip()           # strips trailing !) or ))

    # Final trim
    return action.strip()


def extract_day_time_and_action(line: str) -> Optional[Tuple[int, int, int, int, str]]:
    """
    Returns (day, hour, minute, second, action_text) if a Day/time segment exists.
    Searches the line for 'Day X, HH:MM:SS: ...'
    """
    if not line:
        return None

    line = _fix_mojibake(line)

    m = DAYTIME_RE.search(line)
    if not m:
        return None

    day = int(m.group(1))
    hour = int(m.group(2))
    minute = int(m.group(3))
    second = int(m.group(4))
    action = m.group(5).strip()

    action = clean_action_text(action)
    if not action:
        return None

    return day, hour, minute, second, action


def format_output(day: int, hour: int, minute: int, second: int, action: str) -> str:
    # EXACT requested format:
    # Day 221, 22:51:49 - Sir Magnus claimed 'Roan Pinto - Lvl 150'
    return f"Day {day}, {hour:02d}:{minute:02d}:{second:02d} - {action}"


# ============================================================
# TRIBE MATCHING
# ============================================================

def matches_tribe(line: str, tribe: str) -> bool:
    """
    Tribe lines usually contain: 'Tribe Valkyrie'
    But we also allow just 'Valkyrie' as fallback.
    """
    l = line.lower()
    t = tribe.lower()

    if f"tribe {t}" in l:
        return True
    return t in l


# ============================================================
# WEBHOOK POSTING (forum thread support + rate limit handling)
# ============================================================

def normalize_webhook_base(url: str) -> str:
    # strip any query params off; we will pass thread_id correctly via params
    return url.split("?", 1)[0].strip()

async def post_to_webhook(
    session: aiohttp.ClientSession,
    webhook_url: str,
    thread_id: str,
    embed: Dict[str, Any],
) -> None:
    base = normalize_webhook_base(webhook_url)

    params = {}
    if thread_id:
        # Discord expects thread_id for forum posts
        params["thread_id"] = thread_id

    # We do not need ?wait=true for normal “fire and forget” posts.
    # But keep it off to reduce response payload size.

    payload = {"embeds": [embed]}

    # Handle Discord rate limits (429)
    while True:
        async with session.post(base, params=params, json=payload) as r:
            if r.status == 204 or (200 <= r.status < 300):
                return

            data = None
            try:
                data = await r.json()
            except Exception:
                data = {"message": await r.text()}

            if r.status == 429:
                retry_after = 1.0
                try:
                    retry_after = float(data.get("retry_after", 1.0))
                except Exception:
                    pass
                await asyncio.sleep(max(0.2, retry_after))
                continue

            raise RuntimeError(f"Discord webhook error {r.status}: {data}")


# ============================================================
# DEDUPE
# ============================================================

class Dedupe:
    def __init__(self, max_items: int = 5000):
        self.max_items = max_items
        self._seen = set()
        self._order = []

    def _hash(self, s: str) -> str:
        return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

    def seen(self, key: str) -> bool:
        h = self._hash(key)
        if h in self._seen:
            return True
        self._seen.add(h)
        self._order.append(h)
        if len(self._order) > self.max_items:
            old = self._order.pop(0)
            self._seen.discard(old)
        return False


# ============================================================
# MAIN LOOP
# ============================================================

async def run_tribelogs_loop(client, tree=None):
    """
    - Polls GetGameLog
    - Routes by TRIBE_ROUTES
    - Sends only new entries (dedupe)
    - Heartbeat every 60 mins only if no activity
    """
    await client.wait_until_ready()

    routes = load_routes()
    print("Tribe routes loaded:", [r.get("tribe") for r in routes])

    if not routes:
        print("⚠️ No tribe routes configured. Set TRIBE_ROUTES or use /linktribelog.")
        # still keep running so /linktribelog can add routes live

    dedupe = Dedupe(max_items=8000)

    last_activity_ts = time.time()  # last time we sent any log
    last_heartbeat_ts = 0.0

    # Optionally seed dedupe from current log to avoid spam
    try:
        seed_text = await safe_rcon("GetGameLog", timeout=10.0)
        if seed_text:
            lines = [ln.strip() for ln in seed_text.splitlines() if ln.strip()]
            if BACKLOG_ON_START:
                # send backlog for last BACKLOG_LINES matching lines (per route)
                backlog = lines[-3000:]  # cap scanning
                print(f"Backlog enabled: scanning last {len(backlog)} lines for matches...")
            else:
                # seed dedupe only (no backlog spam)
                backlog = []
                for ln in lines[-2000:]:
                    parsed = extract_day_time_and_action(ln)
                    if not parsed:
                        continue
                    d, h, m, s, act = parsed
                    out = format_output(d, h, m, s, act)
                    dedupe.seen(out)
                print("First run: seeded dedupe from current GetGameLog output (no backlog spam).")

            if BACKLOG_ON_START and routes:
                async with aiohttp.ClientSession() as session:
                    for r in routes:
                        tribe = r["tribe"]
                        webhook = r["webhook"]
                        thread_id = r.get("thread_id", "")
                        sent = 0
                        # send most recent matching lines only
                        for ln in reversed(backlog):
                            if not matches_tribe(ln, tribe):
                                continue
                            parsed = extract_day_time_and_action(ln)
                            if not parsed:
                                continue
                            d, hh, mm, ss, act = parsed
                            out = format_output(d, hh, mm, ss, act)
                            if dedupe.seen(out):
                                continue
                            color = pick_color(act)
                            embed = {"description": out, "color": color}
                            try:
                                await post_to_webhook(session, webhook, thread_id, embed)
                                sent += 1
                                last_activity_ts = time.time()
                            except Exception as e:
                                print(f"Backlog send error ({tribe}): {e}")
                            if sent >= BACKLOG_LINES:
                                break
                        if sent:
                            print(f"Backlog sent for {tribe}: {sent} messages")

    except Exception as e:
        print(f"Seed/GetGameLog error: {e}")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                routes = load_routes()  # reload so adding tribes later works without restart

                log_text = await safe_rcon("GetGameLog", timeout=10.0)
                if log_text:
                    lines = [ln.strip() for ln in log_text.splitlines() if ln.strip()]
                else:
                    lines = []

                # Process from oldest -> newest so ordering stays correct
                new_sends = 0

                for ln in lines[-2500:]:
                    parsed = extract_day_time_and_action(ln)
                    if not parsed:
                        continue

                    day, hh, mm, ss, act = parsed
                    out = format_output(day, hh, mm, ss, act)

                    # dedupe globally (so the same line doesn't spam multiple polls)
                    if dedupe.seen(out):
                        continue

                    # route it to the correct tribe(s)
                    for r in routes:
                        tribe = r["tribe"]
                        if not matches_tribe(ln, tribe):
                            continue

                        webhook = r["webhook"]
                        thread_id = r.get("thread_id", "")

                        color = pick_color(act)
                        embed = {"description": out, "color": color}

                        await post_to_webhook(session, webhook, thread_id, embed)
                        new_sends += 1
                        last_activity_ts = time.time()

                # Heartbeat: only if NO activity for HEARTBEAT_MINUTES
                now = time.time()
                if (now - last_activity_ts) >= (HEARTBEAT_MINUTES * 60) and (now - last_heartbeat_ts) >= (HEARTBEAT_MINUTES * 60):
                    for r in routes:
                        tribe = r["tribe"]
                        webhook = r["webhook"]
                        thread_id = r.get("thread_id", "")
                        embed = {
                            "description": f"🫀 **Heartbeat** — No new logs since last check for **{tribe}**. Still polling.",
                            "color": 0x95A5A6,
                        }
                        try:
                            await post_to_webhook(session, webhook, thread_id, embed)
                            print(f"Heartbeatouting heartbeat for {tribe}")
                        except Exception as e:
                            print(f"Heartbeat error ({tribe}): {e}")
                    last_heartbeat_ts = now

                if new_sends:
                    print(f"Tribe logs: sent {new_sends} new messages")

            except Exception as e:
                print(f"GetGameLog error: {e}")

            await asyncio.sleep(GAMELOG_POLL_SECONDS)


# ============================================================
# OPTIONAL: /linktribelog COMMAND (Admin-only)
# ============================================================

def setup_tribelog_commands(tree, guild_obj):
    """
    Adds:
      /linktribelog tribe webhook thread_id

    Admin-only role id is enforced.
    Persists to tribe_routes.json so it survives restart.
    """
    import discord

    @tree.command(name="linktribelog", guild=guild_obj)
    async def linktribelog(i: discord.Interaction, tribe: str, webhook: str, thread_id: str):
        # role check
        if not any(getattr(r, "id", None) == ADMIN_ROLE_ID for r in getattr(i.user, "roles", [])):
            await i.response.send_message("❌ No permission", ephemeral=True)
            return

        tribe = tribe.strip()
        webhook = webhook.strip()
        thread_id = thread_id.strip()

        if not tribe or not webhook:
            await i.response.send_message("❌ Tribe and webhook are required.", ephemeral=True)
            return

        routes = _load_routes_from_file()

        # upsert by tribe name
        replaced = False
        for r in routes:
            if str(r.get("tribe", "")).strip().lower() == tribe.lower():
                r["webhook"] = webhook
                r["thread_id"] = thread_id
                replaced = True
                break
        if not replaced:
            routes.append({"tribe": tribe, "webhook": webhook, "thread_id": thread_id})

        _save_routes_to_file(routes)

        await i.response.send_message(
            f"✅ Linked tribe **{tribe}** to webhook (thread_id={thread_id or 'none'}).",
            ephemeral=True
        )