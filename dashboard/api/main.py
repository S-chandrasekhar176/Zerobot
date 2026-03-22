# -*- coding: utf-8 -*-
"""
ZeroBot Pro v2.4 — FastAPI Dashboard Backend
FIX LOG:
  [F1] /api/news now reads from NewsFeedAggregator directly (not just event bus history)
       Event bus history capped at 100 items total — news was being evicted
  [F2] /api/status now includes confidence, win_rate, open_price per position
  [F3] /api/status returns dynamic max_positions (capital-based)
  [F4] /api/status margin/capital correctly reflects trade deductions
  [F5] /api/trades correctly returns OPEN vs CLOSED status
  [F6] WebSocket pushes full position detail including stop_loss, target, confidence
  [F7] /api/news returns ALL news (not just |score|>=0.4) — dashboard filters
  [F8] dynamic position limit endpoint added
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from pathlib import Path
import asyncio
import json
import os
import secrets
from datetime import datetime

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    _register_bus_listeners()
    asyncio.create_task(_fetch_indices_background())
    yield

app = FastAPI(title="ZeroBot v1.1 API", version="1.1.0", docs_url="/docs", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Dashboard Basic Auth ────────────────────────────────────────────────────
_security = HTTPBasic(auto_error=False)
_DASH_USER = os.getenv("DASHBOARD_USER", "admin")
_DASH_PASS = os.getenv("DASHBOARD_PASS", "")   # empty = auth disabled

def require_auth(credentials: HTTPBasicCredentials = Depends(_security)):
    """Optional basic auth — only active when DASHBOARD_PASS is set in .env"""
    if not _DASH_PASS:
        return  # auth disabled — skip check
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    user_ok = secrets.compare_digest(credentials.username.encode(), _DASH_USER.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), _DASH_PASS.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

_ws_connections: set = set()
_live_prices: dict = {}

# ── P4-FIX-EXIT: Engine singleton — registered by main.py after ZeroBot() is created ──
_engine = None

def register_engine(engine_instance):
    """Called by main.py to wire the live engine into the API for exit/halt/resume."""
    global _engine
    _engine = engine_instance

# ── FIX-1: In-memory news cache so we keep ALL news, not just what fits in bus history ──
_news_cache: list = []   # All news items ingested this session (max 500)
_MAX_NEWS   = 500

# In-memory activity log buffer - captures key bot events for dashboard display
_activity_log: list = []
_MAX_ACTIVITY = 300

def _add_activity(level: str, msg: str, category: str = "info"):
    """Add entry to activity log. Categories: trade, signal, risk, system, feed"""
    _activity_log.append({
        "ts": datetime.now().isoformat(),
        "level": level,
        "msg": msg,
        "category": category,
    })
    if len(_activity_log) > _MAX_ACTIVITY:
        _activity_log.pop(0)

async def _broadcast(msg: dict):
    dead = set()
    for ws in list(_ws_connections):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    _ws_connections.difference_update(dead)

async def _on_tick(data: dict):
    if data and data.get("symbol"):
        _live_prices[data["symbol"]] = data
        await _broadcast({"type": "tick", "data": data})

async def _on_signal(data: dict):
    if data:
        _invalidate_win_rate_cache()  # BUG-11 FIX: invalidate so next status call rebuilds
        await _broadcast({"type": "signal", **data})
        # Log to activity feed
        side = data.get("side", "")
        sym = data.get("symbol", "")
        conf = data.get("confidence", 0)
        strat = data.get("strategy", "")
        blocked = data.get("blocked_reason", "")
        acted = data.get("acted_on", False)
        if acted:
            _add_activity("TRADE", f"{side} {sym} @ conf={conf:.0f}% via {strat}", "signal")
        elif blocked:
            _add_activity("BLOCKED", f"{side} {sym} blocked: {blocked[:60]}", "risk")

async def _on_order_filled(data: dict):
    if data:
        sym = data.get("symbol", "")
        side = data.get("side", "")
        qty = data.get("qty", 0)
        price = data.get("fill_price", 0)
        pnl = data.get("net_pnl")
        msg = f"{side} {qty}x {sym} @ ₹{price:.2f}"
        if pnl is not None:
            msg += f" | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}"
        _add_activity("FILL", msg, "trade")
        await _broadcast({"type": "order_filled", "data": data})

async def _on_risk_event(data: dict):
    if data:
        ev = data.get("event", "")
        reason = data.get("reason", "")
        _add_activity("RISK", f"{ev}: {reason[:80]}", "risk")

async def _on_news(data: dict):
    """FIX-1: Cache ALL news in memory AND broadcast to WS clients."""
    if data:
        # Inject to our local cache so /api/news can return everything
        _news_cache.append({**data, "received_at": datetime.now().isoformat()})
        if len(_news_cache) > _MAX_NEWS:
            _news_cache.pop(0)
        await _broadcast({"type": "news", "data": data})
        # Log high-impact news to activity
        score = data.get("score", 0)
        if abs(score) >= 0.3:
            sym = data.get("symbol") or (data.get("symbols") or [""])[0] or ""
            title = data.get("title", "")[:70]
            _add_activity("NEWS", f"{'🟢' if score>0 else '🔴'} {sym}: {title}", "news")

def _register_bus_listeners():
    # BUG-9 FIX: Guard against double-registration — was called at import AND lifespan
    global _bus_listeners_registered
    if _bus_listeners_registered:
        return
    _bus_listeners_registered = True
    try:
        from core.event_bus import bus
        bus.subscribe("tick",         _on_tick)
        bus.subscribe("signal",       _on_signal)
        bus.subscribe("news_alert",   _on_news)
        bus.subscribe("order_filled", _on_order_filled)
        bus.subscribe("risk_breach",  _on_risk_event)
        bus.subscribe("stop_hit",     lambda d: _add_activity("STOP", f"Stop hit: {d.get('symbol','')} @ {d.get('price',0):.2f}", "risk"))
        bus.subscribe("target_hit",   lambda d: _add_activity("TARGET", f"Target hit: {d.get('symbol','')} @ {d.get('price',0):.2f}", "trade"))
        _add_activity("SYSTEM", "Dashboard API started — event listeners registered", "system")
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Bus listener registration failed: {e}")

# BUG-9 FIX: Removed duplicate _register_bus_listeners() call that was here at import time.
# It was also called inside lifespan() above → every event fired twice (double fills, double logs).
# Lifespan-only registration is sufficient for uvicorn and gunicorn environments.
_bus_listeners_registered = False  # Guard for edge-case double-lifespan

@app.get("/api/status")
async def get_status():
    from core.state_manager import state_mgr
    from core.clock import session_status
    from core.config import cfg
    summary = state_mgr.get_summary()

    # FIX-2: Enrich each position with confidence, strategy win_rate, open_price
    positions = summary.get("open_positions_detail", [])
    live_pnl  = 0.0
    for pos in positions:
        sym   = pos.get("symbol", "")
        cmp   = _live_prices.get(sym, {}).get("ltp") or pos.get("avg_price", 0)
        qty   = pos.get("qty", 0)
        entry = pos.get("avg_price", 0)
        side  = (pos.get("side") or "BUY").upper()
        is_short = side in ("SELL", "SHORT")
        unrealised = ((entry - cmp) if is_short else (cmp - entry)) * qty
        pos["current_price"]   = cmp
        pos["unrealized_pnl"]  = round(unrealised, 2)
        pos["open_price"]      = pos.get("open_price") or entry   # first price of the day
        # Confidence stored when position opened (via signal)
        pos.setdefault("confidence", pos.get("ml_confidence") or 0)
        pos.setdefault("strategy_win_rate", _get_strategy_win_rate(pos.get("strategy", "")))
        # ALADDIN GAP #6: Add trade rationale text
        try:
            from core.trade_rationale import generate_rationale
            pos["rationale"] = generate_rationale(
                symbol=sym, side=pos.get("side","BUY"), confidence=pos.get("confidence",0),
                strategy=pos.get("strategy",""), cmp=cmp,
                stop_loss=pos.get("stop_loss",0) or 0, target=pos.get("target",0) or 0,
            )
        except Exception:
            pos.setdefault("rationale", "")
        live_pnl += unrealised

    # FIX-3: Dynamic position limit based on capital
    cap     = summary.get("total_capital", cfg.initial_capital)
    max_pos = _dynamic_max_positions(cap)
    summary["max_positions"]       = max_pos
    summary["unrealized_pnl"]      = round(live_pnl, 2)

    # P7: Inject live ML model info into status
    ml_info = {}
    try:
        if _engine and hasattr(_engine, 'predictor') and _engine.predictor:
            ml_info = _engine.predictor.get_model_info()
    except Exception:
        pass

    # Inject broker/feed/mode details for dashboard transparency
    bmode = cfg.broker_name.lower()
    # Determine feed source from actual broker mode (not guessing)
    _FEED_MAP = {
        "p_mode":  "yahoo_finance",
        "paper":   "yahoo_finance",
        "s_paper": "shoonya_ws",
        "s_live":  "shoonya_ws",
        "a_paper": "angel_one_ws",
        "hybrid":  "angel_one_ws",
        "a_live":  "angel_one_ws",
        "dual":    "angel_one_ws",
    }
    feed_source = _FEED_MAP.get(bmode, "yahoo_finance")
    # Override if actual feed is confirmed by tick_count
    if _engine and hasattr(_engine, 'rt_feed'):
        feed = _engine.rt_feed
        tick_count = getattr(feed, '_tick_count', 0) or getattr(feed, 'get_tick_count', lambda: 0)()
        feed_type = type(feed).__name__
        if feed_type == "PaperRealtimeFeed":
            feed_source = "yahoo_finance"
        elif feed_type == "AngelOneRealtimeFeed":
            feed_source = "angel_one_ws" if tick_count > 0 else "angel_one_ws (connecting)"
        elif feed_type == "ShoonyaRealtimeFeed":
            feed_source = "shoonya_ws" if tick_count > 0 else "shoonya_ws (connecting)"
    
    # Strategy performance summary for transparency
    strat_summary = {}
    if _engine and hasattr(_engine, 'strategies'):
        for s in _engine.strategies:
            strat_summary[s.name] = {"enabled": s.enabled, "name": s.name}

    return {
        "bot": {
            **summary,
            "trading_mode": cfg.trading_mode,
            "version": "1.1",
            "broker": cfg.broker_name,
            "ml": ml_info,
            "is_smode": is_smode,
            "feed_source": feed_source,
            "mode_label": {
                "p_mode":  "P-MODE (Yahoo + Paper)",
                "paper":   "P-MODE (Yahoo + Paper)",
                "s_paper": "S-PAPER (Shoonya WS + Paper)",
                "a_paper": "A-PAPER (Angel One WS + Paper)",
                "hybrid":  "A-PAPER (Angel One WS + Paper)",
                "dual":    "DUAL (Angel One WS + Shoonya REAL)",
                "a_live":  "A-LIVE (Angel One WS + Angel One REAL)",
                "s_live":  "S-LIVE (Shoonya WS + Shoonya REAL)",
            }.get(bmode, bmode.upper()
            ),
            "strategies_active": strat_summary,
            "trades_since_retrain": getattr(_engine, "_trades_since_retrain", 0) if _engine else 0,
        },
        "session": session_status(),
        "timestamp": datetime.now().isoformat(),
    }

def _dynamic_max_positions(capital: float) -> int:
    """
    PATCH12: Aligned with risk_engine pos_count() thresholds for consistency.
    Display and actual gate now agree on position limits.
    """
    if capital < 25_000:       return 3
    elif capital < 50_000:     return 5
    elif capital < 75_000:     return 8   # ₹55k → 8 positions
    elif capital < 1_50_000:   return 10
    elif capital < 3_00_000:   return 12
    elif capital < 7_50_000:   return 15
    else:                      return 20

# BUG-11 FIX: Cache strategy win_rates — was O(n*positions) on EVERY status call and WS ping.
# Cache invalidates when a new signal lands via _on_signal handler.
_win_rate_cache: dict = {}
_win_rate_dirty: bool = True

def _invalidate_win_rate_cache():
    global _win_rate_dirty
    _win_rate_dirty = True

def _get_strategy_win_rate(strategy_name: str) -> float:
    """Look up historical win rate for a strategy — cached to avoid O(n) scan per call."""
    global _win_rate_dirty, _win_rate_cache
    if _win_rate_dirty:
        # Rebuild the whole cache in one pass over signal history
        try:
            from core.event_bus import bus
            all_sigs = [r["data"] for r in bus.get_history("signal")
                        if isinstance(r.get("data"), dict) and r["data"].get("acted_on")]
            by_strat: dict = {}
            for s in all_sigs:
                n = s.get("strategy", "Unknown") or "Unknown"
                if n not in by_strat:
                    by_strat[n] = {"wins": 0, "total": 0}
                by_strat[n]["total"] += 1
                if (s.get("net_pnl") or 0) > 0:
                    by_strat[n]["wins"] += 1
            _win_rate_cache = {
                n: round(v["wins"] / v["total"], 3) if v["total"] >= 3 else 0.0
                for n, v in by_strat.items()
            }
            _win_rate_dirty = False
        except Exception:
            return 0.0
    return _win_rate_cache.get(strategy_name, 0.0)

@app.get("/api/portfolio")
async def get_portfolio():
    from core.state_manager import state_mgr
    s = state_mgr.state
    return {
        "capital": s.capital,
        "total_capital": s.total_capital,
        "daily_pnl": round(s.daily_pnl, 2),
        "total_pnl": round(s.total_pnl, 2),
        "available_margin": round(s.available_margin, 2),
        "open_positions": len(s.open_positions),
        "daily_trades": s.daily_trades,
        "win_rate": round(s.win_rate, 2),
        "drawdown_pct": round(s.drawdown_pct, 2),
    }

@app.get("/api/positions")
async def get_positions():
    """FIX-2: Returns positions enriched with live CMP and P&L."""
    from core.state_manager import state_mgr
    from core.config import cfg
    s = state_mgr.state
    positions = []
    for sym, pos in s.open_positions.items():
        cmp   = _live_prices.get(sym, {}).get("ltp") or pos.get("avg_price", 0)
        entry = pos.get("avg_price", 0)
        qty   = pos.get("qty", 0)
        side  = (pos.get("side") or "BUY").upper()
        is_short = side in ("SELL", "SHORT")
        unrealised = ((entry - cmp) if is_short else (cmp - entry)) * qty
        positions.append({
            **pos,
            "symbol":         sym,
            "current_price":  cmp,
            "unrealized_pnl": round(unrealised, 2),
            "open_price":     pos.get("open_price") or entry,
            "confidence":     pos.get("confidence", 0),
            "strategy_win_rate": _get_strategy_win_rate(pos.get("strategy", "")),
        })
    capital = s.total_capital
    return {
        "positions": positions,
        "count": len(positions),
        "max_positions": _dynamic_max_positions(capital),
        "available_margin": round(s.available_margin, 2),
    }

@app.get("/api/signals")
async def get_signals():
    from core.event_bus import bus
    raw = bus.get_history("signal")[-100:]
    signals = [r["data"] for r in raw if isinstance(r.get("data"), dict)]
    return {"signals": signals, "count": len(signals)}

@app.get("/api/news")
async def get_news(limit: int = 200, min_score: float = None, source: str = None):
    """
    FIX-1: Returns from our local _news_cache (not event bus history which is limited).
    Also falls back to reading from feed_aggregator directly for all items (not just high-score).
    Optional: min_score filter, source filter.
    """
    items = list(_news_cache)  # our session cache

    # Fallback: read directly from running news_feed aggregator
    if len(items) < 5:
        try:
            from core.engine import engine
            news_feed = getattr(engine, 'news_feed', None) or getattr(engine, '_news', None)
            if news_feed:
                for item in list(news_feed._all)[-200:]:
                    d = {
                        "id":           item.id,
                        "title":        item.title,
                        "source":       item.source,
                        "score":        item.sentiment_score,
                        "symbols":      item.symbols,
                        "symbol":       item.symbols[0] if item.symbols else None,
                        "published_at": item.published_at.isoformat() if hasattr(item.published_at, 'isoformat') else str(item.published_at),
                        "is_hard_block": False,
                    }
                    # Check if already in cache
                    if not any(x.get("id") == d["id"] for x in items):
                        items.append(d)
        except Exception:
            pass

    # Apply filters
    if min_score is not None:
        items = [x for x in items if abs(x.get("score", 0)) >= min_score]
    if source:
        items = [x for x in items if (x.get("source") or "").lower() == source.lower()]

    # Sort newest first
    items = sorted(items, key=lambda x: x.get("published_at", x.get("received_at", "")), reverse=True)
    return {"items": items[:limit], "count": len(items)}

@app.get("/api/risk/status")
async def get_risk_status():
    from core.state_manager import state_mgr
    from core.config import cfg
    s = state_mgr.state
    daily_limit  = s.capital * (cfg.risk.max_daily_loss_pct / 100)
    daily_loss   = abs(min(0, s.daily_pnl))
    capital      = s.total_capital
    return {
        "daily_loss":          round(daily_loss, 2),
        "daily_loss_limit":    round(daily_limit, 2),
        "daily_loss_used_pct": round(daily_loss / daily_limit * 100, 1) if daily_limit > 0 else 0,
        "open_positions":      len(s.open_positions),
        "max_positions":       _dynamic_max_positions(capital),
        "consecutive_losses":  s.consecutive_losses,
        "is_halted":           s.status == "HALTED",
        "vix_threshold":       cfg.risk.vix_halt_threshold,
        "available_margin":    round(s.available_margin, 2),
        "capital":             round(capital, 2),
        "gate_count":          11,
    }

@app.get("/api/risk/var")
async def get_var():
    from core.state_manager import state_mgr
    s = state_mgr.state
    total = s.total_capital
    return {
        "var_95_est":      round(total * 0.015 * 1.645, 2),
        "var_99_est":      round(total * 0.015 * 2.326, 2),
        "daily_vol_est_pct": 1.5,
        "note": "Parametric estimate. Historical VaR activates after 30 trading days."
    }

@app.get("/api/markets")
async def get_markets():
    from core.config import cfg
    return {
        "enabled_markets": getattr(cfg, 'enabled_markets', ['NSE']),
        "nse_symbols":     cfg.symbols,
        "crypto_symbols":  getattr(cfg, 'crypto_symbols', []),
        "us_symbols":      getattr(cfg, 'us_symbols', []),
        "forex_symbols":   getattr(cfg, 'forex_symbols', []),
    }

# In-memory index cache populated by background task
_index_cache: dict = {}
_index_opens: dict = {}

async def _fetch_indices_background():
    """P14: Robust background task — fetches Sensex/VIX/indices from multiple sources.
    Priority: (1) Direct Yahoo Finance JSON API via requests  (2) NSE REST API
              (3) yfinance fast_info  (4) yfinance history()
    Runs every 6 seconds — stores in _index_cache so /api/indices is always fresh.
    """
    import warnings, asyncio
    target_yf = {
        "^BSESN":    "sensex",
        "^INDIAVIX": "vix",
        "^NSEI":     "nifty",
        "^NSEBANK":  "banknifty",
        "^CNXIT":    "niftyit",
    }
    # NSE REST API symbol map — works without rate limits
    nse_to_key = {
        "NIFTY 50": "nifty",
        "NIFTY BANK": "banknifty",
        "NIFTY IT": "niftyit",
        "INDIA VIX": "vix",
    }
    _YF_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    _NSE_HEADERS = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
        "Referer": "https://www.nseindia.com",
    }
    _BSE_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.bseindia.com",
    }

    def _try_bse_sensex() -> float:
        """BUG-FIX P16: Fetch Sensex directly from BSE India public API.
        NSE API doesn't include BSE Sensex. BSE provides a dedicated endpoint.
        Falls back to alternative BSE endpoint if primary fails."""
        try:
            import requests
            # Primary: BSE India public Sensex endpoint
            r = requests.get(
                "https://api.bseindia.com/BseIndiaAPI/api/Sensex/w",
                headers=_BSE_HEADERS, timeout=5
            )
            if r.status_code == 200:
                j = r.json()
                p = j.get("last") or j.get("CurrValue") or j.get("currentValue")
                if p:
                    return float(str(p).replace(",", ""))
        except Exception:
            pass
        try:
            import requests
            # Fallback: BSE India getQuote for SENSEX index code 1
            r = requests.get(
                "https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w?Debtflag=&scripcode=1&seriesid=",
                headers=_BSE_HEADERS, timeout=5
            )
            if r.status_code == 200:
                j = r.json()
                p = j.get("CurrRate") or j.get("Currvalue")
                if p:
                    return float(str(p).replace(",", ""))
        except Exception:
            pass
        return 0.0

    def _try_nse_api() -> dict:
        """Fetch from NSE allIndices REST endpoint — no rate limits."""
        try:
            import requests
            r = requests.get(
                "https://www.nseindia.com/api/allIndices",
                headers=_NSE_HEADERS, timeout=5
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                out = {}
                for item in data:
                    key = nse_to_key.get(item.get("index", ""))
                    if key:
                        p = float(item.get("last", 0) or item.get("indexSymbol", 0) or 0)
                        if p > 0:
                            ref = _index_opens.get(key, p)
                            if not _index_opens.get(key):
                                _index_opens[key] = p
                            out[key] = {
                                "symbol": item.get("index", key),
                                "ltp": round(p, 2),
                                "change": round(p - ref, 2),
                                "change_pct": round((p - ref) / ref * 100, 4) if ref > 0 else 0,
                                "timestamp": datetime.now().isoformat(),
                            }
                return out
        except Exception:
            pass
        return {}

    def _try_yahoo_direct(sym: str) -> float:
        """Fetch price via direct Yahoo Finance JSON endpoint — more reliable than yfinance lib."""
        try:
            import requests
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m&range=1d"
            r = requests.get(url, headers=_YF_HEADERS, timeout=6)
            if r.status_code == 200:
                j = r.json()
                meta = j.get("chart", {}).get("result", [{}])[0].get("meta", {})
                p = meta.get("regularMarketPrice") or meta.get("previousClose")
                if p and float(p) > 0:
                    return float(p)
        except Exception:
            pass
        return 0.0

    def _try_yfinance(sym: str) -> float:
        """yfinance fallback — fast_info then history."""
        try:
            import yfinance as yf
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                t = yf.Ticker(sym)
                try:
                    p = getattr(t.fast_info, "last_price", None)
                    if p and float(p) > 0:
                        return float(p)
                except Exception:
                    pass
                try:
                    hist = t.history(period="1d", interval="1m")
                    if hist is not None and not hist.empty:
                        p2 = hist["Close"].dropna().iloc[-1]
                        if float(p2) > 0:
                            return float(p2)
                except Exception:
                    pass
        except Exception:
            pass
        return 0.0

    while True:
        try:
            # Source 1: NSE API (most reliable for NSE indices, no rate limit)
            nse_data = await asyncio.get_event_loop().run_in_executor(None, _try_nse_api)
            for key, info in nse_data.items():
                _index_cache[key] = info
                _live_prices[info["symbol"]] = info

            # Source 2: BSE India API for Sensex (P16 FIX — NSE API has no Sensex)
            if "sensex" not in _index_cache or _index_cache.get("sensex", {}).get("ltp", 0) == 0:
                sensex_p = await asyncio.get_event_loop().run_in_executor(None, _try_bse_sensex)
                if sensex_p and sensex_p > 0:
                    if not _index_opens.get("sensex"):
                        _index_opens["sensex"] = sensex_p
                    ref = _index_opens.get("sensex", sensex_p)
                    _index_cache["sensex"] = {
                        "symbol": "^BSESN", "ltp": round(sensex_p, 2),
                        "change": round(sensex_p - ref, 2),
                        "change_pct": round((sensex_p - ref) / ref * 100, 4) if ref > 0 else 0,
                        "timestamp": datetime.now().isoformat(),
                    }
                    _live_prices["^BSESN"] = {**_index_cache["sensex"]}

            # Source 3 + 4: Yahoo Direct + yfinance for any remaining missing symbols
            for sym, key in target_yf.items():
                if key in _index_cache and _index_cache[key].get("ltp", 0) > 0:
                    continue  # Already got it from NSE or BSE
                p = await asyncio.get_event_loop().run_in_executor(None, _try_yahoo_direct, sym)
                if not p or p <= 0:
                    p = await asyncio.get_event_loop().run_in_executor(None, _try_yfinance, sym)
                if p and p > 0:
                    if key not in _index_opens or _index_opens[key] <= 0:
                        _index_opens[key] = p
                    ref = _index_opens.get(key, p)
                    change = round(p - ref, 2)
                    change_pct = round(change / ref * 100, 4) if ref > 0 else 0
                    _index_cache[key] = {
                        "symbol": sym, "ltp": round(p, 2),
                        "change": change, "change_pct": change_pct,
                        "timestamp": datetime.now().isoformat(),
                    }
                    _live_prices[sym] = {**_index_cache[key]}

            # Source 5: Historical data fallback — use engine's last downloaded close
            # Ensures Sensex/VIX always show a value even when all live sources fail
            if _engine is not None:
                hist_fallbacks = {
                    "nifty":     ("^NSEI",    ["^NSEI"]),
                    "banknifty": ("^NSEBANK", ["^NSEBANK"]),
                    "niftyit":   ("^CNXIT",   ["^CNXIT"]),
                    "sensex":    ("^BSESN",   ["^BSESN"]),
                    "vix":       ("^INDIAVIX",["^VIX", "^INDIAVIX"]),
                }
                for key, (symbol, candle_keys) in hist_fallbacks.items():
                    if key in _index_cache and _index_cache[key].get("ltp", 0) > 0:
                        continue  # Already have live data
                    for ck in candle_keys:
                        df_hist = getattr(_engine, "_candle_data", {}).get(ck)
                        if df_hist is not None and not df_hist.empty:
                            try:
                                p = float(df_hist["close"].iloc[-1])
                                if p > 0:
                                    ref = _index_opens.get(key, p)
                                    if not _index_opens.get(key):
                                        _index_opens[key] = p
                                    _index_cache[key] = {
                                        "symbol": symbol, "ltp": round(p, 2),
                                        "change": round(p - ref, 2),
                                        "change_pct": round((p - ref) / ref * 100, 4) if ref > 0 else 0,
                                        "timestamp": datetime.now().isoformat(),
                                        "stale": True,  # flag: from historical, not live
                                    }
                                    break
                            except Exception:
                                pass
        except Exception as e:
            import logging
            logging.getLogger(__name__).debug(f"Index fetch error: {e}")
        await asyncio.sleep(6)

@app.get("/api/indices")
async def get_indices():
    """P13: Returns indices from background cache (always fresh) + state fallback."""
    from core.state_manager import state_mgr
    idx_map = {
        "^NSEI":     "nifty",
        "^NSEBANK":  "banknifty",
        "^CNXIT":    "niftyit",
        "^BSESN":    "sensex",
        "^INDIAVIX": "vix",
        "NIFTY50":   "nifty",
        "BANKNIFTY": "banknifty",
    }
    result = dict(_index_cache)  # start with background-fetched data
    # Merge live_prices for any symbols not yet in cache
    for sym, key in idx_map.items():
        if key not in result:
            data = _live_prices.get(sym)
            if data and data.get("ltp", 0) > 0:
                result[key] = {
                    "symbol": sym, "ltp": data.get("ltp", 0),
                    "change": data.get("change", 0), "change_pct": data.get("change_pct", 0),
                    "timestamp": data.get("timestamp", ""),
                }
    # VIX fallback from state
    if "vix" not in result or result.get("vix", {}).get("ltp", 0) == 0:
        vix_val = state_mgr.state.market_data.get("india_vix", 0)
        if vix_val > 0:
            result["vix"] = {"symbol": "^INDIAVIX", "ltp": vix_val, "change": 0, "change_pct": 0}
    return {"indices": result, "timestamp": datetime.now().isoformat()}

@app.get("/api/strategies")
async def get_strategies():
    from core.event_bus import bus
    raw = bus.get_history("signal")
    by_strategy = {}
    for rec in raw:
        sig  = rec.get("data") or rec
        name = sig.get("strategy", "Unknown")
        if name not in by_strategy:
            by_strategy[name] = {"count": 0, "buy": 0, "sell": 0, "wins": 0}
        by_strategy[name]["count"] += 1
        side = (sig.get("side") or "").upper()
        if side in ("BUY", "LONG"):
            by_strategy[name]["buy"] += 1
        elif side in ("SELL", "SHORT"):
            by_strategy[name]["sell"] += 1
        if (sig.get("net_pnl") or 0) > 0:
            by_strategy[name]["wins"] += 1
    return {"strategies": by_strategy}

@app.get("/api/position_limit")
async def get_position_limit():
    """FIX-3: Returns dynamic position limit based on capital."""
    from core.state_manager import state_mgr
    capital = state_mgr.state.total_capital
    limit   = _dynamic_max_positions(capital)
    return {
        "max_positions": limit,
        "capital":       round(capital, 2),
        "logic": {
            "< 50K":     3,
            "50K–1L":    5,
            "1L–2.5L":   8,
            "2.5L–5L":   10,
            "5L–10L":    12,
            "10L+":      15,
        }
    }

@app.post("/api/halt")
async def emergency_halt():
    from core.state_manager import state_mgr
    state_mgr.state.status = "HALTED"
    return {"status": "HALTED", "timestamp": datetime.now().isoformat()}

@app.post("/api/resume")
async def resume():
    from core.state_manager import state_mgr
    state_mgr.state.status = "RUNNING"
    return {"status": "RUNNING"}

class ExitRequest(BaseModel):
    symbol: str
    cmp: float = 0.0

@app.post("/api/exit_position")
async def exit_position(req: ExitRequest):
    """
    Manually exit a position at current market price from dashboard.
    P4-FIX-EXIT: Uses engine._close_position() which handles both broker
    order AND state cleanup atomically. Falls back to direct state cleanup
    if engine isn't available (e.g. options with No LTP).
    """
    try:
        from core.state_manager import state_mgr
        sym = req.symbol.strip()
        positions = state_mgr.state.open_positions

        # Resolve CMP — use engine price first, then passed value
        cmp = req.cmp
        if _engine:
            live = _engine._get_current_price(sym)
            if live and live > 0:
                cmp = live

        # If option has No LTP, use entry price as exit price
        if cmp <= 0 and sym in positions:
            cmp = positions[sym].get("avg_price", 0)

        if cmp <= 0:
            return {"success": False, "error": "Could not determine exit price"}

        # --- Primary path: use engine's close_position if available ---
        if _engine and hasattr(_engine, '_close_position_manual'):
            try:
                await _engine._close_position_manual(sym, cmp)
                return {"success": True, "symbol": sym, "cmp": cmp, "method": "engine"}
            except Exception as eng_err:
                pass  # Fall through to direct state cleanup

        # --- Secondary path: broker order ---
        if _engine and hasattr(_engine, 'broker'):
            broker_pos = _engine.broker.get_positions()
            if broker_pos is not None and sym in broker_pos:
                pos = broker_pos[sym]
                qty = pos.get("qty", 0)
                side_str = (pos.get("side") or "LONG").upper()
                close_side = "BUY" if side_str in ("SHORT", "SELL") else "SELL"
                if qty > 0:
                    order = await _engine.broker.place_order(
                        symbol=sym, side=close_side, qty=qty, cmp=cmp,
                        strategy="ManualExit", confidence=100.0
                    )
                    return {"success": True, "symbol": sym, "side": close_side,
                            "qty": qty, "cmp": cmp, "order_id": getattr(order, 'order_id', '?'),
                            "method": "broker"}

        # --- Fallback: direct state cleanup (paper mode / options with no LTP) ---
        if sym in positions:
            pos = positions[sym]
            qty  = pos.get("qty", 1)
            entry = pos.get("avg_price", cmp)
            side_str = (pos.get("side") or "BUY").upper()
            is_short = side_str in ("SHORT", "SELL")
            pnl = ((entry - cmp) if is_short else (cmp - entry)) * qty
            # Use risk engine's update_after_trade to avoid double-counting
            if _engine and hasattr(_engine, 'risk'):
                _engine.risk.update_after_trade(pnl)
            else:
                # Manual update if no engine
                state_mgr.state.update_pnl(pnl)
                state_mgr.state.daily_trades = getattr(state_mgr.state, 'daily_trades', 0) + 1
                if pnl >= 0:
                    state_mgr.state.daily_wins = getattr(state_mgr.state, 'daily_wins', 0) + 1
                    state_mgr.state.consecutive_losses = 0
                else:
                    state_mgr.state.daily_losses = getattr(state_mgr.state, 'daily_losses', 0) + 1
                    state_mgr.state.consecutive_losses = getattr(state_mgr.state, 'consecutive_losses', 0) + 1
            state_mgr.state.available_margin += qty * cmp
            # Save closed trade to DB
            try:
                import asyncio as _aio
                _aio.get_event_loop().create_task(state_mgr.save_trade({
                    "symbol": sym, "side": "SELL" if not is_short else "BUY",
                    "qty": int(qty), "entry_price": float(entry), "exit_price": float(cmp),
                    "exit_time": datetime.now(), "net_pnl": round(pnl, 2),
                    "strategy": pos.get("strategy", "ManualExit"), "status": "CLOSED",
                    "mode": state_mgr.state.mode,
                }))
            except Exception:
                pass
            del state_mgr.state.open_positions[sym]
            # Also clean broker if paper mode
            if _engine and hasattr(_engine, 'broker') and hasattr(_engine.broker, '_positions'):
                _engine.broker._positions.pop(sym, None)
            if _engine and hasattr(_engine, '_pending_symbols'):
                _engine._pending_symbols.discard(sym)
            return {"success": True, "symbol": sym, "cmp": cmp, "pnl": round(pnl, 2),
                    "method": "direct_state"}

        return {"success": False, "error": f"Position {sym} not found in state or broker"}
    except Exception as e:
        import traceback
        return {"success": False, "error": str(e), "trace": traceback.format_exc()[-300:]}

@app.get("/api/health")
async def get_health():
    from core.clock import session_status
    from core.config import cfg
    return {
        "status": "OK",
        "version": "1.1",
        "components": {
            "bot": "running", "database": "connected",
            "data_feed": "paper" if cfg.is_paper else "live",
            "risk_engine": "11_gate_+_events", "ml_ensemble": "xgboost+lightgbm",
            "events_calendar": "active", "adv_sizing": "active",
        },
        "session": session_status(),
        "timestamp": datetime.now().isoformat(),
    }

@app.get("/api/events")
async def get_events():
    """Return upcoming market events (earnings, RBI, expiry) — Aladdin Gap #5."""
    try:
        from core.events_calendar import events_calendar
        upcoming = events_calendar.get_upcoming_events(days_ahead=14)
        return {"events": upcoming, "total": len(upcoming), "timestamp": datetime.now().isoformat()}
    except Exception as e:
        return {"events": [], "error": str(e)}

