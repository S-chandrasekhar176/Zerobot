# ZeroBot v1.1 — Patch 12 Changelog

## Critical Bug Fixes

### 1. Trade Counter Showing Wrong Count on Startup
**File:** core/state_manager.py
**Root cause:** daily_trades/wins/losses restored from previous session DB state → showing "1 trade" at startup.
**Fix:** Reset all daily counters to 0 on every restart.

### 2. No Trades in 30+ Minutes (3 compounding causes fixed)
**a) Signal cooldown** core/engine.py — Was: 1 hour. Now: 15 minutes.
**b) ML direction filter** core/engine.py — Was: hard block if ML disagrees. Now: -15 confidence penalty but still allows trade (ML F1≈0.2 is nearly random so hard-blocking cut too many valid signals).
**c) ML confidence threshold** risk/risk_engine.py — Was: 55%. Now: 50% (minimum meaningful edge).

### 3. Duplicate Function in API
**File:** dashboard/api/main.py — _dynamic_max_positions() was defined twice. Fixed.

### 4. Duplicate switchTab() in Frontend
**File:** dashboard/frontend/index.html — Two switchTab() definitions, second had wrong tab list. Fixed.

### 5. Daily Win/Loss Stats Not Reset
**File:** core/state_manager.py — daily_wins, daily_losses, consecutive_losses now reset to 0 on startup.

## Dashboard Changes

### 6. Removed 11-Gate Full Grid from Overview
Now shows compact clickable pill dots only. Full detail in Intelligence tab.

### 7. Removed System Status Panel from Right Sidebar
Session Stats panel merged to include all key metrics in one place.

### 8. New Trade Ideas Panel in Right Sidebar
High-confidence news signals (|score|≥0.4) now show directly on Overview page.

### 9. Dynamic Position Limit Display
"of 5 max" → "of 8 max" for ₹55k capital. Now correctly reflects capital-based limit.

### 10. Win Rate Added to Session Stats
rt-winrate element now updates correctly in right sidebar.
