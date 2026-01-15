import os
import asyncio
import discord
from discord import app_commands

from tribelogs_module import run_tribelogs_loop, setup_tribelog_commands
from time_module import run_time_loop, setup_time_commands

# Optional modules (only if you actually have these files)
try:
    from players_module import run_players_loop
except ModuleNotFoundError:
    run_players_loop = None

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    guild_obj = discord.Object(id=GUILD_ID)

    # Register slash commands
    setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
    setup_time_commands(tree, GUILD_ID, ADMIN_ROLE_ID)

    # Sync commands to guild
    await tree.sync(guild=guild_obj)

    # Start loops
    # tribelogs loop is async (coroutine) in your current setup
    asyncio.create_task(run_tribelogs_loop())

    # time loop should start its own task(s)
    run_time_loop(client)

    # players loop if module exists
    if run_players_loop:
        run_players_loop(client)
        print("✅ Players loop enabled")
    else:
        print("ℹ️ players_module.py not found — skipping players loop")

    print(f"✅ Solunaris bot online | commands synced to guild {GUILD_ID}")

client.run(DISCORD_TOKEN)