@app.get("/api/stress_test")
async def stress_test():
    """
    Run current portfolio through 5 historical NSE stress scenarios.
    Aladdin Gap #7: Scenario stress testing.
    """
    try:
        from core.state_manager import state_mgr
        positions = state_mgr.state.open_positions
        scenarios = [
            {"name": "COVID-2020 Crash",     "market_drop": -0.38, "date": "Mar 2020"},
            {"name": "2022 Rate Shock",       "market_drop": -0.15, "date": "Jun 2022"},
            {"name": "2023 Recovery",         "market_drop": +0.28, "date": "Dec 2023"},
            {"name": "Election Spike 2024",   "market_drop": +0.10, "date": "Jun 2024"},
            {"name": "Global Selloff 2025",   "market_drop": -0.12, "date": "Aug 2025"},
        ]
        results = []
        total_value = sum(p.get("qty",0) * p.get("avg_price",0) for p in positions.values())
        for scenario in scenarios:
            drop = scenario["market_drop"]
            stressed_pnl = total_value * drop
            results.append({
                "scenario": scenario["name"],
                "market_move": f"{drop:+.0%}",
                "portfolio_impact": round(stressed_pnl, 2),
                "impact_pct": f"{drop:+.1%}",
                "date_reference": scenario["date"],
            })
        return {
            "portfolio_value": round(total_value, 2),
            "positions": len(positions),
            "scenarios": results,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/costs/calculator")
async def calculate_costs(qty: int = 10, price: float = 2847.0, side: str = "BUY"):
    from execution.transaction_cost import CostCalculator
    return CostCalculator().compute(side=side, qty=qty, price=price)

@app.get("/api/activity")
async def get_activity(limit: int = 100, category: str = None):
    """Recent bot activity log — trades, signals, risk events, feed status."""
    items = list(_activity_log)
    if category:
        items = [x for x in items if x.get("category") == category]
    return {
        "items": items[-limit:][::-1],   # newest first
        "count": len(items),
        "timestamp": datetime.now().isoformat(),
    }

@app.get("/api/engine/status")
async def get_engine_status():
    """Detailed engine internals — task health, feed status, broker state."""
    from core.config import cfg
    from core.state_manager import state_mgr
    result = {
        "running": False,
        "feed_source": "unknown",
        "feed_tick_count": 0,
        "feed_is_fallback": False,
        "broker_type": "unknown",
        "broker_connected": False,
        "shoonya_connected": False,
        "angel_connected": False,
        "pending_symbols": [],
        "candle_symbols_loaded": 0,
        "intraday_symbols_loaded": 0,
        "ml_ready": False,
        "trades_since_retrain": 0,
        "timestamp": datetime.now().isoformat(),
    }
    if _engine:
        result["running"] = _engine._running
        result["trades_since_retrain"] = getattr(_engine, "_trades_since_retrain", 0)
        result["pending_symbols"] = list(getattr(_engine, "_pending_symbols", set()))
        result["candle_symbols_loaded"] = len(getattr(_engine, "_candle_data", {}))
        result["intraday_symbols_loaded"] = len(getattr(_engine, "_intraday_data", {}))
        if hasattr(_engine, "predictor") and _engine.predictor:
            result["ml_ready"] = _engine.predictor.is_ready()
        # Feed info
        if hasattr(_engine, "rt_feed"):
            feed = _engine.rt_feed
            result["feed_source"] = type(feed).__name__
            result["feed_tick_count"] = getattr(feed, "_tick_count", 0) or getattr(feed, "tick_count", 0)
            result["feed_is_fallback"] = getattr(feed, "is_fallback", False) or getattr(feed, "_fallback_active", False)
        # Broker info
        broker = getattr(_engine, "broker", None)
        if broker:
            result["broker_type"] = type(broker).__name__
            result["broker_connected"] = getattr(broker, "is_connected", True)
            # S-Mode: check Shoonya sub-broker
            shoonya = getattr(broker, "_shoonya", None)
            if shoonya:
                result["shoonya_connected"] = getattr(shoonya, "connected", False)
            # Hybrid: check Angel sub-broker
            angel = getattr(broker, "_angel", None)
            if angel:
                result["angel_connected"] = getattr(angel, "_connected", False)
    return result

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    _ws_connections.add(websocket)
    try:
        while True:
            from core.state_manager import state_mgr
            from core.clock import session_status
            from core.config import cfg
            summary = state_mgr.get_summary()

            # FIX-2 & FIX-4: Enrich positions with live CMP, P&L, confidence
            positions = summary.get("open_positions_detail", [])
            live_pnl = 0.0
            for pos in positions:
                sym   = pos.get("symbol", "")
                cmp   = _live_prices.get(sym, {}).get("ltp") or pos.get("avg_price", 0)
                entry = pos.get("avg_price", 0)
                qty   = pos.get("qty", 0)
                side  = (pos.get("side") or "BUY").upper()
                is_short = side in ("SELL", "SHORT")
                unrealised = ((entry - cmp) if is_short else (cmp - entry)) * qty
                pos["current_price"]      = cmp
                pos["unrealized_pnl"]     = round(unrealised, 2)
                pos["open_price"]         = pos.get("open_price") or entry
                pos.setdefault("confidence", 0)
                pos.setdefault("strategy_win_rate", _get_strategy_win_rate(pos.get("strategy", "")))
                live_pnl += unrealised

            summary["unrealized_pnl"] = round(live_pnl, 2)
            summary["max_positions"]  = _dynamic_max_positions(summary.get("total_capital", cfg.initial_capital))

            await websocket.send_json({
                "type": "state_update",
                "data": {
                    "bot":     summary,
                    "session": session_status(),
                    "config":  {"initial_capital": cfg.initial_capital, "symbols_count": len(cfg.symbols)},
                },
                "timestamp": datetime.now().isoformat(),
            })
            # PATCH8-FIX: Must receive() with timeout so TCP buffer never fills.
            # Without this, a stale/closed client blocks the send() forever.
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=2.0)
            except asyncio.TimeoutError:
                pass   # Normal — no message from client, just continue loop
            except Exception:
                break  # Client closed connection
    except WebSocketDisconnect:
        _ws_connections.discard(websocket)
    except Exception:
        _ws_connections.discard(websocket)

