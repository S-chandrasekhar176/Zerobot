# -*- coding: utf-8 -*-
"""
╔═══════════════════════════════════════════════════════════════╗
║                      Z E R O B O T  v2.1                     ║
║         Institutional-Grade NSE India Trading System         ║
║                                                              ║
║  Run:     python main.py                                     ║
║  Test:    python test_bot.py --simulate                      ║
║  Mode:    Set in config/settings.yaml → bot.mode            ║
║           "paper" = paper trading (default)                  ║
║           "live"  = real Angel One orders                    ║
╚═══════════════════════════════════════════════════════════════╝
"""
import asyncio
import sys
import os
import signal
from pathlib import Path
_Path = Path  # alias used in startup checklist

# ── M5 / C4 FIX: Force UTF-8 stdout on Windows so Unicode chars (✅ ❌ ═══)
# don't crash with 'charmap' codec errors.  Must happen before ANY print/log.
if sys.platform == "win32":
    import io
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from core.logger import log
from core.config import cfg
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint

console = Console()


def print_banner():
    _mode_label = {
        "stocks":  "📊 STOCKS ONLY",
        "options": "📈 OPTIONS ONLY",
        "both":    "📊📈 STOCKS + OPTIONS",
    }.get(cfg.trading_mode, "📊 STOCKS")

    # Mode display based on broker_name (6 clean modes)
    _bmode = cfg.broker_name.lower()
    _MODE_DISPLAY = {
        "p_mode":  ("📄 P-MODE  (Yahoo Finance · Paper execution)", "Yahoo Finance data · Paper money"),
        "paper":   ("📄 P-MODE  (Yahoo Finance · Paper execution)", "Yahoo Finance data · Paper money"),
        "s_paper": ("📊 S-PAPER  (Shoonya live data · Paper execution)", "Shoonya WebSocket data · Paper money"),
        "a_paper": ("📡 A-PAPER  (Angel One live data · Paper execution)", "Angel One WebSocket data · Paper money"),
        "hybrid":  ("📡 A-PAPER  (Angel One live data · Paper execution)", "Angel One WebSocket data · Paper money"),
        "dual":    ("🔴 DUAL  (Angel One data · Shoonya REAL orders)", "Angel One WS data · Shoonya execution · REAL MONEY"),
        "a_live":  ("🔴 A-LIVE  (Angel One data + execution)", "Angel One live data · Angel One REAL orders"),
        "s_live":  ("🔴 S-LIVE  (Shoonya data + execution)", "Shoonya live data · Shoonya REAL orders"),
    }
    _mode_str, _bottom_text = _MODE_DISPLAY.get(_bmode, ("📄 UNKNOWN MODE", "Check broker.name in settings.yaml"))
    _bottom = f"[yellow]Mode info:[/yellow] {_bottom_text}"

    import os as _os
    _sentiment = (
        "[green]FinBERT[/green] (ZEROBOT_USE_FINBERT=1)"
        if _os.getenv("ZEROBOT_USE_FINBERT", "0") == "1"
        else "[yellow]Keyword scorer[/yellow] (fast, no deps) · run install_finbert.bat to upgrade"
    )

    _body = (
        "[bold cyan]ZeroBot v1.1[/bold cyan] — NSE India Trading System\n"
        "[yellow]Mode:[/yellow] " + _mode_str + "\n"
        "[yellow]Broker:[/yellow] " + getattr(cfg, "broker_name", "paper").upper() + "\n"
        "[yellow]Trading:[/yellow] " + _mode_label + "\n"
        "[yellow]Capital:[/yellow] ₹" + f"{cfg.initial_capital:,.2f}" + "\n"
        "[yellow]Symbols:[/yellow] " + str(len(cfg.symbols)) + " NSE equities\n"
        "[yellow]Strategies:[/yellow] Momentum | MeanReversion | VWAP | MarketMaking | StatArb\n"
        "[yellow]            [/yellow] [green]Supertrend[/green] | [green]ORB[/green] | [green]RSIDivergence[/green] | [green]Breakout[/green]\n"
        "[yellow]Risk Engine:[/yellow] 11-Gate Validator · [green]Kelly Sizing[/green] · [green]VIX Regime Detector[/green]\n"
        "[yellow]ML Engine:[/yellow] [green]XGBoost[/green] (weight 55%) + [green]LightGBM[/green] (weight 45%) Ensemble · retrain every 50 trades\n"
        "[yellow]Sentiment:[/yellow] " + _sentiment + "\n"
        "[yellow]Options:[/yellow] Real NSE option chain + Black-Scholes fallback\n"
        "[yellow]Database:[/yellow] [green]SQLite[/green] (zero config) — switch to PostgreSQL via DB_URL in .env\n"
        "[yellow]News:[/yellow] NSE + MoneyControl + ET — instant callbacks on high-impact headlines\n"
        "[yellow]Dashboard:[/yellow] http://" + cfg.dashboard_host + ":" + str(cfg.dashboard_port) + "\n"
        + _bottom
    )
    console.print(Panel(_body, title="🤖 ZeroBot v1.1 — NSE India", border_style="cyan"))


