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
import gamelogs_autopost_module
import travelerlogs_module  # ✅ button-only traveler logs module

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

_webhook_message_ids = {"time": None, "players": None}


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


async def _start_task_maybe(func, *args):
    try:
        res = func(*args)
        if asyncio.iscoroutine(res):
            asyncio.create_task(res)
    except TypeError:
        res = func()
        if asyncio.iscoroutine(res):
            asyncio.create_task(res)


@client.event
async def on_ready():
    guild_obj = discord.Object(id=GUILD_ID)

    # ✅ Traveler Logs persistent buttons + interaction routing
    travelerlogs_module.register_views(client)
    travelerlogs_module.setup_interaction_router(client)
    asyncio.create_task(travelerlogs_module.ensure_write_panels(client, guild_id=GUILD_ID))
    print("[travelerlogs] ✅ persistent views + panel ensure scheduled")

    # Commands
    try:
        tribelogs_module.setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
    except TypeError:
        tribelogs_module.setup_tribelog_commands(tree, GUILD_ID)

    rcon_cmd = _get_rcon_command()
    if rcon_cmd is None:
        print("⚠️ WARNING: rcon_command not found. Time/Crosschat/GameLogs may not function correctly.")

    time_module.setup_time_commands(tree, GUILD_ID, ADMIN_ROLE_ID, rcon_cmd, webhook_upsert)

    # Optional /writelog fallback (button is primary)
    try:
        travelerlogs_module.setup_travelerlog_commands(tree, GUILD_ID)
    except Exception as e:
        print(f"[travelerlogs] command setup error: {e}")

    await tree.sync(guild=guild_obj)

    # Loops
    await _start_task_maybe(tribelogs_module.run_tribelogs_loop)
    await _start_task_maybe(time_module.run_time_loop, client, rcon_cmd, webhook_upsert)
    await _start_task_maybe(players_module.run_players_loop)
    await _start_task_maybe(vcstatus_module.run_vcstatus_loop, client)

    if rcon_cmd is not None:
        await _start_task_maybe(crosschat_module.run_crosschat_loop, client, rcon_cmd)
        asyncio.create_task(gamelogs_autopost_module.run_gamelogs_autopost_loop(client, rcon_cmd))

    print(f"✅ Bot online | commands synced to guild {GUILD_ID}")


@client.event
async def on_message(message: discord.Message):
    # ✅ Traveler logs: image upload collector (Add Images button)
    try:
        await travelerlogs_module.handle_possible_image_upload(message)
    except Exception as e:
        print(f"[travelerlogs] image handler error: {e}")

    # Crosschat relay
    rcon_cmd = _get_rcon_command()
    if rcon_cmd is not None:
        try:
            await crosschat_module.on_discord_message(message, rcon_cmd)
        except TypeError:
            await crosschat_module.on_discord_message(message)
        except Exception as e:
            print(f"[crosschat] on_message error: {e}")


client.run(DISCORD_TOKEN)