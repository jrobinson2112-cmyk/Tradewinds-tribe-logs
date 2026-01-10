import os
import time
import ftplib
import re
import hashlib
import asyncio
from typing import Optional, List, Tuple

import aiohttp
import discord
from discord import app_commands

# =========================
# ENV CONFIG
# =========================
FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Discord bot token for slash command support
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Optional (recommended): set for faster slash command sync during development
# If unset, commands sync globally (can take a while to appear).
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # e.g. "123456789012345678"

# Optional: known logs dir (recommended). Example:
# FTP_LOGS_DIR=arksa/ShooterGame/Saved/Logs
FTP_LOGS_DIR_ENV = os.getenv("FTP_LOGS_DIR")

TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie")

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "10"))          # seconds
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "600")) # 10 min default

# =========================
# VALIDATION
# =========================
missing = []
for k, v in [
    ("FTP_HOST", FTP_HOST),
    ("FTP_USER", FTP_USER),
    ("FTP_PASS", FTP_PASS),
    ("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL),
    ("DISCORD_BOT_TOKEN", DISCORD_BOT_TOKEN),
]:
    if not v:
        missing.append(k)
if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

# =========================
# LOG PARSING / FORMATTING
# =========================
RICH_COLOR_RE = re.compile(r"<\/?RichColor[^>]*>", re.IGNORECASE)

def clean_line(line: str) -> str:
    line = RICH_COLOR_RE.sub("", line)
    return line.strip()

def discord_color_for_line(text: str) -> int:
    lower = text.lower()
    if "claimed" in lower or "claiming" in lower:
        return 0x9B59B6  # purple
    if "tamed" in lower or "taming" in lower:
        return 0x2ECC71  # green
    if "killed" in lower or "died" in lower:
        return 0xE74C3C  # red
    if "demolished" in lower or "destroyed" in lower:
        return 0xF1C40F  # yellow
    return 0x95A5A6  # grey

def fingerprint(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8", errors="ignore")).hexdigest()

# =========================
# WEBHOOK SENDER (aiohttp)
# =========================
async def send_webhook(session: aiohttp.ClientSession, text: str, title: Optional[str] = None) -> bool:
    """
    Sends a single embed to Discord webhook.
    Returns True on success.
    Retries on 429 using retry_after.
    """
    text = clean_line(text)
    if not text:
        return False

    embed = {
        "description": text,
        "color": discord_color_for_line(text),
    }
    if title:
        embed["title"] = title

    payload = {"embeds": [embed]}

    while True:
        try:
            async with session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 204:
                    return True

                if resp.status == 429:
                    data = await resp.json(content_type=None)
                    retry_after = float(data.get("retry_after", 1.0))
                    await asyncio.sleep(max(0.25, retry_after))
                    continue

                body = await resp.text()
                print(f"Error: Discord webhook error {resp.status}: {body}")
                return False
        except asyncio.TimeoutError:
            print("Error: Discord webhook timeout")
            return False
        except Exception as e:
            print(f"Error: Discord webhook exception: {e}")
            return False

# =========================
# FTP HELPERS (sync, run in thread)
# =========================
def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=20)
    ftp.login(FTP_USER, FTP_PASS)
    # Prefer binary
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass
    return ftp

def safe_pwd(ftp: ftplib.FTP) -> str:
    try:
        return ftp.pwd()
    except Exception:
        return "(unknown)"

def dir_exists(ftp: ftplib.FTP, path: str) -> bool:
    cur = safe_pwd(ftp)
    try:
        ftp.cwd(path)
        return True
    except Exception:
        return False
    finally:
        try:
            ftp.cwd(cur)
        except Exception:
            pass

def discover_logs_dir(ftp: ftplib.FTP) -> str:
    candidates = []
    if FTP_LOGS_DIR_ENV:
        candidates.append(FTP_LOGS_DIR_ENV.strip("/"))

    candidates += [
        "arksa/ShooterGame/Saved/Logs",
        "ShooterGame/Saved/Logs",
        "Saved/Logs",
    ]

    seen = set()
    ordered = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)

    print(f"FTP PWD: {safe_pwd(ftp)}")
    for c in ordered:
        if dir_exists(ftp, c):
            print(f"Using logs dir: {c}")
            return c

    raise RuntimeError("No valid logs directory found. Set FTP_LOGS_DIR to the correct path.")