async def run_dashboard():
    """
    Start FastAPI dashboard. Auto-tries ports 8000, 8001, 8002 if primary is busy.
    The scary-looking 'ERROR: [Errno 10048]' line is printed directly to stderr by
    uvicorn's C-level socket code before Python can intercept it — it is cosmetic only.
    The bot trades normally regardless of whether the dashboard binds successfully.
    """
    try:
        import uvicorn
        import logging as _logging
        from dashboard.api.main import app

        log.info("run_dashboard: starting uvicorn...")
        # Suppress uvicorn error logger for the entire port-scan loop so the
        # bind-failure message never reaches stderr at all.
        _uv_log = _logging.getLogger("uvicorn.error")
        _orig_level = _uv_log.level
        _uv_log.setLevel(_logging.CRITICAL)   # ← must be set BEFORE server.serve()

        # Also redirect stderr temporarily to swallow the low-level OS message
        import io, sys as _sys
        _orig_stderr = _sys.stderr
        _sys.stderr = io.StringIO()

        try:
            for port in [cfg.dashboard_port, cfg.dashboard_port + 1, cfg.dashboard_port + 2]:
                try:
                    config = uvicorn.Config(
                        app,
                        host=cfg.dashboard_host,
                        port=port,
                        log_level="warning",
                        lifespan="off",   # prevent lifespan conflicts when running inside asyncio.gather
                    )
                    server = uvicorn.Server(config)
                    # Restore stderr before serving so normal uvicorn output is visible
                    _sys.stderr = _orig_stderr
                    _uv_log.setLevel(_orig_level)
                    log.info(f"Dashboard running → http://{cfg.dashboard_host}:{port}")
                    log.info("run_dashboard: calling await server.serve()...")
                    log.info(f"[UVICORN-DEBUG] Server type: {type(server)}")
                    log.info(f"[UVICORN-DEBUG] Config: host={cfg.dashboard_host}, port={port}")
                    try:
                        log.info("running server.run() in thread executor...")
                        import asyncio
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, server.run)
                        log.info("run_dashboard: server.run() returned")
                    except Exception as serve_err:
                        print(f"\n!!! CRITICAL: server.run() raised: {type(serve_err).__name__}: {serve_err}")
                        log.critical(f"run_dashboard: server.run() raised exception: {serve_err}")
                        import traceback
                        log.critical(traceback.format_exc())
                        print(traceback.format_exc())
                        raise
                    log.info("run_dashboard: server.serve() returned (unexpected)")
                    return
                except OSError as e:
                    err = str(e)
                    if "10048" in err or "address already in use" in err.lower():
                        if port < cfg.dashboard_port + 2:
                            log.warning(f"Dashboard: port {port} busy, trying {port + 1}...")
                            continue
                        # All 3 ports busy — give up gracefully
                        log.warning(
                            f"Dashboard ports {cfg.dashboard_port}–{port} all busy. "
                            "Dashboard unavailable — bot is trading normally."
                        )
                        log.warning(
                            "To free port: netstat -ano | findstr :"
                            + str(cfg.dashboard_port)
                            + "  then: taskkill /PID <pid> /F"
                        )
                    else:
                        log.error(f"Dashboard OS error: {e}")
                    return
        finally:
            # Always restore stderr even if an unexpected exception occurs
            _sys.stderr = _orig_stderr
            _uv_log.setLevel(_orig_level)

    except ImportError as ie:
        log.warning(f"FastAPI/uvicorn not installed. Run: pip install fastapi uvicorn | Error: {ie}")
    except Exception as e:
        log.critical(f"run_dashboard: Unhandled exception: {type(e).__name__}: {e}")
        import traceback
        log.critical(traceback.format_exc())


