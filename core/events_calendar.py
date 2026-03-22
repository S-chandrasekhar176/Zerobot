# -*- coding: utf-8 -*-
"""
ZeroBot Pro — Events Calendar (Aladdin Gap #5)
===============================================
Tracks NSE earnings, RBI policy meetings, budget dates, and
index rebalancing events to reduce exposure before major events.

How it works:
  1. Maintains a static + dynamic calendar of known events
  2. Engine calls `events_calendar.get_event_risk(symbol, date)` before each trade
  3. Within EVENT_BUFFER_DAYS before an event: position size reduced by 50%
  4. Day of event: no new positions in affected symbols
  5. Day after event: normal sizing resumes

Why this matters (Aladdin analogy):
  Aladdin monitors 2000+ risk factors. Event risk is one of the biggest — a
  company missing earnings by 1% can cause a 10%+ gap down. This module
  creates a "soft fence" around known event dates without completely blocking trading.
"""

from datetime import date, timedelta
from typing import Optional, Tuple
from core.logger import log

# Days before event to start reducing exposure
EVENT_BUFFER_DAYS = 3

# Event risk multipliers
EVENT_SIZE_MULTIPLIER = {
    "earnings":      0.5,   # 50% of normal size
    "rbi_policy":    0.4,   # 40% (broad market impact)
    "budget":        0.3,   # 30% (highest uncertainty)
    "index_rebal":   0.7,   # 70% (moderate impact)
    "derivative_exp":0.6,   # 60% (F&O expiry volatility)
}

# ── Static Calendar (update monthly) ──────────────────────────────────────────
# Format: (date, event_type, description, affected_symbols or None for all)
STATIC_EVENTS = [
    # RBI MPC meetings 2026 (typically Feb, Apr, Jun, Aug, Oct, Dec)
    (date(2026, 4, 9),  "rbi_policy", "RBI MPC Q1 2026", None),
    (date(2026, 6, 6),  "rbi_policy", "RBI MPC Q2 2026", None),
    (date(2026, 8, 8),  "rbi_policy", "RBI MPC Q2b 2026", None),
    (date(2026, 10, 7), "rbi_policy", "RBI MPC Q3 2026", None),
    (date(2026, 12, 5), "rbi_policy", "RBI MPC Q4 2026", None),

    # NSE F&O monthly expiries (last Thursday of month)
    (date(2026, 3, 26), "derivative_exp", "Mar 2026 F&O Expiry", None),
    (date(2026, 4, 30), "derivative_exp", "Apr 2026 F&O Expiry", None),
    (date(2026, 5, 28), "derivative_exp", "May 2026 F&O Expiry", None),
    (date(2026, 6, 25), "derivative_exp", "Jun 2026 F&O Expiry", None),

    # Q3 results season (Jan-Feb) — major stocks
    # Q4 results season (Apr-May)
]

# Symbol-specific earnings (approximate — update each quarter)
EARNINGS_EVENTS = {
    "RELIANCE.NS":   [date(2026, 4, 20), date(2026, 7, 18)],
    "HDFCBANK.NS":   [date(2026, 4, 15), date(2026, 7, 13)],
    "ICICIBANK.NS":  [date(2026, 4, 22), date(2026, 7, 20)],
    "TCS.NS":        [date(2026, 4, 10), date(2026, 7, 10)],
    "INFY.NS":       [date(2026, 4, 17), date(2026, 7, 17)],
    "WIPRO.NS":      [date(2026, 4, 23), date(2026, 7, 21)],
    "HCLTECH.NS":    [date(2026, 4, 24), date(2026, 7, 22)],
    "BAJFINANCE.NS": [date(2026, 4, 28), date(2026, 7, 26)],
    "MARUTI.NS":     [date(2026, 4, 30), date(2026, 7, 28)],
    "SBIN.NS":       [date(2026, 5, 5),  date(2026, 8, 3)],
    "AXISBANK.NS":   [date(2026, 4, 25), date(2026, 7, 23)],
    "KOTAKBANK.NS":  [date(2026, 4, 27), date(2026, 7, 25)],
    "LT.NS":         [date(2026, 5, 10), date(2026, 8, 8)],
    "TATASTEEL.NS":  [date(2026, 5, 12), date(2026, 8, 10)],
    "NESTLEIND.NS":  [date(2026, 4, 29), date(2026, 7, 27)],
    "ONGC.NS":       [date(2026, 5, 14), date(2026, 8, 12)],
    "NTPC.NS":       [date(2026, 5, 15), date(2026, 8, 13)],
    "ITC.NS":        [date(2026, 5, 8),  date(2026, 8, 6)],
}


class EventsCalendar:
    """
    Maintains awareness of upcoming market-moving events.
    Engine queries this before sizing positions.
    """

    def __init__(self):
        self._events = list(STATIC_EVENTS)
        # Load earnings events
        for sym, dates in EARNINGS_EVENTS.items():
            for d in dates:
                self._events.append((d, "earnings", f"{sym} Q results", [sym]))
        log.info(f"EventsCalendar: {len(self._events)} events loaded")

    def get_event_risk(
        self,
        symbol: str,
        check_date: date = None,
        buffer_days: int = EVENT_BUFFER_DAYS,
    ) -> Tuple[float, Optional[str]]:
        """
        Returns (size_multiplier, event_description) for the given symbol.
        - size_multiplier = 1.0 means no restriction (normal sizing)
        - size_multiplier < 1.0 means reduce position size
        - size_multiplier = 0.0 means block entirely (event day)

        Example:
            mult, reason = calendar.get_event_risk("RELIANCE.NS")
            if mult < 1.0:
                qty = int(qty * mult)
        """
        today = check_date or date.today()
        min_mult = 1.0
        worst_reason = None

        for event_date, event_type, desc, affected in self._events:
            # Check if this event affects this symbol
            if affected is not None and symbol not in affected:
                continue

            days_until = (event_date - today).days

            # Event day: block new positions
            if days_until == 0:
                return 0.0, f"EVENT DAY: {desc}"

            # Within buffer: reduce size
            if 0 < days_until <= buffer_days:
                mult = EVENT_SIZE_MULTIPLIER.get(event_type, 0.5)
                # Scale: closer = more reduction
                scale = 1 - (1 - mult) * (buffer_days - days_until + 1) / buffer_days
                scale = max(mult, scale)
                if scale < min_mult:
                    min_mult = scale
                    worst_reason = f"{desc} in {days_until}d — size reduced to {scale:.0%}"

        return round(min_mult, 2), worst_reason

    def get_upcoming_events(self, days_ahead: int = 7) -> list:
        """Return list of events in the next N days."""
        today = date.today()
        cutoff = today + timedelta(days=days_ahead)
        upcoming = []
        for event_date, event_type, desc, affected in self._events:
            if today <= event_date <= cutoff:
                upcoming.append({
                    "date": event_date.isoformat(),
                    "days_until": (event_date - today).days,
                    "type": event_type,
                    "description": desc,
                    "affected": affected or ["ALL"],
                    "size_mult": EVENT_SIZE_MULTIPLIER.get(event_type, 0.5),
                })
        return sorted(upcoming, key=lambda x: x["date"])

    def is_event_day(self, symbol: str) -> bool:
        mult, _ = self.get_event_risk(symbol)
        return mult == 0.0


# Singleton
events_calendar = EventsCalendar()
