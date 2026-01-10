import os
import re
import io
import time
import json
import ftplib
import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict

import discord
from discord import app_commands

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("tribe-forwarder")

# ============================================================
# ENV / CONFIG
# ============================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

FTP_LOGS_DIR = os.getenv("FTP_LOGS_DIR", "arksa/ShooterGame/Saved/Logs").strip().rstrip("/")
TARGET_TRIBE = os.getenv("TARGET_TRIBE", "Tribe Valkyrie").strip()

POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "10"))
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "10"))

DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
DISCORD_CHANNEL_ID_INT = int(DISCORD_CHANNEL_ID) if DISCORD_CHANNEL_ID and DISCORD_CHANNEL_ID.isdigit() else None

STATE_PATH = "/tmp/tribe_forwarder_state.json"

ALLOWED_RE = re.compile(r"^(ShooterGame\.log|ServerGame\..*\.log)$", re.IGNORECASE)
SKIP_SUBSTRINGS = ("backup", "failedwater", "crash", "crashstack")

# ============================================================
# VALIDATION
# ============================================================
missing = []
for name, val in [
    ("DISCORD_TOKEN", DISCORD_TOKEN),
    ("FTP_HOST", FTP_HOST),
    ("FTP_USER", FTP_USER),
    ("FTP_PASS", FTP_PASS),
]:
    if not val:
        missing.append(name)

if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

# ============================================================
# DISCORD FORMAT HELPERS
# ============================================================
def color_for_line(text: str) -> int:
    lower = text.lower()
    if "claimed" in lower or "claiming" in lower:
        return 0x9B59B6  # purple
    if "tamed" in lower or "taming" in lower:
        return 0x2ECC71  # green
    if "killed" in lower or "died" in lower or "death" in lower:
        return 0xE74C3C  # red
    if "demolished" in lower or "destroyed" in lower:
        return 0xF1C40F  # yellow
    return 0x95A5A6  # default grey

def strip_richcolor(text: str) -> str:
    return re.sub(r"<\/?RichColor[^>]*>", "", text, flags=re.IGNORECASE)

def make_embed(line: str) -> discord.Embed:
    clean = strip_richcolor(line.strip())
    return discord.Embed(description=clean, color=color_for_line(clean))

# ============================================================
# FTP HELPERS
# ============================================================
def ftp_connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=15)
    ftp.login(FTP_USER, FTP_PASS)
    return ftp

def ftp_list_logs(ftp: ftplib.FTP, logs_dir: str) -> List[str]:
    try:
        entries = ftp.nlst(logs_dir)
    except ftplib.error_perm as e:
        log.warning(f"NLST failed on {logs_dir}: {e}. Trying CWD + NLST.")
        ftp.cwd("/")
        ftp.cwd(logs_dir)
        entries = ftp.nlst()

    names = []
    for e in entries:
        names.append(e.split("/")[-1])
    return sorted(set(names))

def ftp_size_binary(ftp: ftplib.FTP, remote_path: str) -> int:
    ftp.voidcmd("TYPE I")
    return ftp.size(remote_path)  # type: ignore

def ftp_read_from_offset(ftp: ftplib.FTP, remote_path: str, offset: int) -> bytes:
    ftp.voidcmd("TYPE I")
    buf = io.BytesIO()
    def _cb(chunk: bytes):
        buf.write(chunk)
    ftp.retrbinary(f"RETR {remote_path}", _cb, rest=offset)
    return buf.getvalue()

def list_allowed(names: List[str]) -> List[str]:
    out = []
    for n in names:
        lower = n.lower()
        if any(s in lower for s in SKIP_SUBSTRINGS):
            continue
        if not ALLOWED_RE.match(n):
            continue
        out.append(n)
    return out

def pick_growing_log(ftp: ftplib.FTP, names: List[str], logs_dir: str, last_sizes: Dict[str, int]) -> Optional[str]:
    """
    Choose the log that is currently growing (or largest if all equal),
    among ShooterGame.log and ServerGame*.log.
    """
    allowed = list_allowed(names)
    if not allowed:
        return None

    sizes = {}
    for n in allowed:
        rp = f"{logs_dir}/{n}"
        try:
            sizes[n] = ftp_size_binary(ftp, rp)
        except Exception:
            # ignore files we can't size
            continue

    if not sizes:
        return None

    # Prefer whichever increased since last time
    best = None
    best_delta = -1
    best_size = -1

    for n, sz in sizes.items():
        prev = last_sizes.get(n, 0)
        delta = sz - prev
        if delta > best_delta or (delta == best_delta and sz > best_size):
            best = n
            best_delta = delta
            best_size = sz

    # Update last_sizes snapshot
    last_sizes.clear()
    last_sizes.update(sizes)

    return best

