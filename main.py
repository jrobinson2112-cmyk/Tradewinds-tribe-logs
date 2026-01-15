import os
import asyncio
import discord
from discord import app_commands

from tribelogs_module import run_tribelogs_loop, setup_tribelog_commands
from time_module import run_time_loop, setup_time_commands  # <-- make sure these exist

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Your server + admin role
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

    # 1) Register commands from both modules on the SAME tree
    setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
    setup_time_commands(tree, GUILD_ID, ADMIN_ROLE_ID)

    # 2) Force a guild sync (instant vs global sync which can take ages)
    synced = await tree.sync(guild=guild_obj)

    print(f"✅ Logged in as: {client.user} ({client.user.id})")
    print(f"✅ Commands synced to guild {GUILD_ID}: {[c.name for c in synced]}")

    # 3) Start background loops
    asyncio.create_task(run_tribelogs_loop())
    asyncio.create_task(run_time_loop(client))

    print("✅ Solunaris bot online (tribelogs + time)")

client.run(DISCORD_TOKEN)