def is_allowed_log_name(name: str) -> bool:
    lower = name.lower()
    if "backup" in lower:
        return False
    if "failedwater" in lower:
        return False
    if lower.endswith(".crashstack"):
        return False

    if name == "ShooterGame.log":
        return True
    if name.startswith("ServerGame.") and name.endswith(".log"):
        return True
    return False

def list_filenames_in_cwd(ftp: ftplib.FTP) -> List[str]:
    try:
        names = ftp.nlst()
    except Exception:
        names = []
    # Ensure filenames only (some servers return paths; we only want leaf names)
    cleaned = []
    for n in names:
        if not n:
            continue
        n2 = n.split("/")[-1]
        cleaned.append(n2)
    return list(dict.fromkeys(cleaned))

def pick_active_log_filename(ftp: ftplib.FTP) -> Optional[str]:
    names = list_filenames_in_cwd(ftp)
    allowed = [n for n in names if is_allowed_log_name(n)]
    if not allowed:
        return None

    if "ShooterGame.log" in allowed:
        return "ShooterGame.log"

    # Try MLSD for newest file
    try:
        candidates: List[Tuple[str, str]] = []
        for name, facts in ftp.mlsd():
            name_leaf = name.split("/")[-1]
            if is_allowed_log_name(name_leaf):
                candidates.append((name_leaf, facts.get("modify", "")))
        if candidates:
            candidates.sort(key=lambda x: x[1], reverse=True)
            return candidates[0][0]
    except Exception:
        pass

    allowed.sort()
    return allowed[-1]

def get_remote_size(ftp: ftplib.FTP, filename: str) -> Optional[int]:
    try:
        ftp.voidcmd("TYPE I")
    except Exception:
        pass
    try:
        return ftp.size(filename)
    except Exception as e:
        print(f"Could not get remote file size: {e}")
        return None

def read_from_offset(ftp: ftplib.FTP, filename: str, offset: int) -> bytes:
    buf = bytearray()

    def _cb(data: bytes):
        buf.extend(data)

    ftp.voidcmd("TYPE I")
    ftp.retrbinary(f"RETR {filename}", _cb, rest=offset)
    return bytes(buf)

# =========================
# STATE + CHECKER
# =========================
class LogForwarderState:
    def __init__(self):
        self.logs_dir: Optional[str] = None
        self.active_filename: Optional[str] = None
        self.offset: int = 0
        self.first_run: bool = True

        self.last_sent_fp: Optional[str] = None
        self.last_sent_at: float = 0.0

        self._lock = asyncio.Lock()

STATE = LogForwarderState()

