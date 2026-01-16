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

WEBHOOK_URL = os.getenv("WEBHOOK_URL")                 # time webhook
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL") # players webhook

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Store message IDs so webhooks EDIT instead of posting new each time
_webhook_message_ids = {"time": None, "players": None}


async def _webhook_upsert_impl(session: aiohttp.ClientSession, url: str, key: str, embed: dict):
    mid = _webhook_message_ids.get(key)

    # Edit existing message if we have it
    if mid:
        async with session.patch(f"{url}/messages/{mid}", json={"embeds": [embed]}) as r:
            if r.status == 404:
                _webhook_message_ids[key] = None
                return await _webhook_upsert_impl(session, url, key, embed)
            return

    # Otherwise create it once and store the message ID
    async with session.post(url + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json()
        if "id" in data:
            _webhook_message_ids[key] = data["id"]
        else:
            raise RuntimeError(f"Webhook post failed ({r.status}): {data}")


async def webhook_upsert(*args, **kwargs):
    """
    Supports the module calling style:
      webhook_upsert(session, url, key, embed)
    """
    if len(args) != 4:
        raise TypeError("webhook_upsert must be called as (session, url, key, embed)")
    session, url, key, embed = args
    return await _webhook_upsert_impl(session, url, key, embed)


@client.event
async def on_ready():
    guild_obj = discord.Object(id=GUILD_ID)

    # ---- RCON function (comes from tribelogs module) ----
    rcon_cmd = getattr(tribelogs_module, "rcon_command", None)
    if rcon_cmd is None:
        raise RuntimeError("tribelogs_module.rcon_command not found (RCON not available)")

    # ---- Register commands (NO FALLBACKS, NO GUESSING) ----
    tribelogs_module.setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
    time_module.setup_time_commands(tree, GUILD_ID, ADMIN_ROLE_ID, rcon_cmd, webhook_upsert)

    await tree.sync(guild=guild_obj)

    # ---- Start loops ----
    asyncio.create_task(tribelogs_module.run_tribelogs_loop())
    asyncio.create_task(time_module.run_time_loop(client, rcon_cmd, webhook_upsert))

    asyncio.create_task(players_module.run_players_loop())
    asyncio.create_task(vcstatus_module.run_vcstatus_loop(client))

    print(f"✅ Solunaris bot online | commands synced to guild {GUILD_ID}")
    print("✅ Modules running: tribelogs, time, vcstatus, players")


client.run(DISCORD_TOKEN)