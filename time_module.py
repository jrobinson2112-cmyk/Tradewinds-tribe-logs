import os, time, json, asyncio, aiohttp
from config import TIME_WEBHOOK_URL

STATE_FILE = "state.json"

DAY_SPM = 4.7666667
NIGHT_SPM = 4.045
SUNRISE = 5 * 60 + 30
SUNSET = 17 * 60 + 30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

TIME_UPDATE_STEP_MINUTES = 10

message_id = None
last_announced_day = None

def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)

state = load_state()

def is_day(m): return SUNRISE <= m < SUNSET
def spm(m): return DAY_SPM if is_day(m) else NIGHT_SPM

def _advance_one_minute(m, d, y):
    m += 1
    if m >= 1440:
        m = 0; d += 1
        if d > 365:
            d = 1; y += 1
    return m, d, y

def calculate_time_details():
    if not state:
        return None
    elapsed = float(time.time() - state["epoch"])
    m = int(state["hour"]) * 60 + int(state["minute"])
    d = int(state["day"])
    y = int(state["year"])
    rem = elapsed
    while True:
        cur = spm(m)
        if rem >= cur:
            rem -= cur
            m, d, y = _advance_one_minute(m, d, y)
            continue
        return m, d, y, rem, cur

def build_time_embed(m, d, y):
    hh = m // 60
    mm = m % 60
    emoji = "☀️" if is_day(m) else "🌙"
    color = DAY_COLOR if is_day(m) else NIGHT_COLOR
    return {"title": f"{emoji} | Solunaris Time | {hh:02d}:{mm:02d} | Day {d} | Year {y}", "color": color}

async def upsert(session: aiohttp.ClientSession, embed: dict):
    global message_id
    if message_id:
        async with session.patch(f"{TIME_WEBHOOK_URL}/messages/{message_id}", json={"embeds": [embed]}) as r:
            if r.status == 404:
                message_id = None
                return await upsert(session, embed)
        return
    async with session.post(TIME_WEBHOOK_URL + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json()
        if "id" in data:
            message_id = data["id"]

def seconds_until_next_round_step(m, seconds_into_minute, step):
    mod = m % step
    minutes_to_boundary = (step - mod) if mod != 0 else step
    remaining_in_current = max(0.0, spm(m) - seconds_into_minute)
    total = remaining_in_current
    m2 = m
    for _ in range(minutes_to_boundary - 1):
        m2, _, _ = _advance_one_minute(m2, 1, 1)
        total += spm(m2)
    return max(0.5, total)

async def run_time_loop(client):
    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while True:
            details = calculate_time_details()
            if not details:
                await asyncio.sleep(5); continue
            m, d, y, sec_into, _ = details
            sleep_for = seconds_until_next_round_step(m, sec_into, TIME_UPDATE_STEP_MINUTES)
            if (m % TIME_UPDATE_STEP_MINUTES) == 0:
                await upsert(session, build_time_embed(m, d, y))
            await asyncio.sleep(sleep_for)