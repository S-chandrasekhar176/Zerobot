"""
ZeroBot — Market Clock
Handles all market session timing, holiday checks, IST conversions.

PATCH13 FIX: Replaced generic holidays.India() library with NSE's official
exchange holiday calendar. The library included state/regional holidays that
NSE doesn't observe, causing is_market_day() to return False on normal trading
days and blocking all signals.
"""
from datetime import datetime, time, date
from zoneinfo import ZoneInfo
import os as _os

# ── TEST MODE: set ZEROBOT_FORCE_MARKET_OPEN=1 to bypass market hours
_FORCE_OPEN = _os.environ.get("ZEROBOT_FORCE_MARKET_OPEN", "0") == "1"
if _FORCE_OPEN:
    import warnings
    warnings.warn("ZEROBOT_FORCE_MARKET_OPEN=1 — market hours bypassed (TEST MODE ONLY)")

from core.config import cfg

IST = ZoneInfo(cfg.timezone)

# ── NSE Official Exchange Holidays 2025 & 2026 ────────────────────────────
# Source: NSE India official circular. Updated annually.
# NOTE: This is the NSE exchange holiday list, NOT Indian public holidays.
# They differ — NSE has its own calendar that does not include all state holidays.
NSE_HOLIDAYS: set = {
    # 2025
    date(2025, 1, 26),   # Republic Day
    date(2025, 2, 26),   # Maha Shivaratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-Ul-Fitr (Ramzan Eid)
    date(2025, 4, 10),   # Shri Ram Navami
    date(2025, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Gandhi Jayanti / Dussehra
    date(2025, 10, 21),  # Diwali Amavasya
    date(2025, 10, 24),  # Diwali (Laxmi Pujan)  - Muhurat Trading
    date(2025, 11, 5),   # Prakash Gurpurb
    date(2025, 12, 25),  # Christmas
    # 2026
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 26),   # Maha Shivaratri
    date(2026, 3, 14),   # Holi
    date(2026, 3, 31),   # Id-Ul-Fitr (Ramzan Eid) - tentative
    date(2026, 4, 2),    # Ram Navami
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 10),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 4, 14),   # Mahavir Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 8, 15),   # Independence Day
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 10, 26),  # Diwali Laxmi Pujan
    date(2026, 11, 13),  # Gurunanak Jayanti
    date(2026, 12, 25),  # Christmas
}


def now_ist() -> datetime:
    return datetime.now(IST)

def today_ist() -> date:
    return now_ist().date()

def is_holiday() -> bool:
    """Check against NSE official holiday list only."""
    return today_ist() in NSE_HOLIDAYS

def is_weekend() -> bool:
    return today_ist().weekday() >= 5  # Sat=5, Sun=6

def is_market_day() -> bool:
    if _FORCE_OPEN:
        return True
    return not is_weekend() and not is_holiday()

def _parse_time(t_str: str) -> time:
    h, m = map(int, t_str.split(":"))
    return time(h, m)

def is_market_hours() -> bool:
    """True if within trading session (9:15 – 15:25 IST)."""
    if _FORCE_OPEN:
        return True
    if not is_market_day():
        return False
    now_t = now_ist().time()
    return _parse_time(cfg.session_start) <= now_t <= _parse_time(cfg.session_end)

def is_warmup_period() -> bool:
    """True during the first 15 min (9:15–9:30) — strategies skip but market IS open."""
    if not is_market_day():
        return False
    now_t = now_ist().time()
    return _parse_time(cfg.session_start) <= now_t < _parse_time(cfg.warmup_end)

def is_closing_period() -> bool:
    """True in last 10 min — avoid new trades."""
    if not is_market_day():
        return False
    now_t = now_ist().time()
    return now_t >= _parse_time(cfg.closing_avoid)

def minutes_to_close() -> int:
    """How many minutes until market closes."""
    now = now_ist()
    close = now.replace(hour=15, minute=25, second=0, microsecond=0)
    delta = (close - now).total_seconds() / 60
    return max(0, int(delta))

def session_status() -> dict:
    return {
        "is_market_day":   is_market_day(),
        "is_market_hours": is_market_hours(),
        "is_warmup":       is_warmup_period(),
        "is_closing":      is_closing_period(),
        "is_holiday":      is_holiday(),
        "is_weekend":      is_weekend(),
        "current_ist":     now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
        "minutes_to_close": minutes_to_close(),
    }
