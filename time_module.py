# time_module.py
#
# Solunaris (ASA) time engine using Nitrado/ASA day-night math.
# - ASA default: full day/night cycle = 1 real hour
# - Sunrise 05:30, Sunset 17:30 (12h day / 12h night)
# - Applies multipliers exactly like ASA server settings:
#     effective_speed = DayCycleSpeedScale * (DayTimeSpeedScale or NightTimeSpeedScale)
#     SPM (seconds per in-game minute) = BASE_SPM / effective_speed
#
# Drop this module into your project and import the functions you need.

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Optional, Tuple


# =====================
# ASA / NITRADO CONSTANTS
# =====================

# ASA default: 1 real hour per in-game day -> 3600 real seconds for 1440 in-game minutes
BASE_SPM = 2.5  # 3600 / 1440

# ✅ YOUR NITRADO VALUES (from your screenshot)
DAY_CYCLE_SPEED_SCALE = 5.92
DAY_TIME_SPEED_SCALE = 1.85
NIGHT_TIME_SPEED_SCALE = 2.18

# ✅ Derived SPMs (seconds per in-game minute)
DAY_SPM = BASE_SPM / (DAY_CYCLE_SPEED_SCALE * DAY_TIME_SPEED_SCALE)
NIGHT_SPM = BASE_SPM / (DAY_CYCLE_SPEED_SCALE * NIGHT_TIME_SPEED_SCALE)

# ✅ ASA fixed sunrise/sunset
SUNRISE = 5 * 60 + 30   # 05:30
SUNSET = 17 * 60 + 30   # 17:30

# Calendar model: 365 days per year
DAYS_PER_YEAR = 365
MINUTES_PER_DAY = 1440


# =====================
# STATE
# =====================

STATE_FILE_DEFAULT = "state.json"


@dataclass
class SolunarisState:
    """
    epoch: real unix timestamp anchor (seconds)
    year/day/hour/minute: the in-game time at that epoch
    """
    epoch: float
    year: int
    day: int
    hour: int
    minute: int

    def to_dict(self) -> dict:
        return {
            "epoch": float(self.epoch),
            "year": int(self.year),
            "day": int(self.day),
            "hour": int(self.hour),
            "minute": int(self.minute),
        }

    @staticmethod
    def from_dict(d: dict) -> "SolunarisState":
        return SolunarisState(
            epoch=float(d["epoch"]),
            year=int(d["year"]),
            day=int(d["day"]),
            hour=int(d["hour"]),
            minute=int(d["minute"]),
        )


def load_state(state_file: str = STATE_FILE_DEFAULT) -> Optional[SolunarisState]:
    if not os.path.exists(state_file):
        return None
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return SolunarisState.from_dict(json.load(f))
    except Exception:
        return None


def save_state(state: SolunarisState, state_file: str = STATE_FILE_DEFAULT) -> None:
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f)


# =====================
# TIME LOGIC
# =====================

def is_day(minute_of_day: int) -> bool:
    """True if current in-game minute is between 05:30 inclusive and 17:30 exclusive."""
    return SUNRISE <= minute_of_day < SUNSET


def spm_for_minute(minute_of_day: int) -> float:
    """Seconds per in-game minute at this in-game minute (day vs night)."""
    return DAY_SPM if is_day(minute_of_day) else NIGHT_SPM


def _advance_one_minute(minute_of_day: int, day: int, year: int) -> Tuple[int, int, int]:
    minute_of_day += 1
    if minute_of_day >= MINUTES_PER_DAY:
        minute_of_day = 0
        day += 1
        if day > DAYS_PER_YEAR:
            day = 1
            year += 1
    return minute_of_day, day, year