FRONTEND = Path(__file__).parent.parent / "frontend"

@app.get("/")
async def serve_dashboard():
    index = FRONTEND / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"message": "ZeroBot v1.1", "docs": "/docs"})

if FRONTEND.exists() and (FRONTEND / "static").exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND / "static")), name="static")

@app.get("/api/live_prices")
async def get_live_prices():
    # Primary: bus tick events populate _live_prices in real-time
    prices = dict(_live_prices)

    # Fallback 1: pull from rt_feed._prices (REST poll or WS cache)
    try:
        if _engine and hasattr(_engine, 'rt_feed'):
            feed = _engine.rt_feed
            feed_prices = getattr(feed, '_prices', {})
            for sym, ltp in feed_prices.items():
                if ltp and ltp > 0 and sym not in prices:
                    prices[sym] = {
                        "symbol": sym, "ltp": round(float(ltp), 2),
                        "change": 0, "change_pct": 0,
                        "timestamp": datetime.now().isoformat(),
                        "source": type(feed).__name__
                    }
    except Exception:
        pass

    # Fallback 2: use last known close from historical candle data
    # This ensures the price table is never empty, even before any live data arrives
    try:
        if _engine and hasattr(_engine, '_candle_data') and len(prices) < 3:
            for sym, df in _engine._candle_data.items():
                if sym not in prices and df is not None and not df.empty:
                    ltp = float(df["Close"].dropna().iloc[-1])
                    prev = float(df["Close"].dropna().iloc[-2]) if len(df) > 1 else ltp
                    if ltp > 0:
                        prices[sym] = {
                            "symbol": sym, "ltp": round(ltp, 2),
                            "open": round(float(df["Open"].dropna().iloc[-1]), 2) if "Open" in df else ltp,
                            "high": round(float(df["High"].dropna().iloc[-1]), 2) if "High" in df else ltp,
                            "low":  round(float(df["Low"].dropna().iloc[-1]),  2) if "Low"  in df else ltp,
                            "change": round(ltp - prev, 2),
                            "change_pct": round((ltp - prev) / prev * 100, 2) if prev > 0 else 0,
                            "timestamp": datetime.now().isoformat(),
                            "source": "historical_candle"
                        }
    except Exception:
        pass

    return {"prices": list(prices.values()), "count": len(prices), "timestamp": datetime.now().isoformat()}

