# ZeroBot G2 — Production Fix Changelog
## Version: G2_PRODUCTION_FIXED (March 2026)

### 🔴 CRITICAL FIXES (Production Blockers)

#### FIX-1: S-Mode Configuration Mismatch
- File: `config/settings.yaml`
- Problem: `bot.mode: "paper"` + `broker.name: "shoonya_paper"` caused inconsistent state
- Fix: Changed to `mode: "s_mode"` to properly activate S-Mode throughout the system

#### FIX-2: Shoonya Token Format (No Ticks Received)
- File: `data/feeds/shoonya_feed.py`
- Problem: Feed was subscribing to `NSE|RELIANCE` (symbol name) but Shoonya requires `NSE|2885` (token number)
- Fix: Added NSE token lookup table for all 30 symbols; broker-based resolution fallback

#### FIX-3: Thread Safety — Ticks Silently Dropped
- File: `data/feeds/shoonya_feed.py`
- Problem: `bus.publish()` is `async` but `_on_tick` runs in Shoonya's sync background thread → ticks silently lost
- Fix: Changed to `bus.publish_sync()` which uses `loop.create_task()` thread-safely

#### FIX-4: Feed Timeout — No Fallback on Session Expiry
- File: `data/feeds/shoonya_feed.py`
- Problem: If Shoonya WS connected but went silent (session expired mid-session), bot stayed in zombie WS state
- Fix: 2-minute tick watchdog — auto-switches to Yahoo Finance fallback if no ticks received

#### FIX-5: Hybrid Broker Blocking Event Loop
- File: `broker/hybrid_broker.py`
- Problem: `_try_connect_angel()` was synchronous, blocking asyncio for 5-10 seconds during startup
- Fix: Moved to background thread; broker connects in parallel with bot startup

#### FIX-6: Angel One TOTP Race Condition
- File: `broker/angel_one.py`
- Problem: If TOTP window was near expiry (28-30s), login failed with "Invalid OTP"
- Fix: Detects window boundary, waits 3s for fresh code; retries once on TOTP rejection

#### FIX-7: S-Mode Candle Refresh Using Yahoo
- File: `core/engine.py`
- Problem: `_candle_refresh_loop()` always used Yahoo Finance even in S-Mode with Shoonya connected
- Fix: Now tries Shoonya historical API first; Yahoo Finance as fallback

#### FIX-8: Yahoo Finance Rate Limiting
- File: `data/feeds/realtime_feed.py`
- Problem: Fixed 0.3s sleep between batches of 8 symbols caused burst requests hitting rate limit
- Fix: Batch size reduced to 5; random 0.8-1.8s jitter between batches

### 🟡 DASHBOARD IMPROVEMENTS

#### DASH-1: Feed Source Chip in Topbar
- Shows real-time data source: 📡 LIVE FEED (green) / ⚠ YAHOO (amber)
- Color-coded by source type

#### DASH-2: S-Mode Label in Mode Chip
- Mode chip now shows "S-MODE" with correct hybrid purple color
- Was showing "PAPER" even in S-Mode

#### DASH-3: Bot Activity Log Panel
- New live panel in Overview sidebar
- Shows: FILL, BLOCKED, RISK, NEWS, STOP, TARGET events in real-time
- Auto-refreshes every 6 seconds

#### DASH-4: Engine Internals Panel
- Shows: feed source, tick count, fallback status, symbols loaded, ML ready, Shoonya connection
- Auto-refreshes every 8 seconds

#### DASH-5: Strategy Signal Counts with Win Rates
- Strategy table now shows `12 (68% WR)` instead of plain `12`

#### DASH-6: Position Card Trade Rationale
- AI-generated rationale shown as blue stripe under each position card

#### DASH-7: New API Endpoints
- `GET /api/activity` — bot activity log (trades, signals, risk, news)
- `GET /api/engine/status` — live engine internals

### 🔑 ANGEL ONE ACTIVATION (New Account)

1. Go to https://smartapi.angelone.in → Login → My Apps → Create New App
2. Copy your API Key to `config/.env` as `ANGEL_API_KEY=`
3. Enable TOTP in Angel One mobile app → copy base32 secret to `ANGEL_TOTP_SECRET=`
4. Fill `ANGEL_CLIENT_ID=` (your Angel One login ID) and `ANGEL_MPIN=`
5. In Smart API portal: My Apps → Edit → Enable "Market Data Feed"
6. In `settings.yaml`: change `broker.name: "hybrid"` to use Angel One data + paper execution

### 📋 KNOWN REMAINING ISSUES

1. **Shoonya 502 errors** — Server-side issue, not code. Bot now gracefully falls back to Yahoo.
2. **NorenRestApiPy** — Must be installed: `pip install NorenRestApiPy pyotp`
3. **Signal history reset on restart** — Bus history is in-memory only; trade history is in SQLite DB
4. **Yahoo Finance 30-symbol limit** — Reduce to 15 symbols in settings.yaml if rate limits persist
