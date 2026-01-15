import os
import asyncio
import inspect
import discord
from discord import app_commands

import tribelogs_module
import time_module
import vcstatus_module
import players_module

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Your server (guild)
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076  # Discord Admin role

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def _start_loop(loop_func, name: str):
    """
    Robustly start a module loop.
    Handles:
      - async def loop_func(client) -> coroutine
      - def loop_func(client) -> Task | None
      - async def loop_func() -> coroutine
      - def loop_func() -> Task | None
    """
    try:
        sig = inspect.signature(loop_func)
        wants_client = len(sig.parameters) >= 1
    except Exception:
        wants_client = True

    try:
        result = loop_func(client) if wants_client else loop_func()

        # If it returned a coroutine, schedule it
        if inspect.iscoroutine(result):
            asyncio.create_task(result)
            print(f"✅ Started {name} (scheduled coroutine)")
            return

        # If it returned a Task/Future, it's already scheduled
        if isinstance(result, asyncio.Future):
            print(f"✅ Started {name} (returned Task/Future)")
            return

        # If it returned None, assume it scheduled itself internally
        print(f"✅ Started {name} (no return / self-scheduled)")

    except Exception as e:
        print(f"❌ Failed to start {name}: {e}")


@client.event
async def on_ready():
    guild_obj = discord.Object(id=GUILD_ID)

    # Register slash commands (guild-scoped so they appear instantly)
    try:
        tribelogs_module.setup_tribelog_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
    except Exception as e:
        print(f"❌ setup_tribelog_commands failed: {e}")

    try:
        time_module.setup_time_commands(tree, GUILD_ID, ADMIN_ROLE_ID)
    except Exception as e:
        print(f"❌ setup_time_commands failed: {e}")

    await tree.sync(guild=guild_obj)

    print(f"✅ Solunaris bot online | commands synced to guild {GUILD_ID}")

    # Start module loops
    _start_loop(tribelogs_module.run_tribelogs_loop, "tribelogs")
    _start_loop(time_module.run_time_loop, "time")
    _start_loop(vcstatus_module.run_vcstatus_loop, "vcstatus")
    _start_loop(players_module.run_players_loop, "players")

    print("✅ Modules running: tribelogs, time, vcstatus, players")


client.run(DISCORD_TOKEN)