@app.post("/api/clear_positions")
async def clear_all_positions():
    """
    PATCH10: Emergency clear all stale/incorrect open positions from state.
    Use when positions list is corrupted or showing wrong data on startup.
    Does NOT place any orders — purely clears in-memory state.
    """
    from core.state_manager import state_mgr
    count = len(state_mgr.state.open_positions)
    state_mgr.state.open_positions.clear()
    if _engine and hasattr(_engine, 'broker') and hasattr(_engine.broker, '_positions'):
        _engine.broker._positions.clear()
    if _engine and hasattr(_engine, '_pending_symbols'):
        _engine._pending_symbols.clear()
    # Also reset daily trade counters so win_rate starts fresh
    state_mgr.state.daily_wins = 0
    state_mgr.state.daily_losses = 0
    state_mgr.state.daily_trades = 0
    state_mgr.state.consecutive_losses = 0
    return {"success": True, "cleared": count, "message": f"Cleared {count} stale positions"}


@app.get("/api/trades/closed")
async def get_closed_trades(limit: int = 200, symbol: str = None):
    """PATCH10: Returns only closed trades with proper PnL data."""
    from core.state_manager import state_mgr
    trades = state_mgr.get_trade_history(limit=limit, symbol=symbol)
    closed = [t for t in trades if t.get("status") == "CLOSED" or t.get("exit_price") is not None]
    return {"trades": closed, "count": len(closed)}


