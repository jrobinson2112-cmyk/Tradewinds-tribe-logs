import os
import asyncio
import discord
from discord import app_commands

# --- import your existing modules ---
import tribelogs_module
import time_module
import players_module
import vcstatus_module

# --- import SHARED helpers you already use ---
from rcon import rcon_command                 # <-- this already exists in your project
from webhook_helper import webhook_upsert     # <-- this already exists in your project

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1430388266393276509

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    # ----- Register slash commands -----
    tribelogs_module.setup_tribelog_commands(tree, GUILD_ID)
    time_module.setup_time_commands(tree, GUILD_ID, rcon_command)

    await tree.sync(guild=discord.Object(id=GUILD_ID))

    # ----- Start background loops -----
    asyncio.create_task(tribelogs_module.run_tribelogs_loop())
    asyncio.create_task(time_module.run_time_loop(client, rcon_command, webhook_upsert))
    asyncio.create_task(players_module.run_players_loop())
    asyncio.create_task(vcstatus_module.run_vcstatus_loop(client))

    print("✅ Solunaris bot online | modules running: tribelogs, time, players, vcstatus")

client.run(DISCORD_TOKEN)