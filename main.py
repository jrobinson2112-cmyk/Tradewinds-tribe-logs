import os
import asyncio
import discord
from discord import app_commands

from tribelogs_module import run_tribelogs_loop, setup_tribelog_commands
from players_module import run_players_loop

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1430388266393276509  # your server ID

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    # Register slash commands for tribe logs
    setup_tribelog_commands(tree, discord.Object(id=GUILD_ID))
    await tree.sync(guild=discord.Object(id=GUILD_ID))

    # Start background loops
    asyncio.create_task(run_tribelogs_loop(client))
    asyncio.create_task(run_players_loop())

    print("✅ Solunaris Tribe Logs + Players bot online")

client.run(DISCORD_TOKEN)