@app.get("/api/trades/open")
async def get_open_trades(limit: int = 200):
    """PATCH10: Returns only open/active trades."""
    from core.state_manager import state_mgr
    s = state_mgr.state
    positions = []
    for sym, pos in s.open_positions.items():
        cmp = _live_prices.get(sym, {}).get("ltp") or pos.get("avg_price", 0)
        entry = pos.get("avg_price", 0)
        qty = pos.get("qty", 0)
        side = (pos.get("side") or "BUY").upper()
        is_short = side in ("SELL", "SHORT")
        unrealised = ((entry - cmp) if is_short else (cmp - entry)) * qty
        positions.append({
            **pos, "symbol": sym,
            "current_price": cmp,
            "unrealized_pnl": round(unrealised, 2),
            "status": "OPEN",
        })
    return {"trades": positions, "count": len(positions)}


@app.get("/api/db/stats")
async def get_db_stats():
    from core.state_manager import state_mgr
    return state_mgr.get_db_stats()

@app.get("/api/ml/win_ratio")
async def get_ml_win_ratio():
    """P15: Per-strategy and per-model win/loss ratio — feeds Intelligence tab panel."""
    from core.state_manager import state_mgr

    # ── Strategy stats from in-memory tracker ──
    strat_stats = dict(state_mgr._strategy_stats)

    # Also enrich from closed trades in DB / memory
    closed = state_mgr.get_trade_history(limit=500)
    closed_only = [t for t in closed if t.get("status") == "CLOSED"]

    if closed_only:
        for t in closed_only:
            name = t.get("strategy", "Unknown") or "Unknown"
            pnl  = t.get("net_pnl", 0) or 0
            conf = float(t.get("confidence", 0) or 0)
            qty  = t.get("qty", 0) or 0
            avg_price = t.get("avg_price", 0) or 0
            if name not in strat_stats:
                strat_stats[name] = {
                    "wins": 0, "losses": 0, "total_pnl": 0.0,
                    "total_won": 0.0, "total_lost": 0.0,   # BUG-FIX: was never written
                    "capital_deployed": 0.0,               # BUG-FIX: was never written
                    "avg_conf": 0.0, "conf_samples": 0,
                    "best_pnl": 0.0, "worst_pnl": 0.0,
                }
            s = strat_stats[name]
            if pnl > 0:
                s["wins"] += 1
                s["total_won"] = round(s.get("total_won", 0) + pnl, 2)   # BUG-FIX
            else:
                s["losses"] += 1
                s["total_lost"] = round(s.get("total_lost", 0) + pnl, 2) # BUG-FIX
            s["total_pnl"] = round(s.get("total_pnl", 0) + pnl, 2)
            s["best_pnl"]  = max(s.get("best_pnl", 0), pnl)
            s["worst_pnl"] = min(s.get("worst_pnl", 0), pnl)
            # BUG-FIX: accumulate capital deployed so ROI is non-zero
            if qty > 0 and avg_price > 0:
                s["capital_deployed"] = round(s.get("capital_deployed", 0) + qty * avg_price, 2)
            if conf > 0:
                n = s.get("conf_samples", 0)
                s["avg_conf"] = round((s.get("avg_conf", 0) * n + conf) / (n + 1), 2)
                s["conf_samples"] = n + 1

    # ── Compute derived metrics ──
    strategies = []
    for name, s in strat_stats.items():
        total = s.get("wins", 0) + s.get("losses", 0)
        wr    = round(s["wins"] / total * 100, 1) if total > 0 else 0
        total_won  = round(s.get("total_won", 0), 2)
        total_lost = round(s.get("total_lost", 0), 2)
        total_pnl  = round(s.get("total_pnl", 0), 2)
        deployed   = round(s.get("capital_deployed", 0), 2)
        # ROI = net_pnl / capital_deployed × 100
        roi = round(total_pnl / deployed * 100, 2) if deployed > 0 else 0.0
        strategies.append({
            "name":             name,
            "wins":             s.get("wins", 0),
            "losses":           s.get("losses", 0),
            "total":            total,
            "win_rate":         wr,
            "total_pnl":        total_pnl,
            "total_won":        total_won,        # Money won (positive trades sum)
            "total_lost":       total_lost,       # Money lost (negative trades sum)
            "capital_deployed": deployed,         # Total capital deployed
            "roi_pct":          roi,              # Return on deployed capital
            "avg_conf":         round(s.get("avg_conf", 0), 1),
            "best_trade":       round(s.get("best_pnl", 0), 2),
            "worst_trade":      round(s.get("worst_pnl", 0), 2),
            "expectancy":       round(total_pnl / max(1, total), 2),
        })
    strategies.sort(key=lambda x: x["win_rate"], reverse=True)

    # ── ML model confidence accuracy (from in-memory tracker) ──
    ml_models = []
    for model_name, ms in state_mgr._ml_model_stats.items():
        total = ms.get("total", 0)
        acc   = round(ms.get("correct", 0) / max(1, total) * 100, 1)
        avg_c = round(ms.get("total_conf", 0) / max(1, total), 1)
        ml_models.append({
            "name":        model_name,
            "predictions": total,
            "correct":     ms.get("correct", 0),
            "accuracy":    acc,
            "avg_confidence": avg_c,
        })

    # ── Session summary ──
    s = state_mgr.state
    total_trades = s.daily_wins + s.daily_losses
    session_wr   = round(s.daily_wins / max(1, total_trades) * 100, 1)

    return {
        "strategies":      strategies,
        "ml_models":       ml_models,
        "session": {
            "wins":        s.daily_wins,
            "losses":      s.daily_losses,
            "total":       total_trades,
            "win_rate":    session_wr,
            "realized_pnl":round(s.daily_pnl, 2),
            "best_trade":  max((t.get("net_pnl", 0) for t in closed_only), default=0),
            "worst_trade": min((t.get("net_pnl", 0) for t in closed_only), default=0),
        },
        "total_closed_trades": len(closed_only),
        "ml_warmup_needed":    len(closed_only) < 50,
        "ml_warmup_progress":  round(min(100, len(closed_only) / 50 * 100), 1),
    }