# ============================================================
# STATE
# ============================================================
@dataclass
class ForwarderState:
    active_file: Optional[str] = None
    offset: int = 0
    carry: str = ""
    last_sent_line_hash: Optional[str] = None
    last_sent_ts: float = 0.0
    last_sizes: Dict[str, int] = None  # log_name -> last known size

def load_state() -> ForwarderState:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return ForwarderState(
            active_file=d.get("active_file"),
            offset=int(d.get("offset", 0)),
            carry=d.get("carry", "") or "",
            last_sent_line_hash=d.get("last_sent_line_hash"),
            last_sent_ts=float(d.get("last_sent_ts", 0.0)),
            last_sizes=d.get("last_sizes", {}) or {},
        )
    except Exception:
        return ForwarderState(last_sizes={})

def save_state(st: ForwarderState) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "active_file": st.active_file,
                "offset": st.offset,
                "carry": st.carry,
                "last_sent_line_hash": st.last_sent_line_hash,
                "last_sent_ts": st.last_sent_ts,
                "last_sizes": st.last_sizes or {},
            }, f)
    except Exception as e:
        log.warning(f"Could not save state: {e}")

def stable_hash(s: str) -> str:
    return str(abs(hash(s)))

# ============================================================
# CORE LOGIC
# ============================================================
def filter_matching_lines(lines: List[str]) -> List[str]:
    t = TARGET_TRIBE.lower()
    return [ln for ln in lines if t in ln.lower()]

async def send_embed_safely(channel: discord.abc.Messageable, embed: discord.Embed) -> bool:
    try:
        await channel.send(embed=embed)
        return True
    except discord.HTTPException as e:
        log.error(f"Discord send failed: {e}")
        return False

async def check_for_new_lines_and_collect(st: ForwarderState) -> Tuple[Optional[str], List[str], str]:
    ftp = None
    try:
        ftp = ftp_connect()
        names = ftp_list_logs(ftp, FTP_LOGS_DIR)

        if st.last_sizes is None:
            st.last_sizes = {}

        # Choose the log that's actually growing right now
        active = pick_growing_log(ftp, names, FTP_LOGS_DIR, st.last_sizes)
        if not active:
            return None, [], f"No allowed logs found in {FTP_LOGS_DIR}"

        remote_path = f"{FTP_LOGS_DIR}/{active}"

        try:
            remote_size = ftp_size_binary(ftp, remote_path)
        except Exception as e:
            return active, [], f"Could not get remote file size: {e}"

        # If active file changed, reset cursor
        if st.active_file != active:
            log.info(f"Active log changed: {st.active_file} -> {active} (resetting cursor)")
            st.active_file = active
            st.offset = 0
            st.carry = ""

        # Handle rotation/truncation
        if st.offset > remote_size:
            log.info(f"Detected truncation/rotation (offset {st.offset} > size {remote_size}); resetting offset")
            st.offset = 0
            st.carry = ""

        if remote_size == st.offset:
            return active, [], f"Heartbeat: file={active} size={remote_size} offset={st.offset}->{st.offset} new_lines=0"

        chunk = ftp_read_from_offset(ftp, remote_path, st.offset)
        old_offset = st.offset
        st.offset = remote_size

        text = chunk.decode("utf-8", errors="replace")
        if st.carry:
            text = st.carry + text

        # carry partial line
        if text and not text.endswith("\n") and not text.endswith("\r"):
            parts = text.splitlines(keepends=False)
            if parts:
                st.carry = parts[-1]
                lines = parts[:-1]
            else:
                st.carry = text
                lines = []
        else:
            st.carry = ""
            lines = text.splitlines()

        return active, lines, f"Heartbeat: file={active} size={remote_size} offset={old_offset}->{st.offset} new_lines={len(lines)}"

    finally:
        try:
            if ftp:
                ftp.quit()
        except Exception:
            pass

# ============================================================
# DISCORD BOT
# ============================================================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

state = load_state()
last_known_channel_id: Optional[int] = None

