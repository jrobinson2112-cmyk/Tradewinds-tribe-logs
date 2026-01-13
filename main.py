import os
import asyncio
import discord
from discord import app_commands

from tribelogs_module import run_tribelogs_loop, setup_tribelog_commands

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1430388266393276509  # your server ID

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    # Register slash commands
    setup_tribelog_commands(tree, discord.Object(id=GUILD_ID))
    await tree.sync(guild=discord.Object(id=GUILD_ID))

    # Start tribe log loop
    asyncio.create_task(run_tribelogs_loop(client))

    print("✅ Solunaris Tribe Logs bot online")

client.run(DISCORD_TOKEN)