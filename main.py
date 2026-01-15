import os
import asyncio
import inspect
import discord
from discord import app_commands

from tribelogs_module import run_tribelogs_loop, setup_tribelog_commands
from time_module import run_time_loop, setup_time_commands

# These exist in your repo per your screenshot / setup
from vcstatus_module import run_vcstatus_loop

# Optional: only if the file exists
try:
    import players_module
except ModuleNotFoundError:
    players_module = None


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def _call_maybe_with_client(func, client_obj):
    """
    Calls a function that may be defined as:
      - func()
      - func(client)
    Returns whatever the function returns.
    """
    try:
        sig = inspect.signature(func)
        if len(sig.parameters) == 0:
            return func()
        return func(client_obj)
    except (TypeError, ValueError):
        # Fallback if signature can't be inspected for any reason
        try:
            return func(client_obj)
        except TypeError:
            return func()


@client.event
async def on_ready():
    guild_obj = discord.Object(id=GUILD_ID)

    # Register slash commands
    setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
    setup_time_commands(tree, GUILD_ID, ADMIN_ROLE_ID)

    # Sync commands to your guild only (fast + reliable)
    await tree.sync(guild=guild_obj)

    # Start loops
    # Tribe logs loop is a coroutine
    asyncio.create_task(run_tribelogs_loop())

    # Time module starts its own async loop(s)
    # (Your time_module is written to be started with client)
    run_time_loop(client)

    # VC Status loop is a coroutine taking (client)
    asyncio.create_task(run_vcstatus_loop(client))

    # Players module (optional): supports either run_players_loop() or run_players_loop(client)
    if players_module and hasattr(players_module, "run_players_loop"):
        _call_maybe_with_client(players_module.run_players_loop, client)
        print("✅ players_module loop enabled")
    else:
        print("ℹ️ players_module.py not found or has no run_players_loop — skipping players loop")

    print(f"✅ Solunaris bot online | commands synced to guild {GUILD_ID}")
    print("✅ Modules running: tribelogs, time, vcstatus" + (", players" if players_module else ""))


client.run(DISCORD_TOKEN)