import os
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
    setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)

    # Sync commands to your guild so they show up immediately
    await tree.sync(guild=discord.Object(id=GUILD_ID))

    # IMPORTANT: these functions already start their own tasks internally
    run_tribelogs_loop()
    run_players_loop()

    print("✅ Solunaris bot online")

client.run(DISCORD_TOKEN)