import os
import asyncio
import discord
from discord import app_commands

from tribelogs_module import setup_tribelog_commands, tribelogs_start_polling

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "1430388266393276509"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "1439069787207766076"))

if not DISCORD_TOKEN:
    raise RuntimeError("Missing required environment variable: DISCORD_TOKEN")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    # Register guild-only commands so they appear instantly
    setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
    await tree.sync(guild=discord.Object(id=GUILD_ID))

    # Start the tribe log polling loop
    tribelogs_start_polling()

    print(f"✅ Solunaris Tribe Logs bot online | commands synced to guild {GUILD_ID}")

client.run(DISCORD_TOKEN)