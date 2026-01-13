import os
import discord
from discord import app_commands

from tribelogs_module import run_tribelogs_loop, setup_tribelog_commands

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076  # Discord Admin role

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Register commands BEFORE syncing (important)
setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)

@client.event
async def on_ready():
    # Sync commands to your guild so they appear immediately
    await tree.sync(guild=discord.Object(id=GUILD_ID))

    # Start background polling once
    if not getattr(client, "_tribelogs_started", False):
        run_tribelogs_loop()
        client._tribelogs_started = True

    print(f"✅ Solunaris Tribe Logs bot online | commands synced to guild {GUILD_ID}")

client.run(DISCORD_TOKEN)