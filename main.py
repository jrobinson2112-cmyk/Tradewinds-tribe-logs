import os
import asyncio
import discord
from discord import app_commands

from time_module import (
    setup_time_commands,
    run_time_loop,
    run_gamelog_sync_loop,
)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    setup_time_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
    await tree.sync(guild=discord.Object(id=GUILD_ID))

    asyncio.create_task(run_time_loop(client))
    asyncio.create_task(run_gamelog_sync_loop())

    print("✅ Solunaris Time bot online")


client.run(DISCORD_TOKEN)