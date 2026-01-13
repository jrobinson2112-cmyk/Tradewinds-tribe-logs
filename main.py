import os
import asyncio
import discord
from discord import app_commands

from tribelogs_module import run_tribelogs_loop, setup_tribelog_commands
from players_module import run_players_loop

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    # setup commands (tribelogs_module expects an int guild_id)
    setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)

    # sync to your guild so commands appear instantly
    await tree.sync(guild=discord.Object(id=GUILD_ID))

    # start loops
    asyncio.create_task(run_tribelogs_loop(client))
    asyncio.create_task(run_players_loop())

    print("✅ Solunaris bot online")

client.run(DISCORD_TOKEN)