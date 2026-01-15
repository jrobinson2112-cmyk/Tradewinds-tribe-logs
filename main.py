import os
import asyncio
import discord
from discord import app_commands
import aiohttp

import tribelogs_module
import time_module
import players_module
import vcstatus_module

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

# Webhooks (time + players)
WEBHOOK_URL = os.getenv("WEBHOOK_URL")                # time webhook
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")  # players webhook

intents = discord.Intents.default()
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

    # Edit existing
    if mid:
        async with session.patch(f"{url}/messages/{mid}", json={"embeds": [embed]}) as r:
            # If it was deleted, recreate
            if r.status == 404:
                _webhook_message_ids[key] = None
                return await _webhook_upsert_impl(session, url, key, embed)
            return

    # Create new (store ID)
    async with session.post(url + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json()
        # Some webhook endpoints can return non-standard payloads if wrong URL
        if "id" in data:
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
    # Pull possible args
    session = None
    url = None
    key = None
    embed = None

    # kwargs support
    if "session" in kwargs:
        session = kwargs["session"]
    if "url" in kwargs:
        url = kwargs["url"]
    if "key" in kwargs:
        key = kwargs["key"]
    if "embed" in kwargs:
        embed = kwargs["embed"]

    # positional patterns
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

    # If module didn't pass a session, make a short-lived one
    if session is None:
        async with aiohttp.ClientSession() as s:
            return await _webhook_upsert_impl(s, url, key, embed)

    return await _webhook_upsert_impl(session, url, key, embed)


@client.event
async def on_ready():
    guild_obj = discord.Object(id=GUILD_ID)

    # ---- Register commands ----
    # Tribe logs commands
    try:
        tribelogs_module.setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
    except TypeError:
        tribelogs_module.setup_tribelog_commands(tree, GUILD_ID)

    # Time commands
    rcon_cmd = getattr(tribelogs_module, "rcon_command", None)
    try:
        # Preferred: (tree, guild_id, admin_role_id, rcon_command)
        time_module.setup_time_commands(tree, GUILD_ID, ADMIN_ROLE_ID, rcon_cmd)
    except TypeError:
        try:
            # (tree, guild_id, rcon_command)
            time_module.setup_time_commands(tree, GUILD_ID, rcon_cmd)
        except TypeError:
            # (tree, guild_id)
            time_module.setup_time_commands(tree, GUILD_ID)

    await tree.sync(guild=guild_obj)

    # ---- Start loops ----
    # Tribe logs
    try:
        asyncio.create_task(tribelogs_module.run_tribelogs_loop())
    except TypeError:
        asyncio.create_task(tribelogs_module.run_tribelogs_loop(client))

    # Time (FIXED: pass webhook_upsert)
    asyncio.create_task(time_module.run_time_loop(client, rcon_cmd, webhook_upsert))

    # Players
    try:
        asyncio.create_task(players_module.run_players_loop(client))
    except TypeError:
        asyncio.create_task(players_module.run_players_loop())

    # VC status
    try:
        asyncio.create_task(vcstatus_module.run_vcstatus_loop(client))
    except TypeError:
        asyncio.create_task(vcstatus_module.run_vcstatus_loop())

    print(f"✅ Solunaris bot online | commands synced to guild {GUILD_ID}")
    print("✅ Modules running: tribelogs, time, vcstatus, players")


client.run(DISCORD_TOKEN)