import os
import time
import json
import asyncio
import hashlib
import aiohttp

# =====================
# ENV VALIDATION
# =====================
RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "0"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")
TRIBE_ROUTES_RAW = os.getenv("TRIBE_ROUTES")

if not all([RCON_HOST, RCON_PORT, RCON_PASSWORD, TRIBE_ROUTES_RAW]):
    raise RuntimeError("Missing required environment variables")

try:
    TRIBE_ROUTES = json.loads(TRIBE_ROUTES_RAW)
except Exception as e:
    raise RuntimeError("TRIBE_ROUTES must be valid JSON") from e

POLL_INTERVAL = 10.0

# =====================
# RCON (Source protocol)
# =====================
def _rcon_packet(req_id: int, ptype: int, body: str) -> bytes:
    data = body.encode("utf-8") + b"\x00"
    packet = (
        req_id.to_bytes(4, "little", signed=True)
        + ptype.to_bytes(4, "little", signed=True)
        + data
        + b"\x00"
    )
    return len(packet).to_bytes(4, "little", signed=True) + packet


async def rcon_command(command: str, timeout: float = 6.0) -> str:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout
    )
    try:
        writer.write(_rcon_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        await reader.read(4096)

        writer.write(_rcon_packet(2, 2, command))
        await writer.drain()

        chunks = []
        end = time.time() + timeout
        while time.time() < end:
            try:
                part = await asyncio.wait_for(reader.read(4096), timeout=0.3)
            except asyncio.TimeoutError:
                break
            if not part:
                break
            chunks.append(part)

        data = b"".join(chunks)
        out = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i:i+4], "little", signed=True)
            i += 4
            pkt = data[i:i+size]
            i += size
            body = pkt[8:-2]
            txt = body.decode("utf-8", errors="ignore")
            if txt:
                out.append(txt)

        return "".join(out).strip()
    finally:
        writer.close()
        await writer.wait_closed()

# =====================
# COLOR LOGIC
# =====================
def event_color(text: str) -> int:
    t = text.lower()

    if any(k in t for k in ["killed", "died", "death", "destroyed"]):
        return 0xE74C3C  # Red
    if "demolished" in t:
        return 0xF1C40F  # Yellow
    if "claimed" in t or "unclaimed" in t:
        return 0x9B59B6  # Purple
    if "tamed" in t or "taming" in t:
        return 0x2ECC71  # Green
    if "alliance" in t or "allied" in t:
        return 0x5DADE2  # Light Blue

    return 0xECF0F1  # White

# =====================
# LOG CLEANING
# =====================
def clean_line(line: str) -> str:
    if "Tribe " in line:
        line = line.split("Tribe ", 1)[1]
    if ": Day " in line:
        line = "Day " + line.split(": Day ", 1)[1]

    return (
        line.replace("</>)", "")
            .replace("))", ")")
            .strip()
    )

# =====================
# MAIN LOOP
# =====================
async def main():
    print("Routing tribes:", ", ".join(TRIBE_ROUTES.keys()))
    sent_hashes = set()

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                raw = await rcon_command("GetGameLog")
                lines = raw.splitlines()

                for tribe, cfg in TRIBE_ROUTES.items():
                    matches = [l for l in lines if f"Tribe {tribe}" in l]
                    if not matches:
                        continue

                    latest = matches[-1]
                    cleaned = clean_line(latest)
                    h = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()

                    if h in sent_hashes:
                        continue

                    sent_hashes.add(h)

                    payload = {
                        "embeds": [{
                            "description": cleaned,
                            "color": event_color(cleaned),
                        }]
                    }

                    async with session.post(
                        cfg["webhook"],
                        params={"thread_id": cfg["thread_id"]},
                        json=payload
                    ) as r:
                        if r.status >= 400:
                            print("Discord error:", await r.text())

            except Exception as e:
                print("Error:", e)

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())