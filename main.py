import os
import asyncio
import discord
from discord import app_commands

from tribelogs_module import setup_tribelog_commands, run_tribelogs_loop
from players_module import run_players_loop
from vcstatus_module import run_vcstatus_loop

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
    await tree.sync(guild=discord.Object(id=GUILD_ID))

    asyncio.create_task(run_tribelogs_loop())
    asyncio.create_task(run_players_loop())
    asyncio.create_task(run_vcstatus_loop(client))

    print("✅ Solunaris bot online (tribelogs + players + vcstatus)")

client.run(DISCORD_TOKEN)