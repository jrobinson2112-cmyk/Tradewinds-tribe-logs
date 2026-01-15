import os
import asyncio
import discord
from discord import app_commands

from tribelogs_module import run_tribelogs_loop, setup_tribelog_commands
from time_module import time_loop, setup_time_commands  # <-- uses your existing function

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

if not DISCORD_TOKEN:
    raise RuntimeError("Missing required environment variable: DISCORD_TOKEN")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    guild_obj = discord.Object(id=GUILD_ID)

    # register both modules' commands on the SAME tree
    setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
    setup_time_commands(tree, GUILD_ID, ADMIN_ROLE_ID)

    synced = await tree.sync(guild=guild_obj)
    print(f"✅ Commands synced: {[c.name for c in synced]}")

    # start loops
    asyncio.create_task(run_tribelogs_loop())
    asyncio.create_task(time_loop(client))  # <-- pass client if your time_loop expects it

    print("✅ Solunaris bot online (tribelogs + time)")

client.run(DISCORD_TOKEN)