import os
import asyncio
import discord
from discord import app_commands

import tribelogs_module
import time_module
import players_module
import vcstatus_module

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    guild_obj = discord.Object(id=GUILD_ID)

    # ---- Register commands ----
    # Tribe logs
    try:
        tribelogs_module.setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
    except TypeError:
        tribelogs_module.setup_tribelog_commands(tree, GUILD_ID)

    # Time (use the SAME rcon_command that tribe logs uses, if present)
    rcon_cmd = getattr(tribelogs_module, "rcon_command", None)
    if rcon_cmd is not None:
        try:
            time_module.setup_time_commands(tree, GUILD_ID, ADMIN_ROLE_ID, rcon_cmd)
        except TypeError:
            try:
                time_module.setup_time_commands(tree, GUILD_ID, rcon_cmd)
            except TypeError:
                time_module.setup_time_commands(tree, GUILD_ID)
    else:
        # If your time module doesn't need rcon, this is fine
        try:
            time_module.setup_time_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
        except TypeError:
            time_module.setup_time_commands(tree, GUILD_ID)

    await tree.sync(guild=guild_obj)

    # ---- Start loops ----
    # Tribe logs loop
    try:
        asyncio.create_task(tribelogs_module.run_tribelogs_loop())
    except TypeError:
        asyncio.create_task(tribelogs_module.run_tribelogs_loop(client))

    # Time loop
    if rcon_cmd is not None:
        try:
            asyncio.create_task(time_module.run_time_loop(client, rcon_cmd))
        except TypeError:
            try:
                asyncio.create_task(time_module.run_time_loop())
            except TypeError:
                asyncio.create_task(time_module.time_loop())
    else:
        try:
            asyncio.create_task(time_module.run_time_loop())
        except TypeError:
            asyncio.create_task(time_module.time_loop())

    # Players loop
    try:
        asyncio.create_task(players_module.run_players_loop())
    except TypeError:
        asyncio.create_task(players_module.run_players_loop(client))

    # VC status loop
    try:
        asyncio.create_task(vcstatus_module.run_vcstatus_loop(client))
    except TypeError:
        asyncio.create_task(vcstatus_module.run_vcstatus_loop())

    print(f"✅ Solunaris bot online | commands synced to guild {GUILD_ID}")
    print("✅ Modules running: tribelogs, time, vcstatus, players")


client.run(DISCORD_TOKEN)