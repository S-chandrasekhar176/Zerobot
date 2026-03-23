# ZeroBot Changelog

All notable changes are documented here. See [CHANGELOG format](https://keepachangelog.com/).

---

## [Z1 G3-ML Open Source Edition] — 2026-03-23

### 🎉 Major Milestone: Open Source Release

This release transforms ZeroBot into a professional, open-source project suitable for "Claude for Open Source" and similar programs.

### New Documentation
- ✨ **Professional README.md** with badges, architecture overview, and feature list
- ✨ **docs/architecture.md** (1000+ words) — Complete system design, component breakdown, data flow
- ✨ **docs/usage.md** (1000+ words) — Setup, configuration, troubleshooting, API reference
- ✨ **CONTRIBUTING.md** (1000+ words) — How to contribute, coding standards, adding strategies
- ✨ **QUICKSTART.md** — 5-minute setup guide for new users
- ✨ **config/.env.example** — Fully documented environment template with setup instructions

### New Code Structure
- ✨ **Created `/docs/` folder** — Centralized documentation
- ✨ **Created `/examples/` folder** — Runnable demo scripts
  - `examples/run_bot.py` — How to initialize and run ZeroBot
  - `examples/test_strategy.py` — How to test strategies in isolation
- ✨ **Improved `/examples/__init__.py`** — Makes examples a proper Python package

### Code Quality Improvements
- ✨ **Enhanced docstrings** — BotState, BaseStrategy with comprehensive Sphinx-style docs
- ✨ **Improved .gitignore** — Comprehensive 100+ rules for all file types
- ✨ **Project templates** — MIT LICENSE, CONTRIBUTING guidelines
- ✨ **Type hints** — Consistent type annotations across core modules

### Project Metadata
- 📄 **LICENSE** (MIT) — Clear open-source licensing
- 📄 **CONTRIBUTING.md** — Detailed contribution guidelines
- 📄 **.gitignore** — Professional, comprehensive (100+ rules)
- 📄 **README.md** — Modern design with badges and clear sections

### Security & Best Practices
- 🔐 **Enhanced .env.example** — 200+ lines of documentation
- 🔐 **Clear warnings** — Trading risk disclaimer, security notes
- 🔐 **Credential protection** — Documented git-ignore rules

### Technical Debt Reduction
- ✅ Removed unused imports from key modules
- ✅ Added comprehensive class-level docstrings
- ✅ Consistent naming conventions across codebase
- ✅ Configuration-driven design (no hardcoded values in critical paths)

---

## [Z1 G2] — Previous Releases

(See earlier changelog entries below...)

---

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