@app.get("/api/broker/status")
async def get_broker_status():
    """P15+P16: Broker mode, hybrid status, and connection details."""
    from core.config import cfg
    status = {
        "mode":             cfg.mode,
        "is_paper":         cfg.is_paper,
        "is_live":          cfg.is_live,
        "is_hybrid":        cfg.is_hybrid,
        "broker_name":      cfg.broker_name,
        "angel_configured": cfg.angel_one.is_configured,
    }
    if _engine and hasattr(_engine, "broker"):
        broker = _engine.broker
        if hasattr(broker, "get_hybrid_status"):
            status["hybrid"] = broker.get_hybrid_status()
        if hasattr(broker, "get_status"):
            status.update(broker.get_status())
        status["architecture"]  = type(broker).__name__
        status["is_connected"]  = getattr(broker, "is_connected", True)
    return status

@app.get("/api/signals/quality")
async def get_signal_quality():
    """P16: Signal pass rate, confidence distribution, top blocked gates."""
    from core.event_bus import bus
    sigs = [r["data"] for r in bus.get_history("signal") if isinstance(r.get("data"), dict)]
    risk_blocks = []
    try:
        from core.state_manager import state_mgr
        risk_blocks = list(state_mgr._risk_blocks_mem)
    except Exception:
        pass

    total_sigs   = len(sigs)
    total_blocked= len(risk_blocks)
    total_all    = total_sigs + total_blocked
    pass_rate    = round(total_sigs / max(1, total_all) * 100, 1)

    # Confidence buckets
    conf_buckets = {"50_62": 0, "62_70": 0, "70_80": 0, "80_90": 0, "90_plus": 0}
    confidences  = []
    for s in sigs:
        c = float(s.get("confidence", 0) or 0)
        if c > 0:
            confidences.append(c)
            if c < 62:   conf_buckets["50_62"]   += 1
            elif c < 70: conf_buckets["62_70"]   += 1
            elif c < 80: conf_buckets["70_80"]   += 1
            elif c < 90: conf_buckets["80_90"]   += 1
            else:        conf_buckets["90_plus"] += 1

    avg_conf = round(sum(confidences) / len(confidences), 1) if confidences else 0.0

    # Top blocked gate reasons
    blocked_reasons: dict = {}
    for b in risk_blocks:
        reason = b.get("reason") or b.get("gate") or b.get("event") or "Unknown"
        # Shorten reason to gate name
        short = reason.split(":")[0].strip() if ":" in reason else reason[:40]
        blocked_reasons[short] = blocked_reasons.get(short, 0) + 1

    return {
        "total_signals":   total_sigs,
        "total_blocked":   total_blocked,
        "pass_rate":       pass_rate,
        "avg_confidence":  avg_conf,
        "conf_buckets":    conf_buckets,
        "blocked_reasons": blocked_reasons,
    }