def calculate_time_details(state: SolunarisState) -> Tuple[int, int, int, float, float]:
    """
    Returns:
      minute_of_day (0..1439)
      day (1..365)
      year (>=1)
      seconds_into_current_minute (real seconds since current in-game minute started)
      current_minute_spm (real seconds per in-game minute)
    """
    elapsed = float(time.time() - state.epoch)
    minute_of_day = int(state.hour) * 60 + int(state.minute)
    day = int(state.day)
    year = int(state.year)

    remaining = elapsed

    # Progress through full in-game minutes, consuming real seconds according to current SPM.
    while True:
        cur_spm = spm_for_minute(minute_of_day)
        if remaining >= cur_spm:
            remaining -= cur_spm
            minute_of_day, day, year = _advance_one_minute(minute_of_day, day, year)
            continue

        seconds_into_current_minute = remaining
        return minute_of_day, day, year, seconds_into_current_minute, cur_spm


def seconds_until_next_round_step(
    minute_of_day: int,
    seconds_into_minute: float,
    step_minutes: int,
) -> float:
    """
    Real seconds until the next in-game minute boundary where minute_of_day % step_minutes == 0.
    If already on a boundary, returns time until the NEXT boundary (step minutes ahead).
    """
    m = minute_of_day
    mod = m % step_minutes
    minutes_to_boundary = (step_minutes - mod) if mod != 0 else step_minutes

    cur_spm = spm_for_minute(m)
    remaining_in_current_minute = max(0.0, cur_spm - seconds_into_minute)
    total = remaining_in_current_minute

    # Add SPM for the minutes between next tick and the boundary tick
    m2 = m
    d2 = 1
    y2 = 1
    # (d2/y2 not actually needed for spm, since day/night depends only on minute_of_day)
    for _ in range(minutes_to_boundary - 1):
        # advance one minute on the clock
        m2 = (m2 + 1) % MINUTES_PER_DAY
        total += spm_for_minute(m2)

    return max(0.5, total)


def minute_of_day_to_hhmm(minute_of_day: int) -> Tuple[int, int]:
    return minute_of_day // 60, minute_of_day % 60


def build_time_embed(minute_of_day: int, day: int, year: int) -> dict:
    hour, minute = minute_of_day_to_hhmm(minute_of_day)
    emoji = "☀️" if is_day(minute_of_day) else "🌙"
    color = 0xF1C40F if is_day(minute_of_day) else 0x5865F2
    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    return {"title": title, "color": color}


# =====================
# OPTIONAL: APPLY SYNC FROM PARSED GAME TIME
# =====================

def apply_sync_from_gamelog(
    state: SolunarisState,
    parsed_day: int,
    parsed_hour: int,
    parsed_minute: int,
) -> SolunarisState:
    """
    Adjusts the anchor (epoch) so that "now" aligns with (parsed_day, parsed_hour, parsed_minute),
    while keeping your SPM model intact.

    This is "hard" sync. If you want "soft" corrections, do that outside this module.
    """
    # Current computed time
    cur_minute_of_day, cur_day, cur_year, seconds_into_minute, _cur_spm = calculate_time_details(state)

    target_minute_of_day = int(parsed_hour) * 60 + int(parsed_minute)

    # Handle day wrap-around within a year (closest direction)
    day_diff = int(parsed_day) - int(cur_day)
    if day_diff > (DAYS_PER_YEAR // 2):
        day_diff -= DAYS_PER_YEAR
    elif day_diff < -(DAYS_PER_YEAR // 2):
        day_diff += DAYS_PER_YEAR

    # Total in-game minutes of drift (day_diff + time_diff)
    minute_diff = (day_diff * MINUTES_PER_DAY) + (target_minute_of_day - cur_minute_of_day)

    # Clamp to nearest 12 hours to avoid weird wrap cases
    while minute_diff > 720:
        minute_diff -= 1440
    while minute_diff < -720:
        minute_diff += 1440

    # Convert in-game minutes drift to real seconds shift.
    # For small drifts this is fine. (If you ever do huge shifts, you’d integrate minute-by-minute.)
    real_seconds_shift = float(minute_diff) * spm_for_minute(cur_minute_of_day)

    # If your computed time is ahead (minute_diff negative), we need to move epoch forward, etc.
    new_epoch = float(state.epoch) - real_seconds_shift

    return SolunarisState(
        epoch=new_epoch,
        year=int(cur_year),          # keep year unless you also parse year from logs
        day=int(parsed_day),
        hour=int(parsed_hour),
        minute=int(parsed_minute),
    )