import os
import time
import ftplib
import hashlib
import requests

# ============================================================
# CONFIG (ENV VARS)
# ============================================================

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Path to ShooterGame.log on Nitrado
FTP_LOG_PATH = "arksa/ShooterGame/Saved/Logs/ShooterGame.log"

# Tribe name to filter
TARGET_TRIBE = "Valkyrie"

# Poll interval (seconds)
POLL_INTERVAL = 15


# ============================================================
# VALIDATION
# ============================================================

required = [
    FTP_HOST,
    FTP_USER,
    FTP_PASS,
    DISCORD_WEBHOOK_URL,
]

if not all(required):
    raise RuntimeError("Missing required environment variables")


# ============================================================
# DISCORD FORMAT HELPERS
# ============================================================

def format_log_line(line: str) -> dict:
    """
    Returns Discord embed payload with color based on ARK log type
    """
    text = line.strip()

    color = 0x95A5A6  # default grey

    lower = text.lower()

    if "claimed" in lower or "claiming" in lower:
        color = 0x9B59B6  # purple
    elif "tamed" in lower or "taming" in lower:
        color = 0x2ECC71  # green
    elif "killed" in lower or "died" in lower:
        color = 0xE74C3C  # red
    elif "demolished" in lower or "destroyed" in lower:
        color = 0xF1C40F  # yellow

    return {
        "embeds": [
            {
                "description": text,
                "color": color
            }
        ]
    }


def send_to_discord(payload: dict):
    requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)


# ============================================================
# FTP LOG HANDLING
# ============================================================

def fetch_log() -> str:
    ftp = ftplib.FTP()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=10)
    ftp.login(FTP_USER, FTP_PASS)

    lines = []

    def handle_line(line):
        lines.append(line)

    ftp.retrlines(f"RETR {FTP_LOG_PATH}", handle_line)
    ftp.quit()

    return "\n".join(lines)


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("Tribe log forwarder started")

    last_hash = None

    while True:
        try:
            log_text = fetch_log()
            current_hash = hash_text(log_text)

            if last_hash is None:
                last_hash = current_hash
                time.sleep(POLL_INTERVAL)
                continue

            if current_hash == last_hash:
                time.sleep(POLL_INTERVAL)
                continue

            new_lines = log_text.splitlines()

            for line in new_lines:
                if TARGET_TRIBE.lower() in line.lower():
                    payload = format_log_line(line)
                    send_to_discord(payload)

            last_hash = current_hash

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(POLL_INTERVAL)


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    main()