async def get_output_channel(interaction: Optional[discord.Interaction] = None) -> Optional[discord.abc.Messageable]:
    global last_known_channel_id

    if DISCORD_CHANNEL_ID_INT:
        ch = client.get_channel(DISCORD_CHANNEL_ID_INT)
        if ch:
            return ch

    if interaction and interaction.channel:
        last_known_channel_id = interaction.channel.id
        return interaction.channel

    if last_known_channel_id:
        ch = client.get_channel(last_known_channel_id)
        if ch:
            return ch

    return None

async def send_most_recent_matching(channel: discord.abc.Messageable, matching: List[str]) -> int:
    if not matching:
        return 0

    most_recent = matching[-1].strip()
    h = stable_hash(most_recent)

    if state.last_sent_line_hash == h:
        return 0

    ok = await send_embed_safely(channel, make_embed(most_recent))
    if ok:
        state.last_sent_line_hash = h
        state.last_sent_ts = time.time()
        save_state(state)
        return 1
    return 0

async def send_up_to_n_matching(channel: discord.abc.Messageable, lines: List[str], limit: int = 8) -> int:
    match = filter_matching_lines(lines)
    if not match:
        return 0

    to_send = match[-limit:]
    sent = 0
    for ln in to_send:
        ln = ln.strip()
        h = stable_hash(ln)
        if state.last_sent_line_hash == h:
            continue
        ok = await send_embed_safely(channel, make_embed(ln))
        if ok:
            sent += 1
            state.last_sent_line_hash = h
            state.last_sent_ts = time.time()
            save_state(state)
            await asyncio.sleep(0.35)
    return sent

@tree.command(name="gettribelogs", description="Check FTP for new Tribe Valkyrie logs and post the latest.")
async def gettribelogs(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    channel = await get_output_channel(interaction)
    if channel is None:
        await interaction.followup.send("I can't find a channel to post into. Set DISCORD_CHANNEL_ID or run the command in a text channel.", ephemeral=True)
        return

    active, new_lines, status = await check_for_new_lines_and_collect(state)
    save_state(state)

    sent = 0
    if new_lines:
        sent = await send_up_to_n_matching(channel, new_lines, limit=8)

    await interaction.followup.send(
        f"Checked `{active or 'none'}`. {status}\nSent `{sent}` message(s).",
        ephemeral=True
    )

async def poll_loop():
    await client.wait_until_ready()
    log.info("Starting Container")
    log.info(f"Polling every {POLL_INTERVAL:.1f} seconds")
    log.info(f"Filtering: {TARGET_TRIBE} (sending ONLY the most recent matching log)")
    log.info(f"Logs dir: {FTP_LOGS_DIR}")
    log.info("Allowed logs: ShooterGame.log and ServerGame*.log (excluding backups/FailedWater/etc)")

    first_run = True

    while not client.is_closed():
        try:
            channel = await get_output_channel(None)
            active, new_lines, status = await check_for_new_lines_and_collect(state)
            save_state(state)

            if active is None:
                log.warning(status)
                await asyncio.sleep(POLL_INTERVAL)
                continue

            log.info(status)

            if first_run:
                log.info("First run: skipped backlog and started live from the end.")
                first_run = False
                await asyncio.sleep(POLL_INTERVAL)
                continue

            if channel and new_lines:
                matching = filter_matching_lines(new_lines)
                sent = await send_most_recent_matching(channel, matching)
                if sent:
                    log.info("Sent 1 message to Discord")

        except Exception as e:
            log.error(f"Poll error: {e}")

        await asyncio.sleep(POLL_INTERVAL)

async def heartbeat_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            await asyncio.sleep(HEARTBEAT_MINUTES * 60)
            channel = await get_output_channel(None)
            if not channel:
                continue

            now = time.time()
            if state.last_sent_ts == 0 or (now - state.last_sent_ts) >= (HEARTBEAT_MINUTES * 60):
                await channel.send(f"🫀 Heartbeat: No new logs since last check. (Filtering: {TARGET_TRIBE})")

        except Exception as e:
            log.error(f"Heartbeat error: {e}")

@client.event
async def on_ready():
    try:
        synced = await tree.sync()
        log.info(f"Slash commands synced: {len(synced)}")
    except Exception as e:
        log.error(f"Failed to sync commands: {e}")

    if not getattr(client, "_poll_task_started", False):
        client._poll_task_started = True  # type: ignore
        asyncio.create_task(poll_loop())
        asyncio.create_task(heartbeat_loop())

def main():
    client.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()