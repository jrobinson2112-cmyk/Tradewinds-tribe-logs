import os
import asyncio
import discord
from discord import app_commands

# Modules
from tribelogs_module import run_tribelogs_loop, setup_tribelog_commands
from players_module import run_players_loop  # assumes your working players module exposes this
from status_module import run_status_loop    # assumes your working vc status module exposes this
from time_module import run_time_loop, setup_time_commands

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Your server / role IDs
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    guild_obj = discord.Object(id=GUILD_ID)

    # Register slash commands (time + tribelogs)
    setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
    setup_time_commands(tree, GUILD_ID, ADMIN_ROLE_ID)

    # Sync commands to your guild (fast + reliable)
    await tree.sync(guild=guild_obj)

    # Start loops (ONLY schedule tasks here)
    # tribelogs_module: returns a Task OR coroutine depending on your version, so call the wrapper you already use.
    asyncio.create_task(run_tribelogs_loop())

    # players/status/time loops (your working modules)
    # If your modules already create their own tasks internally, call them directly (no create_task).
    run_players_loop(client)
    run_status_loop(client)
    run_time_loop(client)

    print(f"✅ Solunaris bot online | commands synced to guild {GUILD_ID}")


client.run(DISCORD_TOKEN)