async def check_for_new_logs(session: aiohttp.ClientSession, *, force: bool = False) -> int:
    """
    Checks FTP for new log content and sends ONLY the most recent matching Valkyrie line.
    Returns number of messages sent (0 or 1).
    If force=True, it still won't resend duplicates; it just runs immediately.
    """
    async with STATE._lock:
        sent = 0

        def _do_ftp_work():
            ftp = ftp_connect()
            try:
                if STATE.logs_dir is None:
                    STATE.logs_dir = discover_logs_dir(ftp)

                ftp.cwd(STATE.logs_dir)

                chosen = pick_active_log_filename(ftp)
                if not chosen:
                    return ("no_logs", None, None)

                if chosen != STATE.active_filename:
                    print(f"Active log selected: {STATE.logs_dir}/{chosen}")
                    STATE.active_filename = chosen
                    STATE.offset = 0
                    STATE.first_run = True

                size = get_remote_size(ftp, STATE.active_filename)
                if size is None:
                    return ("no_size", None, None)

                # First run: jump to end (skip backlog)
                if STATE.first_run:
                    STATE.offset = size
                    STATE.first_run = False
                    return ("first_run", None, None)

                if size < STATE.offset:
                    print(f"Log rotated (size {size} < offset {STATE.offset}) -> resetting offset")
                    STATE.offset = 0

                if size == STATE.offset:
                    return ("no_change", None, None)

                data = read_from_offset(ftp, STATE.active_filename, STATE.offset)
                STATE.offset = size

                text = data.decode("utf-8", errors="ignore")
                lines = [ln for ln in text.splitlines() if ln.strip()]
                newest_match = None
                for ln in reversed(lines):
                    if TARGET_TRIBE.lower() in ln.lower():
                        newest_match = ln
                        break

                return ("new_data", newest_match, size)
            finally:
                try:
                    ftp.quit()
                except Exception:
                    pass

        status, newest_match, _size = await asyncio.to_thread(_do_ftp_work)

        # Optional console heartbeat for debug
        if status == "first_run":
            print("First run: skipped backlog and started live from the end.")
            return 0
        if status in ("no_change", "no_logs", "no_size"):
            return 0

        if newest_match:
            fp = fingerprint(newest_match)
            if fp != STATE.last_sent_fp:
                ok = await send_webhook(session, newest_match)
                if ok:
                    STATE.last_sent_fp = fp
                    STATE.last_sent_at = time.time()
                    sent = 1
                    print("Sent 1 message to Discord (most recent matching log)")

        return sent

# =========================
# DISCORD BOT + TASKS
# =========================
intents = discord.Intents.none()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@tree.command(name="gettribelogs", description="Check FTP now and post the latest new Valkyrie tribe log (if any).")
async def gettribelogs(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)

    async with aiohttp.ClientSession() as session:
        sent = await check_for_new_logs(session, force=True)

    if sent:
        await interaction.followup.send("✅ Checked. Sent the newest new Valkyrie log.", ephemeral=True)
    else:
        await interaction.followup.send("ℹ️ Checked. No new Valkyrie logs since last send.", ephemeral=True)

async def poll_loop():
    print(f"Polling every {POLL_INTERVAL:.1f} seconds")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await check_for_new_logs(session)
            except Exception as e:
                print(f"Error in poll_loop: {e}")
            await asyncio.sleep(POLL_INTERVAL)

async def heartbeat_loop():
    print(f"Heartbeat every {HEARTBEAT_INTERVAL} seconds")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # If nothing new has been sent since the last heartbeat window, post heartbeat
                now = time.time()
                if STATE.last_sent_at == 0:
                    # If we haven't ever sent a log yet, still do a heartbeat
                    await send_webhook(session, "No new logs since last.", title="Heartbeat")
                else:
                    # If last send was more than heartbeat interval ago, post heartbeat
                    if (now - STATE.last_sent_at) >= HEARTBEAT_INTERVAL:
                        await send_webhook(session, "No new logs since last.", title="Heartbeat")
            except Exception as e:
                print(f"Error in heartbeat_loop: {e}")

            await asyncio.sleep(HEARTBEAT_INTERVAL)

@client.event
async def on_ready():
    print(f"Logged in as {client.user} (bot)")
    print(f"Filtering: {TARGET_TRIBE}")

    # Sync commands
    try:
        if DISCORD_GUILD_ID:
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            await tree.sync(guild=guild)
            print(f"Slash commands synced to guild {DISCORD_GUILD_ID}")
        else:
            await tree.sync()
            print("Slash commands synced globally (may take time to appear)")
    except Exception as e:
        print(f"Command sync error: {e}")

    # Start background tasks
    asyncio.create_task(poll_loop())
    asyncio.create_task(heartbeat_loop())

def main():
    print("Starting Container")
    client.run(DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    main()