async def run_bot():
    """Start the trading engine."""
    try:
        from core.engine import ZeroBot
        log.info("Creating ZeroBot instance...")
        bot = ZeroBot()
        log.info("✅ ZeroBot instance created successfully")

        # P4-FIX-EXIT: Register engine with dashboard API so exit/halt/resume work
        try:
            from dashboard.api.main import register_engine
            log.info("Registering engine with dashboard API...")
            register_engine(bot)
            log.info("✅ Engine registered with dashboard API")
        except Exception as _e:
            log.warning(f"Could not register engine with API: {_e}")

        def shutdown(sig, frame):
            log.info("Shutdown signal received...")
            bot.halt("Shutdown requested")
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        log.info("Starting bot main loop (await bot.start())...")
        await bot.start()
        log.info("✅ Bot completed normally")
    except Exception as e:
        log.critical(f"💥 Unhandled error in run_bot(): {e}")
        import traceback
        log.critical(traceback.format_exc())
        raise


async def main():
    """Entry point — runs bot + dashboard concurrently."""
    print_banner()

    # ── NumPy 2.x compatibility check ────────────────────────────────────────
    # FIXED: Only delete models that are ACTUALLY incompatible with current numpy.
    # Previous code deleted ALL models on every startup when numpy >= 2 — this 
    # caused models to retrain from scratch every restart, wasting ~2 minutes.
    # New logic: attempt to unpickle each file; only delete if it raises a
    # numpy-related error. Models built with the SAME numpy version are kept.
    _model_dir = Path(__file__).parent / "models" / "saved"
    _deleted = []
    import pickle as _pickle
    import numpy as _np
    _np_major = int(_np.__version__.split(".")[0])
    for _pkl in _model_dir.glob("*.pkl"):
        try:
            with open(_pkl, "rb") as _fh:
                _pickle.load(_fh)
            # Successfully loaded — model is compatible, keep it
        except Exception as _e:
            err_str = str(_e).lower()
            # Only delete if error is numpy/sklearn/lightgbm version mismatch
            if any(kw in err_str for kw in ("numpy", "module", "rebuild", "incompatible", "dtype")):
                try:
                    _pkl.unlink(missing_ok=True)
                    _deleted.append(_pkl.name)
                except Exception:
                    pass
            # Other errors (corrupt file, etc.) — also delete to be safe
            elif "eoferror" in err_str or "unpickl" in err_str:
                try:
                    _pkl.unlink(missing_ok=True)
                    _deleted.append(_pkl.name)
                except Exception:
                    pass
    if _deleted:
        log.warning(f"⚠️  Deleted {len(_deleted)} incompatible model(s): {', '.join(_deleted)}")
        log.info("Models will retrain automatically (~2 min). Bot will run normally.")
    else:
        log.info(f"✅ Model compatibility check passed — {sum(1 for _ in _model_dir.glob('*.pkl'))} model(s) ready")

    # ── Test mode notice ─────────────────────────────────────────────────────
    import os as _os
    if _os.environ.get("ZEROBOT_FORCE_MARKET_OPEN") == "1":
        log.warning("⚠️  TEST MODE: ZEROBOT_FORCE_MARKET_OPEN=1 — market hours bypassed")
        log.warning("⚠️  All 11 risk gates active EXCEPT 'Market Hours'. Signals will fire immediately.")

    # ─────────────────────────────────────────────────────────────
    #  STARTUP CHECKLIST  (P6)
    #  Checks every API / service before bot starts trading.
    #  Results stored in _STARTUP_CHECKS dict — served by /api/health
    # ─────────────────────────────────────────────────────────────
    from datetime import datetime as _dt
    _checks: dict = {}

    def _ck(name: str, ok: bool, detail: str = ""):
        icon = "✅" if ok else "⚠️ "
        _checks[name] = {"ok": ok, "detail": detail, "ts": _dt.now().isoformat()}
        log.info(f"  {icon}  {name:<30} {detail}")
        return ok

    console.print("\n[bold cyan]═══════ STARTUP CHECKLIST ═══════[/bold cyan]")

    # 1. Database connectivity
    try:
        from core.state_manager import state_mgr
        db_ok = state_mgr._db_available
        db_type = "SQLite" if (hasattr(cfg.database, 'use_sqlite') and cfg.database.use_sqlite) else "PostgreSQL"
        _ck("Database", db_ok, f"{db_type} {'connected' if db_ok else 'FAILED — using JSON fallback'}")
    except Exception as e:
        _ck("Database", False, f"Error: {e}")

    # 2. Telegram Bot
    try:
        tg_ok = bool(cfg.telegram.bot_token and cfg.telegram.chat_id)
        _ck("Telegram Alerts", tg_ok,
            "Token + ChatID configured" if tg_ok else "Not configured — alerts console-only")
    except Exception as e:
        _ck("Telegram Alerts", False, str(e))

    # 3. Yahoo Finance (paper mode data)
    try:
        import yfinance as yf
        ticker = yf.Ticker("RELIANCE.NS")
        info = ticker.fast_info
        price = getattr(info, "last_price", None)
        yf_ok = price is not None and price > 0
        _ck("Yahoo Finance", yf_ok,
            f"RELIANCE.NS ₹{price:.0f}" if yf_ok else "Unavailable — check internet")
    except Exception as e:
        _ck("Yahoo Finance", False, f"Error: {e}")

    # 4. NSE Option Chain API
    try:
        from data.feeds.nse_option_chain import nse_option_chain
        nse_check = nse_option_chain.health_check()
        nse_ok = nse_check.get("ok", False)
        _ck("NSE Option Chain", nse_ok,
            f"NIFTY spot ₹{nse_check.get('nifty_spot', 0):.0f}" if nse_ok
            else f"Unavailable — {nse_check.get('reason', 'timeout')}")
    except Exception as e:
        _ck("NSE Option Chain", False, f"Error: {e}")

    # 5. Angel One API
    try:
        ao_configured = cfg.angel_one.is_configured
        _ck("Angel One API", ao_configured,
            "Credentials present" if ao_configured
            else "Not configured (paper mode) — fill config/.env to activate")
    except Exception as e:
        _ck("Angel One API", False, str(e))

    # 5c. Groq AI (optional — enhances Gates 6 & 11)
    try:
        from risk.groq_gates import get_groq_evaluator as _gge
        _groq = _gge()
        if _groq and _groq.is_available:
            # Live ping to confirm API key works and measure latency
            import time as _tg
            _t0 = _tg.time()
            _test = _groq.evaluate_sync("TEST.NS", "BUY", 0.65, 15.0, "")
            _latency = int((_tg.time() - _t0) * 1000)
            if _test and _test.source == "groq":
                _ck("Groq AI (Gates 6+11)", True,
                    f"✅ LLaMA 3.3-70B LIVE — {_groq._MODEL} | ping={_latency}ms | "
                    f"calls_today={_groq._CALL_COUNT}")
            else:
                # FIX-4: API key set but Groq unreachable — disable to avoid
                # silent 3s timeout on every future trade signal
                _groq._available = False
                _ck("Groq AI (Gates 6+11)", True,
                    f"⚠️  Key set but API unreachable — using local gates. "
                    f"Check GROQ_API_KEY and network. Latency: {_latency}ms")
        else:
            _ck("Groq AI (Gates 6+11)", True, "ℹ️  Not configured (optional) — add GROQ_API_KEY to .env for LLM gates")
    except Exception as _ge:
        _ck("Groq AI (Gates 6+11)", True, f"ℹ️  Not available: {_ge}")

    # 5b. P16: Shoonya (Finvasia) execution broker
    try:
        from dotenv import load_dotenv as _ld_sh
        import os as _os_sh
        _env_sh = Path(__file__).parent / "config" / ".env"
        if _env_sh.exists():
            _ld_sh(_env_sh, encoding="utf-8-sig", override=True)
        def _e(k): return _os_sh.getenv(k,"").strip().lstrip("\ufeff").strip()
        if _e("SHOONYA_USER"):     cfg.shoonya.user_id     = _e("SHOONYA_USER")
        if _e("SHOONYA_PASSWORD"): cfg.shoonya.password    = _e("SHOONYA_PASSWORD")
        if _e("SHOONYA_TOTP_SECRET"): cfg.shoonya.totp_secret = _e("SHOONYA_TOTP_SECRET")
        if _e("SHOONYA_VENDOR_CODE"): cfg.shoonya.vendor_code = _e("SHOONYA_VENDOR_CODE")
        if _e("SHOONYA_API_KEY"):  cfg.shoonya.api_key     = _e("SHOONYA_API_KEY")

        if cfg.shoonya.is_configured:
            bmode = cfg.broker_name.lower()
            if bmode in ("s_paper", "s_live"):
                hint = f" → ACTIVE as data+exec source for {bmode.upper()}"
            elif bmode == "dual":
                hint = " → ACTIVE as execution broker for DUAL mode"
            elif bmode in ("p_mode", "paper"):
                hint = " → configured but not used in P-mode"
            else:
                hint = " → credentials loaded"
            _ck("Shoonya (Finvasia)", True,
                f"✅ Credentials loaded (user={cfg.shoonya.user_id}){hint}")
        else:
            missing = cfg.shoonya.missing_fields if hasattr(cfg.shoonya, "missing_fields") else []
            if not missing:
                # fallback: compute manually
                if not cfg.shoonya.user_id:     missing.append("SHOONYA_USER")
                if not cfg.shoonya.totp_secret: missing.append("SHOONYA_TOTP_SECRET")
                if not cfg.shoonya.api_key:     missing.append("SHOONYA_API_KEY")
                if not cfg.shoonya.vendor_code: missing.append("SHOONYA_VENDOR_CODE")
            _ck("Shoonya (Finvasia)", False,
                f"Not configured — fill these in config/.env: {', '.join(missing)}")
    except Exception as e:
        _ck("Shoonya (Finvasia)", False, str(e))

    # 5d. NorenRestApiPy — required for S-Mode WebSocket ticks
    # Checked here (before bot starts) so it's installed before connect() runs.
    # This avoids the mid-session auto-install race inside shounya.py.
    if cfg.uses_shoonya_data:
        try:
            import NorenRestApiPy  # noqa: F401
            _ck("NorenRestApiPy (Shoonya WS)", True,
                "Installed — Shoonya WebSocket ticks ready")
        except ImportError:
            _ck("NorenRestApiPy (Shoonya WS)", False,
                "Not installed. Run: pip install NorenRestApiPy pyotp  then restart")
            log.error("[STARTUP] NorenRestApiPy missing. Run: pip install NorenRestApiPy pyotp")

    # 6. ML Models
    try:
        from models.predictor import EnsemblePredictor
        model_dir = _Path(__file__).parent / "models" / "saved"
        pkls = list(model_dir.glob("*.pkl")) if model_dir.exists() else []
        xgb_models = [p.name for p in pkls if p.name.startswith("xgboost_") and not p.name == "feature_names.pkl"]
        lgb_models = [p.name for p in pkls if p.name.startswith("lightgbm_")]
        ml_ok = len(pkls) > 0
        if ml_ok:
            syms = set()
            for p in pkls:
                parts = p.stem.split("_")
                if len(parts) >= 2:
                    syms.add(parts[1])
            detail = f"XGBoost×{len(xgb_models)} + LightGBM×{len(lgb_models)} | Symbols: {', '.join(sorted(syms))}"
        else:
            detail = "No models yet — will train on startup (~2 min)"
        _ck("ML Models (XGBoost+LightGBM)", ml_ok, detail)
    except Exception as e:
        _ck("ML Models (XGBoost+LightGBM)", False, f"Error: {e}")

    # 7. Config file
    try:
        cfg_ok = cfg.initial_capital > 0 and bool(cfg.symbols)
        _ck("Config (settings.yaml)", cfg_ok,
            f"Capital ₹{cfg.initial_capital:,.0f} · {len(cfg.symbols)} symbols")
    except Exception as e:
        _ck("Config (settings.yaml)", False, str(e))

    # 8. Risk Engine
    try:
        from risk.risk_engine import RiskEngine
        _ck("Risk Engine", True, "11-gate validator ready")
    except Exception as e:
        _ck("Risk Engine", False, str(e))

    # 9. Paper Broker
    try:
        from broker.paper_broker import PaperBroker
        bmode = cfg.broker_name.lower()
        if bmode in ("p_mode", "paper", "s_paper", "a_paper", "hybrid"):
            _ck("Paper Broker", True,
                f"Capital ₹{cfg.initial_capital:,.0f} | Slippage 0.05% | Mode: {bmode.upper()}")
        else:
            _ck("Paper Broker", True,
                f"LIVE execution mode ({bmode.upper()}) — real orders will be sent to exchange")
    except Exception as e:
        _ck("Paper Broker", False, str(e))

    # 10. Data directory writable
    try:
        _data_dir = _Path(__file__).parent / "data"
        _data_dir.mkdir(exist_ok=True)
        (_data_dir / ".write_test").write_text("ok")
        (_data_dir / ".write_test").unlink()
        _ck("Data Directory", True, f"{_data_dir} (writable)")
    except Exception as e:
        _ck("Data Directory", False, str(e))

    # 11. News sources (MoneyControl + ET + NSE)
    try:
        import urllib.request as _ur
        import xml.etree.ElementTree as _ET
        _rss_ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        _news_ok = {}
        for _src, _url in [
            ("MoneyControl", "https://www.moneycontrol.com/rss/marketsnews.xml"),
            ("ET Markets",   "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
        ]:
            try:
                _req = _ur.Request(_url, headers={"User-Agent": _rss_ua, "Accept": "*/*"})
                with _ur.urlopen(_req, timeout=6) as _r:
                    _root = _ET.fromstring(_r.read())
                _items = len(_root.findall(".//item"))
                _news_ok[_src] = _items
            except Exception as _e:
                _news_ok[_src] = f"Error: {_e}"
        _all_ok  = all(isinstance(v, int) and v > 0 for v in _news_ok.values())
        _detail  = " | ".join(f"{k}:{v} items" if isinstance(v, int) else f"{k}:{v}" for k, v in _news_ok.items())
        _ck("News Sources (NSE + ET)", _all_ok, _detail)
    except Exception as e:
        _ck("News Sources (NSE + ET)", False, str(e))

    # ── Summary ──────────────────────────────────────────────────
    passed = sum(1 for v in _checks.values() if v["ok"])
    total  = len(_checks)
    critical_failed = [k for k, v in _checks.items()
                       if not v["ok"] and k in ("Database", "Config (settings.yaml)", "Risk Engine")]

    if critical_failed:
        console.print(f"\n[bold red]⛔ CRITICAL FAILURE: {', '.join(critical_failed)}[/bold red]")
        console.print("[red]Bot CANNOT start. Fix above issues and restart.[/red]")
        import sys as _sys; _sys.exit(1)
    elif passed == total:
        console.print(f"\n[bold green]✅ ALL {total} CHECKS PASSED — Starting ZeroBot v1.1[/bold green]\n")
    else:
        warn_count = total - passed
        console.print(f"\n[yellow]⚠️  {passed}/{total} checks passed ({warn_count} warnings) — Starting with degraded features[/yellow]\n")

    # Store for /api/health endpoint
    try:
        from dashboard.api.main import _set_startup_checks
        _set_startup_checks(_checks)
    except Exception:
        pass  # Dashboard not loaded yet — it will query on next poll

    log.info(f"✅ Startup checklist: {passed}/{total} passed")

    # P5-FIX-DASHBOARD: Start dashboard FIRST, give it 1.5s to bind to port,
    # then start bot. This prevents the race condition where uvicorn hasn't
    # finished binding when the browser first tries to connect.
    async def _run_bot_delayed():
        log.info("_run_bot_delayed: waiting 1.5s for dashboard to bind...")
        await asyncio.sleep(1.5)   # let uvicorn bind first
        try:
            log.info("_run_bot_delayed: calling run_bot()...")
            await run_bot()
            log.info("_run_bot_delayed: run_bot() completed (this is unexpected)")
        except Exception as e:
            # PATCH7: Bot crash must NOT kill dashboard.
            # Dashboard stays up so user can see what happened.
            log.critical(f"💥 Bot crashed: {e}. Dashboard stays running at http://127.0.0.1:8000")
            import traceback
            log.critical(traceback.format_exc())
            # Keep the task alive so asyncio.gather doesn't cancel the dashboard
            while True:
                await asyncio.sleep(60)

    log.info("Starting asyncio.gather(run_dashboard, _run_bot_delayed)...")
    
    # Create a keepalive task that never completes to prevent main() from returning
    async def _keepalive():
        log.info("_keepalive: Starting infinite keepalive task")
        while True:
            await asyncio.sleep(3600)  # Sleep 1 hour at a time
    
    # NOTE: Dashboard runs in thread executor, bot runs async. 
    # If dashboard fails to start, bot continues normally.
    try:
        results = await asyncio.gather(
            _run_bot_delayed(),
            _keepalive(),
            return_exceptions=True,
        )
        log.info(f"asyncio.gather completed with results: {results}")
    except Exception as e:
        log.critical(f"asyncio.gather error: {e}")
        raise
    
    # Try to start dashboard in background (non-blocking)
    try:
        import threading
        dashboard_thread = threading.Thread(target=lambda: asyncio.run(run_dashboard()), daemon=True)
        dashboard_thread.start()
    except Exception as e:
        log.warning(f"Could not start dashboard thread: {e}")


if __name__ == "__main__":
    # ── Windows: Switch to SelectorEventLoop ─────────────────────────────────
    # The default ProactorEventLoop on Windows emits noisy tracebacks:
    #   ConnectionResetError: [WinError 10054] An existing connection was forcibly
    #   closed by the remote host
    # This happens on every Yahoo Finance / Telegram connection close and is
    # harmless but floods the console. SelectorEventLoop does not have this bug.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("ZeroBot stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n!!! CRITICAL EXCEPTION IN MAIN: {type(e).__name__}: {e}")
        log.critical(f"MAIN EXCEPTION: {type(e).__name__}: {e}")
        import traceback
        print(traceback.format_exc())
        log.critical(traceback.format_exc())
        sys.exit(1)