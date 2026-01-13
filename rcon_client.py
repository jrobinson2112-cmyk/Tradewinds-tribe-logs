import asyncio
import time
from typing import Optional
from config import RCON_HOST, RCON_PORT, RCON_PASSWORD

RCON_LOCK = asyncio.Lock()

def _pkt(req_id: int, ptype: int, body: str) -> bytes:
    data = body.encode("utf-8") + b"\x00"
    payload = (
        req_id.to_bytes(4, "little", signed=True)
        + ptype.to_bytes(4, "little", signed=True)
        + data
        + b"\x00"
    )
    size = len(payload)
    return size.to_bytes(4, "little", signed=True) + payload

async def rcon_command(command: str, timeout: float = 8.0) -> str:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout)
    try:
        # auth
        writer.write(_pkt(1, 3, RCON_PASSWORD))
        await writer.drain()
        await asyncio.wait_for(reader.read(4096), timeout=timeout)

        # command
        writer.write(_pkt(2, 2, command))
        await writer.drain()

        chunks = []
        end = time.time() + timeout
        while time.time() < end:
            try:
                part = await asyncio.wait_for(reader.read(4096), timeout=0.35)
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
            if size < 10 or i + size > len(data):
                break
            pkt = data[i:i+size]
            i += size
            body = pkt[8:-2]
            txt = body.decode("utf-8", errors="ignore")
            if txt:
                out.append(txt)

        return "".join(out).strip()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

async def safe_rcon(command: str, timeout: float = 8.0, attempts: int = 4) -> str:
    async with RCON_LOCK:
        delay = 0.6
        last: Optional[Exception] = None
        for _ in range(attempts):
            try:
                return await rcon_command(command, timeout=timeout)
            except (ConnectionResetError, OSError, asyncio.TimeoutError) as e:
                last = e
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8.0)
        raise last if last else RuntimeError("RCON failed")