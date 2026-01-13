import os
import time
import asyncio
import discord

from players_module import rcon_command, parse_listplayers  # reuse your working RCON code

# =========================
# ENV / CONFIG
# =========================
STATUS_VC_ID = int(os.getenv("STATUS_VC_ID", "0") or "0")   # REQUIRED
PLAYER_CAP = int(os.getenv("PLAYER_CAP", "42") or "42")

# How often we poll RCON for the VC name
VC_STATUS_POLL_SECONDS = float(os.getenv("VC_STATUS_POLL_SECONDS", "60") or "60")

# Discord rate limits: renaming too often can 429. This is your safety throttle.
VC_EDIT_MIN_SECONDS = float(os.getenv("VC_EDIT_MIN_SECONDS", "300") or "300")  # default 5 min

# If you want emoji in the name:
VC_ONLINE_EMOJI = os.getenv("VC_ONLINE_EMOJI", "🟢")
VC_OFFLINE_EMOJI = os.getenv("VC_OFFLINE_EMOJI", "🔴")

# Prefix text
VC_PREFIX = os.getenv("VC_PREFIX", "Solunaris")

def _ensure_env():
    if not STATUS_VC_ID:
        raise RuntimeError("Missing required environment variable: STATUS_VC_ID")

_last_edit_ts = 0.0
_last_name = None

async def run_vcstatus_loop(client: discord.Client):
    """
    Updates a voice channel name to: "🟢 Solunaris | x/42"
    Uses RCON ListPlayers count. If RCON fails => offline emoji and keep last known count (or 0).
    """
    _ensure_env()
    await client.wait_until_ready()

    global _last_edit_ts, _last_name
    last_known_count = 0

    while True:
        online = False
        count = last_known_count

        try:
            out = await rcon_command("ListPlayers", timeout=10.0)
            names = parse_listplayers(out)
            count = len(names)
            last_known_count = count
            online = True
        except Exception as e:
            # RCON failed; treat as offline but don't crash loop
            print(f"VC status loop RCON error: {e}")

        emoji = VC_ONLINE_EMOJI if online else VC_OFFLINE_EMOJI
        new_name = f"{emoji} {VC_PREFIX} | {count}/{PLAYER_CAP}"

        now = time.time()
        vc = client.get_channel(STATUS_VC_ID)

        if vc is None:
            print(f"VC status loop: could not find channel id {STATUS_VC_ID}")
        else:
            # Only edit if name changed AND we respect the min edit interval
            if new_name != _last_name and (now - _last_edit_ts) >= VC_EDIT_MIN_SECONDS:
                try:
                    await vc.edit(name=new_name)
                    _last_name = new_name
                    _last_edit_ts = now
                except discord.Forbidden:
                    print("VC status loop: Forbidden (missing Manage Channels permission).")
                except discord.HTTPException as e:
                    print(f"VC status loop: HTTPException {e}")

        await asyncio.sleep(VC_STATUS_POLL_SECONDS)