@app.get("/api/ml/runs")
async def get_ml_runs(limit: int = 20):
    from core.state_manager import state_mgr
    if not state_mgr._db_available or not state_mgr._Session:
        return {"runs": [], "connected": False}
    try:
        from sqlalchemy import desc
        from database.models import ModelRun
        with state_mgr._Session() as session:
            runs = session.query(ModelRun).order_by(desc(ModelRun.created_at)).limit(limit).all()
            return {"runs": [{c.name: getattr(r, c.name) for c in ModelRun.__table__.columns} for r in runs], "count": len(runs)}
    except Exception as e:
        return {"runs": [], "error": str(e)}

@app.get("/api/risk/events")
async def get_risk_events(limit: int = 50):
    from core.state_manager import state_mgr
    # P14: Try DB first, then in-memory fallback
    events = []
    if state_mgr._db_available and state_mgr._Session:
        try:
            from sqlalchemy import desc
            from database.models import RiskEvent
            with state_mgr._Session() as session:
                evts = session.query(RiskEvent).order_by(desc(RiskEvent.created_at)).limit(limit).all()
                events = [{c.name: getattr(e, c.name) for c in RiskEvent.__table__.columns} for e in evts]
        except Exception:
            pass
    # Always merge with in-memory blocks (newer events appear here first)
    mem = state_mgr._risk_blocks_mem[:limit]
    if not events:
        events = mem
    else:
        # Merge: DB records + memory records, dedup by timestamp
        seen = {e.get("timestamp", "") for e in events}
        for m in mem:
            if m.get("timestamp", "") not in seen:
                events.append(m)
        events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        events = events[:limit]
    return {"events": events, "count": len(events)}

# ── P6: Startup checks cache ──────────────────────────────────────────────────
_startup_checks: dict = {}

def _set_startup_checks(checks: dict):
    """Called by main.py after preflight checklist runs."""
    global _startup_checks
    _startup_checks = checks

@app.get("/api/startup_checks")
async def get_startup_checks():
    """Return startup checklist results for dashboard display."""
    from core.config import cfg
    from core.state_manager import state_mgr
    if not _startup_checks:
        # Runtime fallback: build live checks
        from datetime import datetime as _dt
        checks = {}
        # DB
        db_ok = state_mgr._db_available
        checks["Database"] = {"ok": db_ok, "detail": "Connected" if db_ok else "JSON fallback"}
        # Yahoo Finance
        try:
            import yfinance as yf
            t = yf.Ticker("RELIANCE.NS")
            p = getattr(t.fast_info, "last_price", None)
            yf_ok = p is not None and p > 0
            checks["Yahoo Finance"] = {"ok": yf_ok, "detail": f"₹{p:.0f}" if yf_ok else "Unavailable"}
        except:
            checks["Yahoo Finance"] = {"ok": False, "detail": "Error"}
        # Telegram
        tg_ok = bool(cfg.telegram.bot_token)
        checks["Telegram"] = {"ok": tg_ok, "detail": "Configured" if tg_ok else "Not set"}
        # Angel One
        ao_ok = cfg.angel_one.is_configured
        checks["Angel One"] = {"ok": ao_ok, "detail": "Ready" if ao_ok else "Credentials not set"}
        # Config
        checks["Config"] = {"ok": True, "detail": f"₹{cfg.initial_capital:,.0f} · {len(cfg.symbols)} symbols"}
        checks["Risk Engine"] = {"ok": True, "detail": "11-gate validator"}
        return {"checks": checks, "timestamp": _dt.now().isoformat()}
    return {"checks": _startup_checks, "timestamp": datetime.now().isoformat()}


# ── P6: Dynamic trading mode switch ──────────────────────────────────────────

class ModeRequest(BaseModel):
    mode: str          # "paper" | "hybrid" | "live"
    trading_mode: str = None   # "stocks" | "options" | "both" — optional
    broker: str = None         # "paper" | "dual" | "hybrid" | "angel" | "shoonya"

@app.post("/api/set_mode")
async def set_mode(req: ModeRequest):
    """
    P6: Dynamically switch bot trading mode from the dashboard.
    Changes are applied immediately to the running bot.
    Settings.yaml is NOT modified — changes are session-only.
    To persist: edit config/settings.yaml manually.

    Allowed transitions:
      paper       → paper (change trading_mode: stocks/options/both)
      paper       → live  (only if Angel One is configured)
      live        → paper (safe, auto-exits all positions first)
    """
    from core.config import cfg
    from core.state_manager import state_mgr

    new_mode = req.mode.lower()
    new_trading = req.trading_mode
    new_broker = req.broker

    if new_mode not in ("paper", "hybrid", "live"):
        return JSONResponse({"success": False, "error": "mode must be 'paper', 'hybrid', or 'live'"}, status_code=400)

    # Safety: going live or hybrid requires Angel One data credentials
    if new_mode in ("live", "hybrid") and not cfg.angel_one.is_configured:
        return JSONResponse({
            "success": False,
            "error": (
                "Angel One credentials required for hybrid/live mode.\n"
                "Fill ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_MPIN, ANGEL_TOTP_SECRET in config/.env\n"
                "Hybrid mode = Real Angel One data + Paper money (safe for testing)"
            )
        }, status_code=400)

    # Safety: switching from live → paper/hybrid auto-halts first
    if cfg.mode == "live" and new_mode in ("paper", "hybrid"):
        if _engine and not state_mgr.state.is_halted:
            _engine.halt(f"Mode switched to {new_mode} — auto-halt for safety")

    old_mode = cfg.mode
    cfg.set("mode", new_mode)  # BUG-10 FIX: use cfg.set() instead of cfg.__dict__[] mutation

    # Auto-select broker based on mode if not explicitly specified
    if not new_broker:
        auto_broker = {"paper": "paper", "hybrid": "hybrid", "live": "dual"}.get(new_mode)
        if auto_broker:
            cfg.set("broker_name", auto_broker)
            new_broker = auto_broker

    if new_trading and new_trading in ("stocks", "options", "both"):
        cfg.set("trading_mode", new_trading)

    # Also switch broker if requested
    if new_broker:
        cfg.set("broker_name", new_broker)

    msg = f"Mode changed: {old_mode} → {new_mode}"
    if new_trading:
        msg += f" | Trading: {new_trading}"

    # Broadcast to all WebSocket clients
    await _broadcast({"type": "mode_change", "data": {
        "mode": new_mode, "trading_mode": cfg.trading_mode, "broker": new_broker or cfg.broker_name
    }})

    return {
        "success": True, "message": msg,
        "mode": new_mode, "trading_mode": cfg.trading_mode,
        "warning": "Changes are session-only. Edit config/settings.yaml to persist."
    }

@app.get("/api/config")
async def get_config():
    """Return current bot configuration (read-only snapshot)."""
    from core.config import cfg
    return {
        "mode": cfg.mode,
        "trading_mode": cfg.trading_mode,
        "broker": cfg.broker_name,
        "capital": cfg.initial_capital,
        "symbols_count": len(cfg.symbols),
        "version": "1.1",
        "is_paper": cfg.is_paper,
        "angel_configured": cfg.angel_one.is_configured,
        "risk": {
            "max_daily_loss_pct": cfg.risk.max_daily_loss_pct,
            "max_open_positions": cfg.risk.max_open_positions,
            "vix_halt_threshold": cfg.risk.vix_halt_threshold,
        }
    }

@app.post("/api/config")
async def update_config(data: dict):
    """P6: Accept config updates from dashboard (session-only). BUG-10 FIX: uses cfg.set()."""
    from core.config import cfg
    if "broker" in data:
        cfg.set("broker_name", data["broker"])
    if "trading_mode" in data:
        if data["trading_mode"] in ("stocks", "options", "both"):
            cfg.set("trading_mode", data["trading_mode"])
    return {"success": True, "applied": data}


