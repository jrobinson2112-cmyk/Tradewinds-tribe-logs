import os
import asyncio
import discord
from discord import app_commands

from time_module import setup_time_commands, start_time_tasks

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    guild_obj = discord.Object(id=GUILD_ID)

    # 1) Register time commands FIRST
    setup_time_commands(tree, GUILD_ID, ADMIN_ROLE_ID)

    # 2) THEN sync to guild (instant availability)
    synced = await tree.sync(guild=guild_obj)
    print("✅ Commands synced to guild:", [c.name for c in synced])

    # 3) Start time tasks
    start_time_tasks(client)

    print("✅ Solunaris Time bot online")

client.run(DISCORD_TOKEN)