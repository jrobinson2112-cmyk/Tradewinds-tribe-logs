import os
import asyncio
import discord
from discord import app_commands
import aiohttp

import tribelogs_module
import time_module
import players_module
import vcstatus_module
import crosschat_module
import travelerlogs_module  # <-- ADD THIS

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Your Discord server + admin role
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

# Webhooks (time + players) used by your webhook-upsert system
WEBHOOK_URL = os.getenv("WEBHOOK_URL")                  # time webhook
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")  # players webhook

# ---- Discord client / intents ----
intents = discord.Intents.default()
# Needed for Discord -> in-game crosschat (reading message.content)
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Store message IDs so webhooks EDIT instead of posting new
_webhook_message_ids = {
    "time": None,
    "players": None,
}


async def _webhook_upsert_impl(session: aiohttp.ClientSession, url: str, key: str, embed: dict):
    """
    Create-or-edit a webhook message (edit if we have an ID; otherwise create once).
    """
    mid = _webhook_message_ids.get(key)

    # Edit existing message
    if mid:
        async with session.patch(f"{url}/messages/{mid}", json={"embeds": [embed]}) as r:
            # If message/webhook removed => recreate
            if r.status == 404:
                _webhook_message_ids[key] = None
                return await _webhook_upsert_impl(session, url, key, embed)
            return

    # Create new (store returned ID)
    async with session.post(url + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json()
        if isinstance(data, dict) and "id" in data:
            _webhook_message_ids[key] = data["id"]
        else:
            raise RuntimeError(f"Webhook post failed ({r.status}): {data}")


async def webhook_upsert(*args, **kwargs):
    """
    Flexible wrapper so modules can call this in different ways:
      - webhook_upsert(embed)
      - webhook_upsert(key, embed)
      - webhook_upsert(session, url, key, embed)
    """
    session = kwargs.get("session")
    url = kwargs.get("url")
    key = kwargs.get("key")
    embed = kwargs.get("embed")

    if len(args) == 1:
        embed = args[0]
        key = key or "time"
        url = url or (WEBHOOK_URL if key == "time" else PLAYERS_WEBHOOK_URL)

    elif len(args) == 2:
        key, embed = args
        url = url or (WEBHOOK_URL if key == "time" else PLAYERS_WEBHOOK_URL)

    elif len(args) == 4:
        session, url, key, embed = args

    else:
        raise TypeError(f"webhook_upsert() got unsupported args: {args} {kwargs}")

    if not url:
        raise RuntimeError("Missing webhook URL (WEBHOOK_URL / PLAYERS_WEBHOOK_URL env var)")

    if session is None:
        async with aiohttp.ClientSession() as s:
            return await _webhook_upsert_impl(s, url, key, embed)

    return await _webhook_upsert_impl(session, url, key, embed)


def _get_rcon_command():
    """
    Your project already exposes an RCON function somewhere.
    Historically you've had tribelogs_module.rcon_command.
    """
    return getattr(tribelogs_module, "rcon_command", None)


async def _start_task_maybe(func, *args):
    """
    Accepts:
      - async function returning coroutine
      - sync function returning coroutine/task/None
    Ensures it gets scheduled safely.
    """
    try:
        res = func(*args)
        if asyncio.iscoroutine(res):
            asyncio.create_task(res)
        elif isinstance(res, asyncio.Task):
            # already scheduled
            pass
        else:
            # sync loop starter or None
            pass
    except TypeError:
        # If signature mismatch, try calling with no args
        res = func()
        if asyncio.iscoroutine(res):
            asyncio.create_task(res)
        elif isinstance(res, asyncio.Task):
            pass


@client.event
async def on_ready():
    guild_obj = discord.Object(id=GUILD_ID)

    # ---- Register commands (MUST be before sync) ----

    # Tribe logs commands
    try:
        tribelogs_module.setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
    except TypeError:
        tribelogs_module.setup_tribelog_commands(tree, GUILD_ID)

    # Time commands (your time_module expects webhook_upsert)
    rcon_cmd = _get_rcon_command()
    if rcon_cmd is None:
        print("⚠️ WARNING: rcon_command not found. Time/Crosschat may not function correctly.")

    time_module.setup_time_commands(tree, GUILD_ID, ADMIN_ROLE_ID, rcon_cmd, webhook_upsert)

    # Traveler Logs (/writelog) - NOT admin locked
    # (travelerlogs_module should handle its own channel restriction internally)
    try:
        travelerlogs_module.setup_travelerlog_commands(tree, GUILD_ID)
    except AttributeError:
        # If you named it differently in the module, try common alternatives
        travelerlogs_module.setup_commands(tree, GUILD_ID)

    # ---- Sync to guild ----
    synced = await tree.sync(guild=guild_obj)

    # ---- Start loops ----

    # Tribe logs loop
    await _start_task_maybe(tribelogs_module.run_tribelogs_loop, client)

    # Time loop
    asyncio.create_task(time_module.run_time_loop(client, rcon_cmd, webhook_upsert))

    # Players loop
    await _start_task_maybe(players_module.run_players_loop, client)

    # VC status loop
    await _start_task_maybe(vcstatus_module.run_vcstatus_loop, client)

    # Crosschat loop
    if rcon_cmd is not None:
        # Some versions expect only (client); others (client, rcon_cmd)
        try:
            asyncio.create_task(crosschat_module.run_crosschat_loop(client, rcon_cmd))
        except TypeError:
            asyncio.create_task(crosschat_module.run_crosschat_loop(client))

    print(f"✅ Solunaris bot online | commands synced to guild {GUILD_ID} ({len(synced)} commands)")
    print("✅ Modules running: tribelogs, time, vcstatus, players, crosschat, travelerlogs")


@client.event
async def on_message(message: discord.Message):
    """
    Needed for Discord -> in-game chat relay.
    We only call the handler if it exists in the crosschat module.
    """
    if message.author.bot:
        return

    handler = getattr(crosschat_module, "on_discord_message", None)
    if handler is None:
        return

    rcon_cmd = _get_rcon_command()
    if rcon_cmd is None:
        return

    try:
        await handler(message, rcon_cmd)
    except Exception as e:
        print(f"[crosschat] on_message error: {e}")


client.run(DISCORD_TOKEN)