import os
import asyncio
import discord
from discord import app_commands

from tribelogs_module import run_tribelogs_loop, setup_tribelog_commands

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

    # Register commands
    setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)

    # Force sync to this guild
    synced = await tree.sync(guild=guild_obj)

    print(f"✅ Logged in as: {client.user} ({client.user.id})")
    print(f"✅ Commands synced to guild {GUILD_ID}: {[c.name for c in synced]}")

    # Start background loop
    asyncio.create_task(run_tribelogs_loop())

client.run(DISCORD_TOKEN)