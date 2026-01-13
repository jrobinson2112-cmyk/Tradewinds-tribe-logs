import time, asyncio, aiohttp
from config import PLAYERS_WEBHOOK_URL
from rcon_client import safe_rcon

PLAYER_CAP = 42
STATUS_POLL_SECONDS = 45

message_id = None

def parse_listplayers(output: str):
    players = []
    if not output:
        return players
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if ". " in line:
            line = line.split(". ", 1)[1]
        name = line.split(",", 1)[0].strip() if "," in line else line
        if name and name.lower() not in ("executing", "listplayers", "done"):
            players.append(name)
    return players

async def upsert(session: aiohttp.ClientSession, embed: dict):
    global message_id
    if message_id:
        async with session.patch(f"{PLAYERS_WEBHOOK_URL}/messages/{message_id}", json={"embeds": [embed]}) as r:
            if r.status == 404:
                message_id = None
                return await upsert(session, embed)
        return
    async with session.post(PLAYERS_WEBHOOK_URL + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json()
        if "id" in data:
            message_id = data["id"]

async def run_players_loop(client):
    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                out = await safe_rcon("ListPlayers", timeout=7.0)
                names = parse_listplayers(out)
                count = len(names)
                emoji = "🟢" if True else "🔴"
                desc = f"**{count}/{PLAYER_CAP}** online\n\n" + ("\n".join([f"{i+1:02d}) {n}" for i, n in enumerate(names[:50])]) if names else "*No player list returned.*")
                embed = {"title": "Online Players", "description": desc, "color": 0x2ECC71, "footer": {"text": f"Last update: {time.strftime('%H:%M:%S')}"}}
                await upsert(session, embed)
            except Exception as e:
                # Don’t crash loop; just log
                print(f"ListPlayers error: {e}")
            await asyncio.sleep(STATUS_POLL_SECONDS)