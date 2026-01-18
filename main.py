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
import rcon_gamelogs_module  # ✅ NEW

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Your Discord server + admin role
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

# Webhooks (time + players) used by webhook-upsert system
WEBHOOK_URL = os.getenv("WEBHOOK_URL")                 # time webhook
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL") # players webhook

# ---- Discord client / intents ----
intents = discord.Intents.default()
intents.message_content = True  # for crosschat

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Store message IDs so webhooks EDIT instead of posting new
_webhook_message_ids = {
    "time": None,
    "players": None,
}


async def _webhook_upsert_impl(session: aiohttp.ClientSession, url: str, key: str, embed: dict):
    mid = _webhook_message_ids.get(key)

    if mid:
        async with session.patch(f"{url}/messages/{mid}", json={"embeds": [embed]}) as r:
            if r.status == 404:
                _webhook_message_ids[key] = None
                return await _webhook_upsert_impl(session, url, key, embed)
            return

    async with session.post(url + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json()
        if isinstance(data, dict) and "id" in data:
            _webhook_message_ids[key] = data["id"]
        else:
            raise RuntimeError(f"Webhook post failed ({r.status}): {data}")


async def webhook_upsert(*args, **kwargs):
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
    return getattr(tribelogs_module, "rcon_command", None)


async def _start_task(func, *args):
    res = func(*args)
    if asyncio.iscoroutine(res):
        asyncio.create_task(res)
    elif isinstance(res, asyncio.Task):
        pass


@client.event
async def on_ready():
    guild_obj = discord.Object(id=GUILD_ID)

    # ---- Register commands ----
    tribelogs_module.setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)

    rcon_cmd = _get_rcon_command()
    if rcon_cmd is None:
        print("⚠️ WARNING: rcon_command not found. Some modules may not function correctly.")

    time_module.setup_time_commands(tree, GUILD_ID, ADMIN_ROLE_ID, rcon_cmd, webhook_upsert)

    # ✅ NEW: /gamelogs command
    rcon_gamelogs_module.setup_gamelogs_commands(tree, GUILD_ID)

    await tree.sync(guild=guild_obj)

    # ---- Start loops ----
    await _start_task(tribelogs_module.run_tribelogs_loop)

    await _start_task(time_module.run_time_loop, client, rcon_cmd, webhook_upsert)

    await _start_task(players_module.run_players_loop)

    await _start_task(vcstatus_module.run_vcstatus_loop, client)

    if rcon_cmd is not None:
        await _start_task(crosschat_module.run_crosschat_loop, client, rcon_cmd)

        # ✅ NEW: start gamelog poller
        await _start_task(rcon_gamelogs_module.run_gamelogs_loop, rcon_cmd)

    print(f"✅ Solunaris bot online | commands synced to guild {GUILD_ID}")
    print("✅ Modules running: tribelogs, time, vcstatus, players, crosschat, gamelogs")


@client.event
async def on_message(message: discord.Message):
    if message.author and getattr(message.author, "bot", False):
        return

    rcon_cmd = _get_rcon_command()
    if rcon_cmd is None:
        return

    try:
        try:
            await crosschat_module.on_discord_message(message, rcon_cmd)
        except TypeError:
            await crosschat_module.on_discord_message(message)
    except Exception as e:
        print(f"[crosschat] on_message error: {e}")


client.run(DISCORD_TOKEN)