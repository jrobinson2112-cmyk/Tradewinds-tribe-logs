import discord
from discord import app_commands
from config import require_env, DISCORD_TOKEN, GUILD_ID
from time_module import run_time_loop
from players_module import run_players_loop
from tribelogs_module import run_tribelogs_loop

require_env()

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    client.loop.create_task(run_time_loop(client))
    client.loop.create_task(run_players_loop(client))
    client.loop.create_task(run_tribelogs_loop(client))
    print("✅ Solunaris bot online (modules separated)")

client.run(DISCORD_TOKEN)