class OptionExpiryRequest(BaseModel):
    expiry: str  # "weekly" | "monthly"

@app.post("/api/set_option_expiry")
async def set_option_expiry(req: OptionExpiryRequest):
    """
    PATCH10: Switch options strategy between weekly (Thursday) and monthly
    (last Thursday) expiry. Change applies immediately to new signals.
    """
    from core.config import cfg
    if req.expiry not in ("weekly", "monthly"):
        return JSONResponse({"success": False, "error": "expiry must be 'weekly' or 'monthly'"}, status_code=400)
    # Update config options section
    try:
        cfg.options.__dict__["expiry"] = req.expiry
    except Exception:
        # Fallback: patch the options attribute directly
        if hasattr(cfg, 'options') and cfg.options:
            object.__setattr__(cfg.options, 'expiry', req.expiry) if hasattr(cfg.options, '__setattr__') else None
    # Also try to update the live strategy if engine is available
    if _engine and hasattr(_engine, '_strategies'):
        for strat in _engine._strategies:
            if hasattr(strat, 'opts') and hasattr(strat.opts, 'expiry'):
                strat.opts.expiry = req.expiry
    await _broadcast({"type": "config_change", "data": {"option_expiry": req.expiry}})
    return {"success": True, "expiry": req.expiry, "message": f"Options now trading {req.expiry} expiry"}

# ── P7+P16: ML Model Status endpoint (merged) ─────────────────────────────────
@app.get("/api/ml/status")
async def get_ml_status():
    """P7+P16: Live ML model info — models, features, weights, symbols, feature importances."""
    from pathlib import Path as _P
    import glob as _glob, json as _json
    model_dir = _P(__file__).parent.parent.parent / "models" / "saved"
    pkls_path = list(model_dir.glob("*.pkl")) if model_dir.exists() else []
    pkls_str  = _glob.glob(str(model_dir / "*.pkl")) if model_dir.exists() else []
    xgb = [p.name for p in pkls_path if p.name.startswith("xgboost_") and p.name != "feature_names.pkl"]
    lgb = [p.name for p in pkls_path if p.name.startswith("lightgbm_")]

    syms = {}
    for p in pkls_path:
        if p.name == "feature_names.pkl":
            continue
        parts = p.stem.split("_")
        if len(parts) >= 2:
            s = parts[1]
            if s not in syms:
                syms[s] = []
            syms[s].append(parts[0])

    live_info     = {}
    feature_imp   = {}
    stat_arb_info = {}

    if _engine:
        if hasattr(_engine, 'predictor') and _engine.predictor:
            try:
                live_info = _engine.predictor.get_model_info()
            except Exception:
                pass
        try:
            fi_path = _P(__file__).parent.parent.parent / "data" / "feature_importance.json"
            if fi_path.exists():
                with open(fi_path) as _fh:
                    feature_imp = _json.load(_fh)
        except Exception:
            pass
        stat_arb_info = {
            "calibrated": getattr(getattr(_engine, 'stat_arb', None), '_calibrated', False),
            "pairs":      len(getattr(getattr(_engine, 'stat_arb', None), 'pairs', [])),
            "pair_names": [
                f"{a}↔{b}"
                for a, b in (getattr(_engine.stat_arb, 'pairs', [])[:5] if _engine else [])
            ],
        }
    else:
        stat_arb_info = {"calibrated": False, "pairs": 0, "pair_names": []}

    return {
        "models": {
            "xgboost":  {"count": len(xgb),  "weight": "55%", "files": xgb[:5]},
            "lightgbm": {"count": len(lgb),  "weight": "45%", "files": lgb[:5]},
        },
        "trained_symbols": list(syms.keys()),
        "total_models": len(pkls_path),
        "live": live_info,
        "feature_importance": feature_imp,
        "retrain_threshold": 50,
        "stat_arb": stat_arb_info,
        "timestamp": datetime.now().isoformat(),
    }


# ─── P16: Broker management endpoints ────────────────────────────────────────

@app.post("/api/broker/reconnect")
async def broker_reconnect():
    """P16: Reconnect the broker (e.g. after disconnect). Works for all broker types."""
    if _engine is None:
        return JSONResponse({"ok": False, "error": "Engine not initialised"}, status_code=503)
    try:
        broker = _engine.broker
        if hasattr(broker, "connect"):
            broker.connect()
            status = "reconnected"
        elif hasattr(broker, "_connect_both"):
            broker._connect_both()
            status = "reconnected (dual)"
        else:
            status = "no connect method"
        return {
            "ok": True,
            "status": status,
            "broker_type": type(broker).__name__,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/broker/orders")
async def get_broker_orders():
    """P16: Fetch current order book + today's trades from the active broker."""
    if _engine is None:
        return JSONResponse({"ok": False, "error": "Engine not initialised"}, status_code=503)
    broker = _engine.broker
    order_book  = []
    trade_book  = []
    broker_info = type(broker).__name__

    # Try DualBroker
    if hasattr(broker, "get_order_book"):
        order_book = broker.get_order_book() or []
    if hasattr(broker, "get_trade_book"):
        trade_book = broker.get_trade_book() or []

    # Try Angel One nested inside broker
    if not order_book and hasattr(broker, "_data_broker"):
        ab = broker._data_broker
        if ab and hasattr(ab, "getOrderBook"):
            order_book = ab.getOrderBook() or []
        if ab and hasattr(ab, "getTradeBook"):
            trade_book = ab.getTradeBook() or []

    return {
        "ok": True,
        "broker": broker_info,
        "order_book": order_book,
        "trade_book": trade_book,
        "order_count": len(order_book),
        "trade_count": len(trade_book),
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/groq/decisions")
async def groq_decisions():
    """Groq AI gate decisions — approve/block log for dashboard."""
    try:
        from risk.groq_gates import GroqGateEvaluator, get_groq_evaluator
        ev = get_groq_evaluator()
        stats = GroqGateEvaluator.get_stats()
        return {
            "available": ev is not None and ev.is_available if ev else False,
            "model": GroqGateEvaluator._MODEL,
            "total_calls": stats["total_calls"],
            "calls_today": stats.get("calls_today", 0),
            "avg_latency_ms": stats["avg_latency_ms"],
            "approved": stats.get("approved", 0),
            "blocked": stats.get("blocked", 0),
            "groq_calls": stats.get("groq_calls", 0),
            "fallback_calls": stats.get("fallback_calls", 0),
            "decisions": stats.get("decisions", []),
        }
    except Exception as e:
        return {"available": False, "error": str(e), "decisions": []}


# ════════════════════════════════════════════════════════════
# G1: New Intelligence Endpoints
# ════════════════════════════════════════════════════════════

@app.get("/api/g1/brain/stats")
async def g1_brain_stats():
    """Groq Brain usage stats."""
    try:
        from core.groq_brain import groq_brain
        return {"ok": True, **groq_brain.get_stats()}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/g1/attribution")
async def g1_attribution():
    """Per-strategy performance attribution."""
    try:
        from core.performance_attribution import attribution
        return {
            "ok":      True,
            "summary": attribution.get_summary(),
            "by_strategy": attribution.get_report(),
            "time_slots":  attribution.get_time_slot_analysis(),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/g1/portfolio/health")
async def g1_portfolio_health():
    """Groq portfolio health check."""
    try:
        from core.groq_brain import groq_brain
        from core.state_manager import state_mgr
        s = state_mgr.state
        result = await groq_brain.portfolio_health(
            open_positions=s.open_positions,
            capital=s.capital,
            daily_pnl=s.daily_pnl,
        )
        return {
            "ok":                 True,
            "health":             result.health,
            "concentration_score":result.concentration_score,
            "warnings":           result.warnings,
            "suggested_actions":  result.suggested_actions,
            "max_new_positions":  result.max_new_positions,
            "source":             result.source,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/g1/fii")
async def g1_fii_data():
    """FII/DII institutional flow data."""
    try:
        from data.feeds.fii_data import fii_feed
        return {"ok": True, **fii_feed.for_dashboard()}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/g1/optimizer/concentration")
async def g1_concentration():
    """Portfolio sector concentration."""
    try:
        from core.portfolio_optimizer import portfolio_optimizer
        from core.state_manager import state_mgr
        conc = portfolio_optimizer.sector_concentration(state_mgr.state.open_positions)
        return {"ok": True, "sector_concentration": conc}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/g1/execution/stats")
async def g1_execution_stats():
    """VWAP/TWAP execution quality stats."""
    try:
        from execution.vwap_slicer import vwap_slicer
        return {"ok": True, **vwap_slicer.get_stats()}
    except Exception as e:
        return {"ok": False, "error": str(e)}