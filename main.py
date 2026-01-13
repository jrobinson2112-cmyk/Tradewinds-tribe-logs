import os
import asyncio
import discord
from discord import app_commands

from tribelogs_module import run_tribelogs_loop, setup_tribelog_commands
from players_module import run_players_loop

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1430388266393276509  # your server ID
ADMIN_ROLE_ID = 1439069787207766076  # Discord Admin role id

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    guild_obj = discord.Object(id=GUILD_ID)

    # Register slash commands for tribe logs (requires admin role id)
    setup_tribelog_commands(tree, guild_obj, ADMIN_ROLE_ID)
    await tree.sync(guild=guild_obj)

    # Start background loops
    asyncio.create_task(run_tribelogs_loop(client))
    asyncio.create_task(run_players_loop())

    print("✅ Solunaris Tribe Logs + Players bot online")

client.run(DISCORD_TOKEN)