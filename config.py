import os

# Discord / Guild
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

# Webhooks
TIME_WEBHOOK_URL = os.getenv("WEBHOOK_URL")               # time webhook
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")    # players webhook

# RCON
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "0") or 0)
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

# Bot token
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

def require_env():
    missing = []
    for k in ["DISCORD_TOKEN", "RCON_HOST", "RCON_PORT", "RCON_PASSWORD", "WEBHOOK_URL", "PLAYERS_WEBHOOK_URL"]:
        if not os.getenv(k):
            missing.append(k)
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))