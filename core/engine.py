# -*- coding: utf-8 -*-
"""
ZeroBot v2 — NSE India Trading Engine  [ENHANCED]
==================================================
Tasks running concurrently:
  1.  _main_loop()               — strategy signals every 60s
  2.  _realtime_feed_loop()      — price ticks every 10s
  3.  _stop_target_loop()        — monitor stop/target every 5s
  4.  _news_position_guard_loop()— exit open positions on bad news every 30s  ← NEW
  5.  _stat_arb_loop()           — pairs trading every 5 min
  6.  _daily_reset_loop()        — reset PnL at market open
  7.  _auto_squareoff_loop()     — force close at 3:15 PM
  8.  _ml_retrain_loop()         — incremental retrain trigger
  9.  _state_save_loop()         — save to DB every 30s
  10. _watchdog_loop()           — drawdown check every 60s
  11. _daily_report_loop()       — Telegram report at 3:30 PM

ENHANCEMENTS vs previous version:
  [E1] _on_news_threshold()     — instant callback when headline score >= 0.4
                                   fires a strategy scan immediately, no 60s wait
  [E2] _news_position_guard_loop() — scans every open position for breaking
                                   bad news every 30s; exits position on hard block
  [E3] sentiment_change handler — when a symbol flips bull→bear, also closes
                                   any open long on that symbol
  [E4] _on_tick() spike detect  — if price moves >= 3% in one tick, triggers
                                   an immediate strategy scan for that symbol
  [E5] news_alert + sentiment_change wired into _setup_subscriptions()
  Paper vs Live: only difference is cfg.mode = "paper" | "live"
                 broker = PaperBroker vs AngelOneBroker
                 everything else — risk gates, ML, news, strategies — identical
"""
import asyncio
import traceback
import time as _time
from datetime import datetime, time
from typing import Dict, List, Optional
from core.config import cfg
from core.logger import log
from core.event_bus import bus
from core.state_manager import state_mgr
from core.clock import session_status, is_market_hours, now_ist
from risk.risk_engine import RiskEngine, DrawdownGuard, TradeSignal
from data.feeds.historical_feed import HistoricalFeed
from data.feeds.realtime_feed import PaperRealtimeFeed, AngelOneRealtimeFeed, ShoonyaRealtimeFeed
from data.processors.indicator_engine import IndicatorEngine
from strategies.momentum import MomentumStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.vwap_strategy import VWAPStrategy
from strategies.market_making import MarketMakingStrategy
from strategies.stat_arb import StatArbStrategy
from strategies.options_strategy import OptionsStrategy
from strategies.supertrend import SupertrendStrategy          # P5-NEW
from strategies.opening_range_breakout import ORBStrategy      # P13-NEW
from strategies.rsi_divergence import RSIDivergenceStrategy    # P13-NEW
from strategies.breakout import BreakoutStrategy               # P13-NEW
from strategies.kotegawa_strategy import KotegawaStrategy       # K1-NEW
from models.predictor import EnsemblePredictor
from models.trainer import FeatureDriftDetector   # [G2] drift monitoring
from alerts.telegram_bot import alerter
from broker.factory import get_broker
from news.feed_aggregator import NewsFeedAggregator
from core.regime_detector import regime_detector              # P5-NEW
from risk.kelly_sizer import kelly_sizer                      # P5-NEW
from data.feeds.nse_option_chain import nse_option_chain      # P5-NEW
from sentiment.finbert_scorer import aggregate_symbol_sentiment  # P5-NEW
# ── G1: New intelligence modules ─────────────────────────────────────────────
from core.groq_brain import groq_brain, init_groq_brain
from core.performance_attribution import attribution
from core.portfolio_optimizer import portfolio_optimizer
from execution.vwap_slicer import vwap_slicer
from data.feeds.fii_data import fii_feed
import pandas as pd


SQUAREOFF_TIME = time(15, 15)   # 3:15 PM IST
_TICK_SPIKE_PCT = 3.0           # [E4] Force signal scan when price moves >= 3%


class ZeroBot:
    VERSION = "1.1"

    def __init__(self):
        log.info(f"ZeroBot v{self.VERSION} | NSE India | {'PAPER' if cfg.is_paper else 'LIVE'}")

        self.state = state_mgr
        self._running = False
        self._pending_symbols: set = set()
        self._last_signal_time: Dict[str, float] = {}
        self._groq_brief = None         # G1: pre-session brief from Groq Brain

        # Risk engine
        self.risk = RiskEngine(state_mgr)
        self.news_feed = NewsFeedAggregator(poll_interval_sec=120)
        self.drawdown_guard = DrawdownGuard(max_drawdown_pct=20.0)

        # Broker initialisation
        self.broker = get_broker()

        # Data feed — chosen based on broker mode, NO silent fallbacks
        self.hist_feed = HistoricalFeed()
        broker_type = type(self.broker).__name__
        mode = cfg.broker_name.lower()

        if mode in ("paper", "p_mode", "p-mode"):
            # P-mode: Yahoo Finance polling
            self.rt_feed = PaperRealtimeFeed()
        elif mode in ("s_paper", "s-paper", "shoonya_paper", "s_live", "s-live", "shoonya_live", "shoonya"):
            # S-paper / S-live: Shoonya WebSocket
            self.rt_feed = ShoonyaRealtimeFeed(self.broker)
            log.info("✅ ShoonyaRealtimeFeed selected")
        elif mode in ("a_paper", "a-paper", "hybrid", "angel_paper", "a_live", "a-live", "angel_live", "angel", "dual", "dual_mode"):
            # A-paper / A-live / Dual: Angel One WebSocket
            self.rt_feed = AngelOneRealtimeFeed(self.broker)
            log.info("✅ AngelOneRealtimeFeed selected")
        else:
            raise ValueError(f"[ENGINE] Unknown broker mode '{mode}' — cannot select feed")
        self.indicator_engine = IndicatorEngine()
        self._candle_data: Dict[str, pd.DataFrame] = {}
        self._intraday_data: Dict[str, pd.DataFrame] = {}

        # Strategies
        self.strategies = [
            MomentumStrategy(),
            MeanReversionStrategy(),
            VWAPStrategy(),
            MarketMakingStrategy(),
            SupertrendStrategy(),      # P5: ATR-based trend flip
            ORBStrategy(),             # P13: Opening Range Breakout — institutional
            RSIDivergenceStrategy(),   # P13: RSI divergence — reversal catches
            BreakoutStrategy(),        # P13: S/R breakout with volume
            KotegawaStrategy(),        # K1: BNF liquidity shock reversal
        ]
        self.kotegawa = next(s for s in self.strategies if s.name == "Kotegawa")
        self.stat_arb = StatArbStrategy()
        self.options_strategy = OptionsStrategy()
        self.predictor    = EnsemblePredictor()
        self._drift_check = FeatureDriftDetector()   # [G2-ML4]

        # P5-NEW: Regime + Kelly + Option chain
        self.regime   = regime_detector
        self.kelly    = kelly_sizer
        self.opt_chain = nse_option_chain

        self._trading_mode = cfg.trading_mode
        log.info(f"Trading mode: {self._trading_mode.upper()}")

        # ML retrain tracking
        self._trades_since_retrain = 0
        self._retrain_threshold = 50

        # [E4] Tick spike tracking: symbol → last known price
        self._last_tick_price: Dict[str, float] = {}

        # [E1] Immediate signal queue: symbols needing a fast scan due to news
        self._urgent_scan_queue: asyncio.Queue = asyncio.Queue()

        self._setup_subscriptions()
        log.info(
            f"Engine ready | Strategies: {[s.name for s in self.strategies] + ['StatArb']} | "
            f"Broker: {type(self.broker).__name__}"
        )

    def _setup_subscriptions(self):
        bus.subscribe("order_filled",      self._on_order_filled)
        bus.subscribe("risk_breach",       self._on_risk_breach)
        bus.subscribe("system_halt",       self._on_halt)
        bus.subscribe("tick",              self._on_tick)
        bus.subscribe("stop_hit",          self._on_stop_hit)
        bus.subscribe("target_hit",        self._on_target_hit)
        # [E5] Wire new news events
        bus.subscribe("news_alert",        self._on_news_alert)
        bus.subscribe("sentiment_change",  self._on_sentiment_change)

    async def start(self):
        self.state.state.status = "RUNNING"
        self.state.state.started_at = datetime.now()
        self._running = True
        # PATCH10: Clear any stale positions from previous session on startup
        # This prevents corrupted position counts from persisting across restarts
        if self.state.state.open_positions:
            log.info(f"Clearing {len(self.state.state.open_positions)} stale positions from previous session")
            self.state.state.open_positions.clear()
        if hasattr(self.broker, '_positions'):
            self.broker._positions.clear()
        
        # [HIGH#6] Guarantee daily_pnl starts at 0.0 on every startup
        # Intraday bot must never carry over previous session's PnL
        self.state.reset_daily()
        if hasattr(self.broker, 'reset_daily'):
            self.broker.reset_daily()
        log.info("[HIGH#6] Daily metrics reset on startup")

        # ── Mode summary (printed at every startup) ──────────────────────────
        bmode = cfg.broker_name.lower()
        _MODE_INFO = {
            "p_mode":   ("Yahoo Finance (15s delayed)", "Paper (NO real orders)"),
            "paper":    ("Yahoo Finance (15s delayed)", "Paper (NO real orders)"),
            "s_paper":  ("Shoonya WebSocket (live NSE)", "Paper (NO real orders)"),
            "a_paper":  ("Angel One WebSocket (live NSE)", "Paper (NO real orders)"),
            "hybrid":   ("Angel One WebSocket (live NSE)", "Paper (NO real orders)"),
            "dual":     ("Angel One WebSocket (live NSE)", "Shoonya REAL ORDERS"),
            "a_live":   ("Angel One WebSocket (live NSE)", "Angel One REAL ORDERS"),
            "s_live":   ("Shoonya WebSocket (live NSE)", "Shoonya REAL ORDERS"),
        }
        data_src, exec_src = _MODE_INFO.get(bmode, ("Unknown", "Unknown"))
        is_real_money = cfg.mode == "live"
        money_warn = "*** REAL MONEY — orders sent to NSE ***" if is_real_money else "Paper money — no real orders"

        lines = [
            "",
            "  +============================================================+",
            "  |         ZeroBot G2 -- ACTIVE MODE SUMMARY                  |",
            "  +============================================================+",
            f"  |  broker.name  : {bmode}",
            f"  |  DATA source  : {data_src}",
            f"  |  EXECUTION    : {exec_src}",
            f"  |  Capital      : {money_warn}",
            "  +------------------------------------------------------------+",
            "  |  p_mode  = Yahoo data  + Paper exec         (always safe)  |",
            "  |  s_paper = Shoonya WS  + Paper exec         (safe)         |",
            "  |  a_paper = Angel One WS + Paper exec        (safe, best)   |",
            "  |  dual    = Angel One WS + Shoonya exec      (REAL MONEY!)  |",
            "  |  a_live  = Angel One WS + Angel One exec    (REAL MONEY!)  |",
            "  |  s_live  = Shoonya WS  + Shoonya exec       (REAL MONEY!)  |",
            "  +============================================================+",
        ]
        log.info("\n".join(lines))
        log.info("ZeroBot STARTED")
        await alerter.send(
            f"ZeroBot v{self.VERSION} started\n"
            f"Mode: {'📄 PAPER' if cfg.is_paper else '🔴 LIVE'}\n"
            f"Capital: ₹{cfg.initial_capital:,.0f}\n"
            f"Symbols: {len(cfg.symbols)}\n"
            f"Strategies: Momentum, MeanReversion, VWAP, MarketMaking, StatArb",
            priority="INFO"
        )

        await self._load_historical_data()

        # P16: Order reconciliation — re-import broker positions after restart
        # Prevents losing track of positions across bot restarts/crashes
        await self._reconcile_positions()

        # G1: Boot Groq Brain + fetch FII data for the session
        try:
            from core.config import cfg as _cfg
            if _cfg.groq_api_key:
                init_groq_brain(_cfg.groq_api_key)
            _fii_data = fii_feed.fetch()
            log.info(f"[G1] FII/DII: {_fii_data.get('bias','?')} | FII={_fii_data.get('fii_net',0):+.0f}cr")
            if groq_brain.is_available:
                _vix_df   = self._candle_data.get("^VIX")
                _vix_val  = float(_vix_df.iloc[-1]["close"]) if _vix_df is not None and not _vix_df.empty else 16.0
                _nifty_df = self._candle_data.get("^NSEI")
                _nifty_chg= 0.0
                if _nifty_df is not None and len(_nifty_df)>=2:
                    _nifty_chg = float((_nifty_df.iloc[-1]["close"]-_nifty_df.iloc[-2]["close"])/_nifty_df.iloc[-2]["close"]*100)
                self._groq_brief = await groq_brain.pre_session_brief(
                    vix=_vix_val, nifty_change_pct=_nifty_chg,
                    fii_net_inr=_fii_data.get("fii_net",0),
                    global_cues=_fii_data.get("fii_label",""),
                )
                log.info(f"[BRAIN] 🧠 Session: regime={self._groq_brief.regime} bias={self._groq_brief.bias}")
                log.info(f"[BRAIN] Focus: {self._groq_brief.sector_focus} | Avoid: {self._groq_brief.sectors_avoid}")
                log.info(f"[BRAIN] {self._groq_brief.reasoning}")
        except Exception as _g1e:
            log.debug(f"[G1] Brain startup error: {_g1e}")

        await self.news_feed.start()
        self.risk.set_news_aggregator(self.news_feed)
        log.info("News feed connected to Risk Engine (Gate 11 active)")

        # [E1] Register instant news threshold callback
        self.news_feed.register_threshold_callback(self._on_news_threshold)
        log.info("News threshold callback registered (instant signal scan on high-impact news)")

        if not self.predictor.is_ready():
            log.info("No ML models found — training from scratch...")
            await self._train_ml_models()
        else:
            log.info(f"ML models loaded: {self.predictor.get_model_info()}")

        if len(self._candle_data) >= 2:
            pairs = await asyncio.get_running_loop().run_in_executor(
                None, self.stat_arb.find_pairs, self._candle_data
            )
            n_pairs = len(self.stat_arb.pairs)
            log.info(f"StatArb: {n_pairs} cointegrated pairs found")
            # FIX: Only mark calibrated if we actually found pairs OR had enough data to try
            # n_pairs==0 with sufficient data means "calibrated but no signals" which is correct
            self.stat_arb._calibrated = True
            if n_pairs == 0:
                log.warning(
                    "StatArb: No cointegrated pairs found — need correlated stocks "
                    "(e.g. HDFCBANK+ICICIBANK, INFY+TCS). Status: Calibrated, 0 active pairs."
                )
            else:
                log.info(f"StatArb: ✅ {n_pairs} cointegrated pairs — calibrated and ready")
        else:
            # Not enough data — still mark calibrated so UI shows correct status
            self.stat_arb._calibrated = False
            log.info("StatArb: Waiting for candle data (need ≥2 symbols)")

        # P16: Wire Telegram command handler to this engine instance
        try:
            if hasattr(alerter, "cmd_handler"):
                alerter.cmd_handler.set_engine(self)
                log.info("[P16] Telegram command handler wired to engine")
        except Exception as _te:
            log.debug(f"Telegram cmd handler wire failed: {_te}")

        tasks = [
            asyncio.create_task(self._main_loop(),                name="main_strategy"),
            asyncio.create_task(self._urgent_scan_loop(),         name="urgent_scan"),      # [E1]
            asyncio.create_task(self._realtime_feed_loop(),       name="rt_feed"),
            asyncio.create_task(self._candle_refresh_loop(),      name="candle_refresh"),   # PATCH11
            asyncio.create_task(self._stop_target_loop(),         name="stop_target"),
            asyncio.create_task(self._news_position_guard_loop(), name="news_pos_guard"),   # [E2]
            asyncio.create_task(self._stat_arb_loop(),            name="stat_arb"),
            asyncio.create_task(self._daily_reset_loop(),         name="daily_reset"),
            asyncio.create_task(self._auto_squareoff_loop(),      name="auto_squareoff"),
            asyncio.create_task(self._ml_retrain_loop(),          name="ml_retrain"),
            asyncio.create_task(self._state_save_loop(),          name="state_save"),
            asyncio.create_task(self._watchdog_loop(),            name="watchdog"),
            asyncio.create_task(self._daily_report_loop(),        name="daily_report"),
            # P16: Inbound Telegram commands (/halt /resume /status /positions /pnl)
            asyncio.create_task(self._telegram_polling_loop(),    name="telegram_poll"),
        ]

        log.info(f"Started {len(tasks)} concurrent tasks")
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            # Log any tasks that exited with an exception rather than silently dying
            task_names = [t.get_name() for t in tasks]
            for name, result in zip(task_names, results):
                if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                    log.error(f"Task '{name}' exited with error: {result}")
        except asyncio.CancelledError:
            log.info("Tasks cancelled — shutting down")

    # ── DATA LOADING ──────────────────────────────────────────────
    async def _load_historical_data(self):
        log.info("Loading historical data (NSE)...")
        # Load indices first (for VIX/direction), then equities
        NON_TRADEABLE_SYM = {"^NSEI","^NSEBANK","^CNXIT","^VIX","^SENSEX","^BSESN","^NIFTYIT"}
        ordered = [s for s in cfg.symbols if s in NON_TRADEABLE_SYM] + \
                  [s for s in cfg.symbols if s not in NON_TRADEABLE_SYM]
        for sym in ordered:
            # Daily data — for ML training + StatArb cointegration
            # P16: try Angel One getCandleData first (real NSE OHLCV), fallback yfinance
            df = None
            if cfg.uses_real_data and hasattr(self.broker, "getCandleData"):
                try:
                    df = await asyncio.get_running_loop().run_in_executor(
                        None, lambda s=sym: self.broker.getCandleData(s, "ONE_DAY")
                    )
                    if df is not None and not df.empty:
                        log.debug(f"[P16] getCandleData (daily) OK: {sym} {len(df)} rows")
                except Exception as _ce:
                    log.debug(f"[P16] Angel getCandleData daily failed for {sym}: {_ce}")
                    df = None
            if df is None or df.empty:
                df = await asyncio.get_running_loop().run_in_executor(
                    None, lambda s=sym: self.hist_feed.download(s, interval="1d", period="2y")
                )
            if df is not None and not df.empty:
                df = self.indicator_engine.add_all(df)
                self._candle_data[sym] = df
                # Feed returns to VaR engine
                if len(df) > 5:
                    # Pass the FULL returns series (list), not a single scalar.
                    # compute_var() calls len(rets) expecting a list — passing a
                    # float caused "object of type 'float' has no len()" every cycle.
                    daily_rets = df["close"].pct_change().dropna().tolist()
                    self.risk.update_returns_cache(sym, daily_rets)

            # Intraday 5m data — for VWAP, Momentum, MeanReversion strategies
            # Skip symbols already marked dead (failed 1d download)
            if sym not in NON_TRADEABLE_SYM:
                from data.feeds.historical_feed import _DEAD_SYMBOLS as _DS
                if sym not in _DS:
                    # P16: try Angel One 5m candles first
                    df_5m = None
                    if cfg.uses_real_data and hasattr(self.broker, "getCandleData"):
                        try:
                            df_5m = await asyncio.get_running_loop().run_in_executor(
                                None, lambda s=sym: self.broker.getCandleData(s, "FIVE_MINUTE")
                            )
                        except Exception:
                            df_5m = None
                    if df_5m is None or df_5m.empty:
                        df_5m = await asyncio.get_running_loop().run_in_executor(
                            None, lambda s=sym: self.hist_feed.download(s, interval="5m", period="5d")
                        )
                    if df_5m is not None and not df_5m.empty:
                        df_5m = self.indicator_engine.add_all(df_5m)
                        self._intraday_data[sym] = df_5m

        # PATCH11: Explicitly download ^INDIAVIX historical (not in cfg.symbols)
        # so the VIX regime gate has a real seed value instead of defaulting to 14.0
        for vix_sym in ("^INDIAVIX", "^BSESN"):
            if vix_sym not in self._candle_data:
                try:
                    df_vix = await asyncio.get_running_loop().run_in_executor(
                        None, lambda s=vix_sym: self.hist_feed.download(s, interval="1d", period="1y")
                    )
                    if df_vix is not None and not df_vix.empty:
                        # Store under the ^VIX key that the strategy cycle reads
                        key = "^VIX" if "VIX" in vix_sym else vix_sym
                        self._candle_data[key] = df_vix
                        if "VIX" in vix_sym:
                            vix_seed = float(df_vix.iloc[-1]["close"])
                            self.state.state.market_data["india_vix"] = vix_seed
                            log.info(f"VIX seeded from {vix_sym}: {vix_seed:.1f}")
                except Exception as e:
                    log.debug(f"Optional {vix_sym} download failed: {e}")

        dead = len(ordered) - len(self._candle_data)
        if dead > 0:
            from data.feeds.historical_feed import _DEAD_SYMBOLS
            log.warning(f"⚠️  {dead} symbols skipped (dead/delisted): {_DEAD_SYMBOLS}")
        log.info(f"Loaded {len(self._candle_data)} symbols daily | {len(self._intraday_data)} symbols intraday (5m)")

        # [H4] Seed VIX in state from historical data so Gate 7 works before
        # the first ^VIX tick arrives (which can take up to 30s).
        vix_df = self._candle_data.get("^VIX")
        if vix_df is not None and not vix_df.empty:
            vix_seed = float(vix_df.iloc[-1]["close"])
            self.state.state.market_data["india_vix"] = vix_seed
            log.info(f"VIX seeded from historical data: {vix_seed:.1f}")

    async def _reconcile_positions(self):
        """
        P16: On startup, fetch live broker positions and re-import into state.
        Prevents the bot from opening duplicate positions after a crash/restart.
        Called once at startup after historical data is loaded.
        """
        try:
            broker_positions = self.broker.get_positions()
            # G1-FIX-F6: get_positions() can return None if broker is disconnected.
            # Without this check, iterating None.items() crashes silently and leaves
            # state inconsistent. Must distinguish None (error) from {} (no positions).
            if broker_positions is None:
                log.warning("[G1-F6] ⚠️  get_positions() returned None — broker disconnected, skipping reconcile")
                return
            if not broker_positions:
                log.info("[P16] Position reconciliation: no open positions found in broker")
                return
            count = 0
            for sym, pos in broker_positions.items():
                if sym not in self.state.state.open_positions:
                    # Re-import broker position into state
                    self.state.state.open_positions[sym] = {
                        "qty":            pos.get("qty", 0),
                        "avg_price":      pos.get("avg_price", 0),
                        "open_price":     pos.get("avg_price", 0),
                        "side":           pos.get("side", "LONG"),
                        "stop_loss":      pos.get("stop_loss"),
                        "target":         pos.get("target"),
                        "strategy":       pos.get("strategy", "RECOVERED"),
                        "confidence":     pos.get("confidence", 0),
                        "current_price":  pos.get("current_price", pos.get("avg_price", 0)),
                        "unrealized_pnl": pos.get("unrealized_pnl", 0),
                        "mode":           cfg.mode,
                        "opened_at":      pos.get("opened_at", ""),
                        "recovered":      True,    # Marks as recovered position
                    }
                    count += 1
                    log.info(
                        f"[P16] Recovered position: {sym} "
                        f"{pos.get('side','?')} {pos.get('qty',0)} @ "
                        f"₹{pos.get('avg_price',0):.2f}"
                    )
            if count > 0:
                log.info(f"[P16] ✅ Reconciled {count} positions from broker")
                from alerts.telegram_bot import alerter
                await alerter.send(
                    f"♻️ Position Recovery\n{count} positions recovered from broker\n"
                    + "\n".join(
                        f"  {sym}: {p.get('side')} {p.get('qty')} @ ₹{p.get('avg_price',0):.2f}"
                        for sym, p in list(self.state.state.open_positions.items())
                        if p.get("recovered")
                    ),
                    priority="HIGH", alert_type="recovery"
                )
        except Exception as e:
            log.warning(f"[P16] Position reconciliation error: {e} — continuing without")

    async def _train_ml_models(self):
        from models.trainer import ModelTrainer
        trainer = ModelTrainer()
        # Train on top 3 liquid symbols for better generalization
        PREFERRED_TRAIN_SYMBOLS = ["HDFCBANK.NS", "RELIANCE.NS", "TCS.NS", "ICICIBANK.NS", "INFY.NS"]
        trained = 0
        for sym in PREFERRED_TRAIN_SYMBOLS:
            df = self._candle_data.get(sym)
            if df is not None and len(df) > 200:
                sym_clean = sym.replace(".NS", "").replace("^", "")
                await asyncio.get_running_loop().run_in_executor(
                    None, trainer.train_full, df, sym_clean, self._candle_data
                )
                trained += 1
                if trained >= 3:  # Train on 3 symbols max for speed
                    break
        if trained == 0:
            # Fallback: use whatever we have
            for sym, df in self._candle_data.items():
                if len(df) > 200:
                    await asyncio.get_running_loop().run_in_executor(
                        None, trainer.train_full, df, sym.replace(".NS", "").replace("^", "")
                    )
                    break
        self.predictor._load_models()
        log.info(f"ML training complete ({trained} symbols): {self.predictor.get_model_info()}")

    async def _drift_check_loop(self):  # [G2-ML4]
        """Hourly feature drift check — flags predictor if drift detected."""
        while self._running:
            await asyncio.sleep(3600)
            try:
                for sym, df in list(self._candle_data.items())[:3]:
                    if df is None or len(df) < 30: continue
                    from data.processors.indicator_engine import IndicatorEngine
                    ie   = IndicatorEngine()
                    feat = ie.add_all(df.copy()).iloc[-50:]
                    result = self._drift_check.check(feat)
                    if result.get('drifted'):
                        self.predictor.flag_drift()
                    break
            except Exception as e:
                log.debug(f'Drift check error: {e}')

    # ── MAIN STRATEGY LOOP ────────────────────────────────────────
    async def _main_loop(self):
        """Core loop: run all strategies every 60s during market hours."""
        while self._running:
            try:
                sess = session_status()
                if not sess["is_market_hours"]:
                    sleep_time = 3600 if not sess["is_market_day"] else 30
                    await asyncio.sleep(sleep_time)
                    continue
                if sess.get("is_warmup") or sess.get("is_closing"):
                    await asyncio.sleep(30)
                    continue

                await self._run_strategy_cycle()
                await asyncio.sleep(60)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Main loop error: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(30)

    async def _run_strategy_cycle(self):
        """Run all single-symbol strategies for all loaded symbols."""
        import time as _t
        NON_TRADEABLE = {"^NSEI", "^NSEBANK", "^CNXIT", "^VIX", "^SENSEX", "^BSESN", "^NIFTYIT"}
        INTRADAY_STRATEGIES = {"VWAP", "Momentum", "MeanReversion", "Kotegawa"}
        # Track symbols that got a signal this cycle — only first strategy wins per symbol per cycle
        _signalled_this_cycle: set = set()
        _now = _t.monotonic()
        _max_new_this_cycle = 2  # [FIX] Max 2 NEW positions per strategy cycle (prevents bulk fill)
        _new_this_cycle = 0

        for sym, df in self._candle_data.items():
            if sym in NON_TRADEABLE:
                continue
            if df.empty or len(df) < 50:
                continue

            # [FIX] Max 2 new positions per cycle — prevent bulk simultaneous orders
            if _new_this_cycle >= _max_new_this_cycle:
                break

            # Already have open position or pending order — skip entirely
            if sym in self.state.state.open_positions or sym in self._pending_symbols:
                continue

            # Per-symbol cooldown: don't re-signal same symbol within 15 minutes
            # PATCH12: Reduced from 1h → 15min. Strategies have their own
            # internal filters (RSI, ADX, VWAP) — no need for aggressive external cooldown.
            _last = self._last_signal_time.get(sym, 0)
            if _now - _last < 900:  # 15 minutes
                continue

            ml_pred = self.predictor.predict(df, sym)
            cmp = float(df.iloc[-1]["close"])

            # Always use India VIX (^INDIAVIX) not US VIX (^VIX)
            # Prefer live india_vix from state (set from ^INDIAVIX downloads)
            vix = float(self.state.state.market_data.get("india_vix", 18.0))
            if vix <= 0:
                vix_df = self._candle_data.get("^VIX", pd.DataFrame())
                vix = float(vix_df.iloc[-1]["close"]) if not vix_df.empty else 18.0

            # P5-NEW: Update regime detector with latest VIX
            nifty_df = self._candle_data.get("^NSEI", pd.DataFrame())
            nifty_px  = float(nifty_df.iloc[-1]["close"]) if not nifty_df.empty else 0
            nifty_sma = float(nifty_df["close"].rolling(50).mean().iloc[-1]) if len(nifty_df) >= 50 else 0
            self.regime.update(vix=vix, nifty_price=nifty_px, nifty_sma50=nifty_sma)

            # P5-NEW: Update Kelly consecutive-loss awareness
            cons_losses = getattr(self.state.state, "consecutive_losses", 0)
            self.kelly.update_fraction(cons_losses)

            for strategy in self.strategies:
                if not strategy.enabled:
                    continue

                # Only one strategy per symbol per cycle (prevents VWAP+MarketMaking double-buy)
                if sym in _signalled_this_cycle:
                    break

                # Route to appropriate timeframe data
                if strategy.name in INTRADAY_STRATEGIES and sym in self._intraday_data:
                    signal_df = self._intraday_data[sym]
                else:
                    signal_df = df

                # K1: Inject rich context for KotegawaStrategy; other strategies
                # ignore unknown kwargs via their fixed generate_signal signature.
                if strategy.name == "Kotegawa":
                    _nifty_df   = self._candle_data.get("^NSEI", pd.DataFrame())
                    _nifty_chg  = 0.0
                    if not _nifty_df.empty and len(_nifty_df) >= 2:
                        _nifty_chg = float(
                            (_nifty_df["close"].iloc[-1] / _nifty_df["close"].iloc[-2] - 1) * 100
                        )
                    signal = strategy.generate_signal(
                        signal_df, sym,
                        candle_data      = self._candle_data,
                        news_feed        = self.news_feed,
                        ml_confidence    = ml_pred.get("confidence", 50.0),
                        ml_direction     = ml_pred.get("direction", "HOLD"),
                        regime           = self.regime.state.regime.value,
                        nifty_change_pct = _nifty_chg,
                    )
                else:
                    signal = strategy.generate_signal(signal_df, sym)
                if not signal:
                    continue

                # P5-NEW: Regime gate — block trades in CRISIS, reduce size in DEFENSIVE
                is_opt = sym.endswith("CE") or sym.endswith("PE")
                regime_ok, regime_reason = self.regime.is_trading_allowed(is_option=is_opt)
                if not regime_ok:
                    log.info(f"[REGIME] {sym} blocked: {regime_reason}")
                    continue

                # [G3-ML] Extract new G3 predictor fields (backward-compat with G2)
                ml_conf               = ml_pred.get("confidence", 50.0)
                ml_dir                = ml_pred.get("direction", "HOLD")
                ml_dir_prob           = ml_pred.get("direction_probability",
                                                    {"BUY": 0.0, "HOLD": 1.0, "SELL": 0.0})
                ml_expected_return    = ml_pred.get("expected_return_score", 0.0)

                # [G3-ML] ML direction filter — upgraded from G2 flat penalty.
                # Now uses direction_probability for a graded penalty/boost:
                #   - ML hard disagrees  (opposite dir_prob > 0.55) → -20 conf
                #   - ML soft disagrees  (opposite dir_prob 0.40–0.55) → -10 conf
                #   - ML neutral/agrees  → blend 60/40 strategy/ML
                #   - ML strongly agrees (same dir_prob > 0.60) → +5 conf bonus
                opp_dir = "SELL" if signal.side == "BUY" else "BUY"
                opp_prob = ml_dir_prob.get(opp_dir, 0.0)
                same_prob = ml_dir_prob.get(signal.side, 0.0)

                if ml_dir not in (signal.side, "HOLD"):
                    if opp_prob > 0.55:
                        signal.confidence = max(35, signal.confidence - 20.0)
                        log.debug(f"[G3-ML] Hard disagree {sym}: {ml_dir} p={opp_prob:.2f} → conf-20")
                    else:
                        signal.confidence = max(40, signal.confidence - 10.0)
                        log.debug(f"[G3-ML] Soft disagree {sym}: {ml_dir} p={opp_prob:.2f} → conf-10")
                else:
                    if same_prob > 0.60:
                        signal.confidence = min(95, signal.confidence + 5.0)
                        log.debug(f"[G3-ML] Strong agree {sym}: p={same_prob:.2f} → conf+5")
                signal.confidence = (signal.confidence * 0.6 + ml_conf * 0.4)

                # P5-NEW: FinBERT/keyword sentiment modifier
                # G1: FII flow confidence modifier
                _fii_mod = fii_feed.signal_modifier(signal.side)
                if abs(_fii_mod) > 0:
                    signal.confidence = max(30, min(95, signal.confidence + _fii_mod))
                    log.debug(f"[FII] {sym} conf {_fii_mod:+.1f}% ({fii_feed._cache.get('fii_label','?') if fii_feed._cache else 'N/A'})")

                sym_headlines = self.news_feed.get_headlines_for_symbol(sym) if hasattr(self.news_feed, "get_headlines_for_symbol") else []
                if sym_headlines:
                    sentiment = aggregate_symbol_sentiment(sym_headlines[-10:], sym)
                    modifier = sentiment.get("confidence_modifier", 0.0)
                    # Only boost if sentiment agrees with signal direction
                    if (signal.side == "BUY" and sentiment["direction"] > 0) or \
                       (signal.side == "SELL" and sentiment["direction"] < 0):
                        signal.confidence = min(95, signal.confidence + abs(modifier))
                    elif sentiment["direction"] != 0:
                        signal.confidence = max(30, signal.confidence - abs(modifier) * 0.5)
                    log.debug(f"Sentiment [{sym}]: {sentiment['label'] if 'label' in sentiment else sentiment['direction']:+} → conf={signal.confidence:.1f}")

                _signalled_this_cycle.add(sym)
                self._last_signal_time[sym] = _now  # Set 4h cooldown

                await bus.publish("signal", {
                    "symbol": sym, "side": signal.side,
                    "strategy": strategy.name, "confidence": signal.confidence,
                    "ml_pred": ml_pred, "cmp": cmp,
                    "timestamp": datetime.now().isoformat(),
                })

                # 10-gate risk check
                adx = float(df.iloc[-1].get("ADX_14", 20)) if df is not None and not df.empty else None
                risk_result = self.risk.evaluate(signal, cmp=cmp, vix=vix, adx=adx)

                # P5-NEW: Kelly sizing override — replaces flat qty
                if risk_result.approved:
                    regime_mult = self.regime.get_size_multiplier()
                    # G1: Correlation-aware size multiplier
                    _corr_mult = portfolio_optimizer.correlation_multiplier(
                        candidate=sym,
                        open_positions=self.state.state.open_positions,
                        candle_data=self._candle_data,
                    )
                    rr = getattr(risk_result, "rr_ratio", 2.0)
                    kelly_result = self.kelly.compute(
                        capital=self.state.state.capital,
                        cmp=cmp,
                        confidence=signal.confidence,
                        rr_ratio=rr,
                        regime_mult=regime_mult * _corr_mult,
                        expected_return_score=ml_expected_return,  # [G3-ML]
                    )
                    # Use Kelly qty but don't exceed risk engine recommendation
                    final_qty = min(kelly_result.qty, risk_result.recommended_qty)
                    final_qty = max(1, final_qty)
                    if final_qty != risk_result.recommended_qty:
                        log.debug(f"Kelly sizing: {risk_result.recommended_qty}→{final_qty}qty ({kelly_result.basis[:60]})")
                    risk_result.recommended_qty = final_qty

                # Save signal to DB regardless
                await state_mgr.save_signal({
                    "symbol": sym, "side": signal.side,
                    "confidence": signal.confidence, "strategy": strategy.name,
                    "acted_on": risk_result.approved,
                    "blocked_reason": risk_result.blocked_reason,
                    "features": ml_pred.get("features_snapshot", {}),
                })

                if risk_result.approved:
                    # [FIX] Stagger order placement — prevent all orders firing simultaneously
                    # Event-driven: each approved signal waits briefly before placing order
                    await asyncio.sleep(0.1 + len(self.state.state.open_positions) * 0.05)
                    order = await self.broker.place_order(
                        symbol=sym,
                        side=signal.side,
                        qty=risk_result.recommended_qty,
                        cmp=cmp,
                        strategy=strategy.name,
                        stop_loss=risk_result.stop_loss,
                        target=risk_result.target,
                        confidence=signal.confidence,
                    )

                    _new_this_cycle += 1
                    order_id = order.order_id if hasattr(order, 'order_id') else str(id(order))
                    self.state.state.active_orders[order_id] = {
                        "symbol": sym, "side": signal.side,
                        "strategy": strategy.name, "cmp": cmp,
                    }
                    self._pending_symbols.add(sym)  # Block concurrent orders for this symbol
                    self.state.state.daily_trades += 1
                    self._trades_since_retrain += 1
                    # G1: Wire attribution for per-strategy tracking
                    # (full record_trade called in _on_position_closed below)

                    await alerter.trade_signal(sym, signal.side, signal.confidence, strategy.name, cmp, stop_loss=risk_result.stop_loss, target=risk_result.target, sentiment=getattr(signal,"sentiment_score",0))

                    # ── Options: also fire an options trade if mode allows ──
                    if self._trading_mode in ("options", "both"):
                        await self._fire_options_signal(sym, signal.side, df)

                else:
                    await state_mgr.save_risk_event(
                        "BLOCKED", risk_result.blocked_reason, sym, "MEDIUM"
                    )

    # ── OPTIONS SIGNAL HANDLER ────────────────────────────────────
    async def _fire_options_signal(self, underlying: str, direction: str, df: pd.DataFrame):
        """
        Fire an options trade when an equity signal fires and trading_mode allows it.
        Uses the OptionsStrategy to select strike, price, and size the trade.
        Only fires if the underlying is in cfg.options.underlyings.
        """
        try:
            if underlying not in cfg.options.underlyings:
                return

            opt_signal = self.options_strategy.generate_signal(
                df, underlying, equity_signal_side=direction
            )
            if not opt_signal:
                return

            cmp = opt_signal.cmp  # option premium
            # Options risk check — simplified (no sector/correlation gates, just margin)
            # Use India VIX for options evaluation
            _vix = float(self.state.state.market_data.get("india_vix", 0)) or None
            risk_result = self.risk.evaluate(opt_signal, cmp=cmp, vix=_vix)
            await state_mgr.save_signal({
                "symbol": opt_signal.symbol, "side": "BUY",
                "confidence": opt_signal.confidence, "strategy": "Options",
                "acted_on": risk_result.approved,
                "blocked_reason": risk_result.blocked_reason,
            })
            if risk_result.approved:
                order = await self.broker.place_order(
                    symbol=opt_signal.symbol,
                    side="BUY",
                    qty=opt_signal.suggested_qty,
                    cmp=cmp,
                    strategy="Options",
                    stop_loss=risk_result.stop_loss,
                    target=risk_result.target,
                    confidence=opt_signal.confidence,
                )
                await alerter.send(
                    f"📈 Options trade: {opt_signal.symbol}\n{opt_signal.trigger}",
                    priority="HIGH"
                )
                log.info(f"Options order placed: {opt_signal.symbol} | {opt_signal.trigger}")
            else:
                log.debug(f"Options blocked: {opt_signal.symbol} — {risk_result.blocked_reason}")
        except Exception as e:
            log.error(f"Options signal error for {underlying}: {e}")

    # ── STOP/TARGET MONITORING (critical for loss minimization) ──
    async def _stop_target_loop(self):
        """
        Check stop losses and targets every 5 seconds.
        This is the most important loss-minimization mechanism.
        """
        while self._running:
            try:
                positions = self.broker.get_positions()
                if positions is None:
                    log.debug("[CRIT-F2] Broker disconnected, skipping stop/target check")
                    await asyncio.sleep(5)
                    continue
                for sym, pos in positions.items():
                    current_price = self._get_current_price(sym)
                    if current_price and hasattr(self.broker, 'check_stops_and_targets'):
                        await self.broker.check_stops_and_targets(sym, current_price)

                        # Update unrealized PnL in state
                        avg_price = pos.get("avg_price", 0)
                        qty = pos.get("qty", 0)
                        unrealized = (current_price - avg_price) * qty
                        pos["unrealized_pnl"] = round(unrealized, 2)
                        pos["current_price"] = current_price

            except Exception as e:
                log.debug(f"Stop/target loop error: {e}")
            await asyncio.sleep(5)

    def _get_current_price(self, symbol: str) -> Optional[float]:
        """Get latest price — realtime feed preferred, historical close as fallback."""
        # Always prefer realtime (live Yahoo Finance) over historical close
        rt_price = self.rt_feed.get_last_price(symbol)
        if rt_price and rt_price > 0:
            return float(rt_price)
        # Fallback: last candle close (yesterday's close on holiday)
        df = self._candle_data.get(symbol)
        if df is not None and not df.empty:
            return float(df.iloc[-1]["close"])
        return None

    # ── [E1] INSTANT NEWS THRESHOLD CALLBACK ─────────────────────
    async def _on_news_threshold(self, item) -> None:
        """
        [E1] Called by NewsFeedAggregator immediately when any headline crosses
        the |score| >= 0.4 threshold — no 60-second polling wait.

        Behaviour:
          - For BULLISH news (score >= +0.4): queue immediate BUY scan for
            each tagged symbol that we don't already hold.
          - For BEARISH news (score <= -0.4): queue immediate SELL scan for
            each symbol we currently hold (position guard).
          - Hard blocks (fraud/ED/SEBI): close position instantly without
            waiting for _news_position_guard_loop.
        """
        score   = item.sentiment_score
        symbols = item.symbols or []
        if not symbols:
            return

        try:
            is_hard, kw = self.news_feed._sentiment.is_hard_block(item.title)
        except Exception:
            is_hard, kw = False, ""

        for sym in symbols:
            # ── Hard block: immediate exit of any open position ──
            if is_hard and sym in self.state.state.open_positions:
                log.warning(
                    f"[E1] HARD BLOCK immediate exit | {sym} | '{kw}' in: {item.title[:60]}"
                )
                await self._emergency_exit(sym, reason=f"Hard block news: {kw}")
                continue

            # ── Strong bullish news → queue urgent BUY scan ──
            if score >= 0.4 and sym not in self.state.state.open_positions \
                    and sym not in self._pending_symbols:
                log.info(f"[E1] Urgent BUY scan queued | {sym} | score={score:+.2f}")
                await self._urgent_scan_queue.put({"sym": sym, "reason": "bullish_news"})

            # ── Strong bearish news → queue urgent SELL scan ──
            elif score <= -0.4 and sym in self.state.state.open_positions:
                log.info(f"[E1] Urgent SELL scan queued | {sym} | score={score:+.2f}")
                await self._urgent_scan_queue.put({"sym": sym, "reason": "bearish_news"})

    # ── [E1] URGENT SCAN LOOP ─────────────────────────────────────
    async def _urgent_scan_loop(self) -> None:
        """
        [E1] Drains the _urgent_scan_queue and runs a targeted one-symbol
        strategy cycle immediately — no 60-second wait.
        Queue items: {"sym": str, "reason": str}
        """
        while self._running:
            try:
                # G1-FIX-F3: During volatile sessions, tick spikes queue thousands
                # of items. Without a cap this causes memory pressure and scan backlog.
                # Solution: if queue > 20, drain excess and log a warning.
                _qsz = self._urgent_scan_queue.qsize()
                if _qsz > 20:
                    _drained = 0
                    while self._urgent_scan_queue.qsize() > 5:
                        try:
                            self._urgent_scan_queue.get_nowait()
                            _drained += 1
                        except asyncio.QueueEmpty:
                            break
                    log.warning(f"[G1-F3] Urgent scan queue overflow ({_qsz} items) — drained {_drained} stale items, keeping 5")

                item = await asyncio.wait_for(self._urgent_scan_queue.get(), timeout=5.0)
                sym    = item["sym"]
                reason = item["reason"]

                sess = session_status()
                if not sess["is_market_hours"] or sess.get("is_warmup") or sess.get("is_closing"):
                    log.debug(f"[E1] Urgent scan {sym} skipped — market not in active session")
                    continue

                log.info(f"[E1] Urgent scan running | {sym} | reason={reason}")
                await self._run_strategy_for_symbol(sym, reason=reason)

            except asyncio.TimeoutError:
                pass  # normal — queue empty
            except Exception as e:
                log.debug(f"Urgent scan loop error: {e}")

    async def _run_strategy_for_symbol(self, sym: str, reason: str = "") -> None:
        """
        Run all strategies for a SINGLE symbol immediately.
        Shared by _urgent_scan_loop and _on_tick spike handler.
        Respects all existing guards (pending, cooldown, position).
        """
        NON_TRADEABLE = {"^NSEI","^NSEBANK","^CNXIT","^VIX","^SENSEX","^BSESN","^NIFTYIT"}
        INTRADAY_STRATEGIES = {"VWAP","Momentum","MeanReversion","Kotegawa"}

        if sym in NON_TRADEABLE:
            return
        if sym in self.state.state.open_positions or sym in self._pending_symbols:
            return

        df = self._candle_data.get(sym)
        if df is None or df.empty or len(df) < 50:
            return

        cmp      = self._get_current_price(sym) or float(df.iloc[-1]["close"])
        ml_pred  = self.predictor.predict(df, sym)
        vix      = float(self.state.state.market_data.get("india_vix", 18.0)) or 18.0
        adx      = float(df.iloc[-1].get("ADX_14", 20))

        _now = _time.monotonic()
        _last = self._last_signal_time.get(sym, 0)
        # P16: News-triggered scans bypass the cooldown — they are time-sensitive
        is_news_trigger = reason in ("bullish_news", "bearish_news_short", "bearish_news", "sentiment_flip")
        if _now - _last < 3600 and not is_news_trigger:  # 1h cooldown except news events
            log.debug(f"[E1] {sym} still in signal cooldown (news bypass={is_news_trigger})")
            return

        for strategy in self.strategies:
            if not strategy.enabled:
                continue
            signal_df = self._intraday_data.get(sym, df) \
                if strategy.name in INTRADAY_STRATEGIES else df
            signal = strategy.generate_signal(signal_df, sym)
            if not signal:
                continue
            # [G3-ML] Graded direction filter using direction_probability
            ml_dir_prob2 = ml_pred.get("direction_probability",
                                        {"BUY": 0.0, "HOLD": 1.0, "SELL": 0.0})
            opp_dir2  = "SELL" if signal.side == "BUY" else "BUY"
            opp_prob2 = ml_dir_prob2.get(opp_dir2, 0.0)
            if ml_pred.get("direction") not in (signal.side, "HOLD") and opp_prob2 > 0.55:
                continue  # hard block only when model is confident in opposite

            ml_conf = ml_pred.get("confidence", 50.0)
            signal.confidence = signal.confidence * 0.6 + ml_conf * 0.4
            self._last_signal_time[sym] = _now

            risk_result = self.risk.evaluate(signal, cmp=cmp, vix=vix, adx=adx)
            await state_mgr.save_signal({
                "symbol": sym, "side": signal.side,
                "confidence": signal.confidence, "strategy": strategy.name,
                "acted_on": risk_result.approved,
                "blocked_reason": risk_result.blocked_reason,
                "features": ml_pred.get("features_snapshot", {}),
            })
            if risk_result.approved:
                order = await self.broker.place_order(
                    symbol=sym, side=signal.side,
                    qty=risk_result.recommended_qty, cmp=cmp,
                    strategy=strategy.name,
                    stop_loss=risk_result.stop_loss,
                    target=risk_result.target,
                    confidence=signal.confidence,
                )
                order_id = order.order_id if hasattr(order, "order_id") else str(id(order))
                self.state.state.active_orders[order_id] = {
                    "symbol": sym, "side": signal.side,
                    "strategy": strategy.name, "cmp": cmp,
                }
                self._pending_symbols.add(sym)
                self.state.state.daily_trades += 1
                self._trades_since_retrain += 1
                await alerter.trade_signal(sym, signal.side, signal.confidence, strategy.name, cmp, stop_loss=risk_result.stop_loss, target=risk_result.target, sentiment=getattr(signal,"sentiment_score",0))
            else:
                await state_mgr.save_risk_event("BLOCKED", risk_result.blocked_reason, sym, "MEDIUM")
            break  # one strategy per urgent scan

    # ── [E2] NEWS POSITION GUARD LOOP ────────────────────────────
    async def _news_position_guard_loop(self) -> None:
        """
        [E2] Scans every open position for breaking bad news every 30 seconds.
        This is the missing piece: previously has_breaking_negative_news() only
        blocked NEW entries. Now it also EXITS positions that have deteriorated.

        Exit conditions:
          1. Hard block (fraud/ED/SEBI/arrested): exit immediately at market.
          2. Soft bearish (score <= -0.5): exit if position is currently in loss
             OR if news score worsened significantly since entry.
        """
        while self._running:
            try:
                positions = dict(self.state.state.open_positions)
                for sym, pos in positions.items():
                    if sym in self._pending_symbols:
                        continue  # order already in flight for this symbol

                    # Hard block — always exit immediately
                    blocked, reason = self.news_feed.has_breaking_negative_news(sym)
                    if blocked:
                        log.warning(
                            f"[E2] NEWS GUARD hard block | {sym} | {reason} — exiting position"
                        )
                        await self._emergency_exit(sym, reason=f"Hard block: {reason}")
                        continue

                    # Soft bearish — exit if position is in loss
                    news_result = self.news_feed.get_sentiment_score(sym)
                    if news_result.has_fresh_data and float(news_result) <= -0.5:
                        cmp = self._get_current_price(sym)
                        if cmp is None:
                            continue
                        avg_price    = pos.get("avg_price", cmp)
                        unrealized   = (cmp - avg_price) * pos.get("qty", 1)
                        if unrealized < 0:
                            log.warning(
                                f"[E2] NEWS GUARD soft bearish | {sym} | "
                                f"score={float(news_result):+.2f} | "
                                f"unrealized=₹{unrealized:+.0f} — exiting losing position"
                            )
                            await self._emergency_exit(
                                sym,
                                reason=f"Bearish news ({float(news_result):+.2f}) + unrealized loss"
                            )

            except Exception as e:
                log.debug(f"News position guard error: {e}")
            await asyncio.sleep(30)

    async def _emergency_exit(self, sym: str, reason: str = "") -> None:
        """
        Market-sell an open position immediately.
        Used by news guard and hard block handlers.
        Idempotent: does nothing if symbol is not in open positions.
        """
        pos = self.state.state.open_positions.get(sym)
        if not pos:
            return
        if sym in self._pending_symbols:
            log.debug(f"Emergency exit {sym} deferred — order already pending")
            return

        cmp = self._get_current_price(sym) or pos.get("avg_price", 0)
        qty = pos.get("qty", 0)
        if qty <= 0:
            return

        log.warning(f"EMERGENCY EXIT | {sym} | {qty}qty @ ₹{cmp:.2f} | reason: {reason}")
        self._pending_symbols.add(sym)

        try:
            await self.broker.place_order(
                symbol=sym, side="SELL", qty=qty, cmp=cmp,
                strategy="EMERGENCY_EXIT",
            )
            await alerter.risk_alert(
                f"⚠️ Emergency exit: {sym}\nReason: {reason}\nQty: {qty} @ ₹{cmp:.2f}"
            )
            await state_mgr.save_risk_event(
                "EMERGENCY_EXIT", f"{reason} — {sym} {qty}qty @ {cmp}", sym, "HIGH"
            )
        except Exception as e:
            log.error(f"Emergency exit failed for {sym}: {e}")
            self._pending_symbols.discard(sym)

    # ── [E3] SENTIMENT CHANGE HANDLER ────────────────────────────
    async def _on_sentiment_change(self, data: dict) -> None:
        """
        [E3] Fired by EventBus when a symbol's sentiment crosses a threshold.
        If we hold a long position and sentiment flips strongly bearish,
        queue an urgent scan that will evaluate whether to exit.
        """
        sym           = data.get("symbol", "")
        new_score     = data.get("new_score", 0.0)
        direction     = data.get("direction_change", "")

        if direction == "bull_to_bear" and sym in self.state.state.open_positions:
            log.info(
                f"[E3] Sentiment flip bull→bear | {sym} | "
                f"{data.get('old_score', 0):+.2f} → {new_score:+.2f} | "
                f"queuing position review"
            )
            await self._urgent_scan_queue.put({"sym": sym, "reason": "sentiment_flip"})

    # ── [E5] NEWS ALERT HANDLER ───────────────────────────────────
    async def _on_news_alert(self, data: dict) -> None:
        """
        [E5] Fired by EventBus on every high-impact headline (|score| >= 0.4).
        Logs the alert and sends a Telegram notification for hard blocks.
        Actual trade logic is in _on_news_threshold (registered on aggregator).
        """
        sym      = data.get("symbol") or (data.get("symbols") or [None])[0]
        title    = data.get("title", "")
        score    = data.get("score", 0.0)
        is_hard  = data.get("is_hard_block", False)

        # PATCH7: Only log news items with meaningful score at INFO.
        # Zero-score routine NSE announcements (100s per day) are silenced at DEBUG.
        if abs(score) >= 0.15 or is_hard:
            log.info(f"[E5] news_alert | {sym} | score={score:+.2f} | {title[:70]}")
        else:
            log.debug(f"[E5] news_alert | {sym} | score={score:+.2f} | {title[:60]}")

        if is_hard:
            await alerter.risk_alert(
                f"🚨 Hard block news\n{sym}: {title[:100]}\nScore: {score:+.2f}"
            )

    # ── AUTO SQUARE-OFF (3:15 PM) ─────────────────────────────────
    async def _auto_squareoff_loop(self):
        """
        Auto square-off all intraday positions at 3:15 PM IST.
        Mirrors Angel One's behavior exactly.
        """
        while self._running:
            now = now_ist()
            if now.time() >= SQUAREOFF_TIME and now.time() < time(15, 30):
                positions = self.broker.get_positions()
                if positions:
                    log.info(f"AUTO SQUARE-OFF: {len(positions)} positions at {now.strftime('%H:%M')}")
                    if hasattr(self.broker, 'square_off_all_intraday'):
                        await self.broker.square_off_all_intraday()
                    await alerter.system_halted(
                        f"Auto square-off at 15:15 IST — {len(positions)} positions closed"
                    )
            await asyncio.sleep(30)

    # ── STAT ARB LOOP ─────────────────────────────────────────────
    async def _stat_arb_loop(self):
        # Indices are not tradeable — StatArb must only trade equity pairs
        NON_TRADEABLE = {"^NSEI", "^NSEBANK", "^CNXIT", "^VIX", "^SENSEX", "^BSESN", "^NIFTYIT"}
        while self._running:
            try:
                if self.stat_arb.pairs and len(self._candle_data) >= 2:
                    for sym_a, sym_b in self.stat_arb.pairs[:3]:
                        # Skip pairs that include indices
                        if sym_a in NON_TRADEABLE or sym_b in NON_TRADEABLE:
                            continue
                        if sym_a not in self._candle_data or sym_b not in self._candle_data:
                            continue
                        signals = self.stat_arb.generate_signal_for_pair(sym_a, sym_b, self._candle_data)
                        if signals:
                            for signal in signals:
                                # Skip if already holding this symbol
                                if signal.symbol in self.state.state.open_positions:
                                    log.debug(f"StatArb skip {signal.symbol} — already in position")
                                    continue
                                cmp = self._get_current_price(signal.symbol) or float(
                                    self._candle_data[signal.symbol]["close"].iloc[-1]
                                )
                                _sa_# Use India VIX from state
                                _sa_vix = float(_sa_vix_df.iloc[-1]["close"]) if not _sa_vix_df.empty else None
                                risk_result = self.risk.evaluate(signal, cmp=cmp, vix=_sa_vix)
                                await state_mgr.save_signal({
                                    "symbol": signal.symbol, "side": signal.side,
                                    "confidence": signal.confidence, "strategy": "StatArb",
                                    "acted_on": risk_result.approved,
                                    "blocked_reason": risk_result.blocked_reason,
                                })
                                if risk_result.approved:
                                    # G1-FIX-F4: StatArb bypasses Kelly sizing,
                                    # so we must add an explicit margin guard.
                                    # Without this, a pair trade can exceed capital
                                    # if Kelly sizer is not called first.
                                    _sa_required = cmp * risk_result.recommended_qty
                                    _sa_available = self.state.state.available_margin
                                    if _sa_available < _sa_required:
                                        log.warning(
                                            f"[G1-F4] StatArb {signal.symbol} SKIPPED — "
                                            f"need ₹{_sa_required:.0f} but only ₹{_sa_available:.0f} available"
                                        )
                                        continue
                                    await self.broker.place_order(
                                        symbol=signal.symbol, side=signal.side,
                                        qty=risk_result.recommended_qty, cmp=cmp,
                                        strategy="StatArb",
                                        stop_loss=risk_result.stop_loss,
                                        target=risk_result.target,
                                        confidence=signal.confidence,
                                    )
            except Exception as e:
                log.debug(f"StatArb loop: {e}")
            await asyncio.sleep(300)

    # ── ML RETRAIN LOOP ───────────────────────────────────────────
    async def _candle_refresh_loop(self):
        """
        PATCH11 CRITICAL FIX — Root cause of zero trades.
        
        The strategy cycle runs on _candle_data and _intraday_data that were
        downloaded ONCE at startup. Without fresh candles, EMA crossovers and
        other technical signals never fire during the day.
        
        This loop refreshes 5m intraday candles every 5 minutes so that
        strategies see live market conditions. Daily candles are refreshed once
        at session open to pick up overnight gaps.
        """
        NON_TRADEABLE = {"^NSEI","^NSEBANK","^CNXIT","^VIX","^SENSEX","^BSESN","^NIFTYIT"}
        _daily_refreshed_today = None
        INTRADAY_REFRESH_SECS = 300  # refresh 5m candles every 5 minutes
        log.info("Candle refresh loop started — intraday data will update every 5 min")
        await asyncio.sleep(60)  # wait for initial load to complete first

        while self._running:
            try:
                sess = session_status()
                if not sess["is_market_hours"]:
                    await asyncio.sleep(60)
                    continue

                today = datetime.now().date()

                # ── Daily refresh once per session open ──
                if _daily_refreshed_today != today:
                    log.info("[CandleRefresh] Refreshing daily candles for session open...")
                    for sym in list(self._candle_data.keys()):
                        try:
                            # BUG FIX: Skip ^VIX (US VIX) during refresh — we use ^INDIAVIX
                            # Downloading US ^VIX overwrites India VIX data, causing false CRISIS
                            if sym == "^VIX":
                                continue
                            df = await asyncio.get_running_loop().run_in_executor(
                                None, lambda s=sym: self.hist_feed.download(s, interval="1d", period="2y")
                            )
                            if df is not None and not df.empty:
                                df = self.indicator_engine.add_all(df)
                                if len(df) > 500:
                                    df = df.tail(500).copy()
                                self._candle_data[sym] = df
                        except Exception as e:
                            log.debug(f"[CandleRefresh] daily {sym}: {e}")

                    # Refresh ^INDIAVIX separately and store under ^VIX key
                    try:
                        india_vix_df = await asyncio.get_running_loop().run_in_executor(
                            None, lambda: self.hist_feed.download("^INDIAVIX", interval="1d", period="1y")
                        )
                        if india_vix_df is not None and not india_vix_df.empty:
                            self._candle_data["^VIX"] = india_vix_df
                            vix_val = float(india_vix_df["Close"].dropna().iloc[-1])
                            self.state.state.market_data["india_vix"] = vix_val
                            log.info(f"[CandleRefresh] India VIX refreshed: {vix_val:.1f}")
                    except Exception as e:
                        log.debug(f"[CandleRefresh] ^INDIAVIX refresh: {e}")

                    _daily_refreshed_today = today
                    log.info("[CandleRefresh] Daily candles refreshed")
                    # G1: Invalidate correlation cache after new daily data
                    portfolio_optimizer.invalidate_cache()

                # ── Intraday 5m refresh — every 5 min ──
                # FIX: In S-Mode, prefer Shoonya historical API over Yahoo Finance
                # Shoonya gives real NSE data with no rate-limit issues
                _use_shoonya_hist = (
                    cfg.is_smode and
                    hasattr(self.broker, '_shoonya') and
                    getattr(self.broker._shoonya, 'connected', False) and
                    hasattr(self.broker._shoonya, 'get_historical_data')
                )
                tradeable = [s for s in self._candle_data if s not in NON_TRADEABLE]
                refreshed = 0
                for sym in tradeable:
                    try:
                        df_5m = None
                        if _use_shoonya_hist:
                            try:
                                df_5m = await asyncio.get_running_loop().run_in_executor(
                                    None, lambda s=sym: self.broker._shoonya.get_historical_data(s, "5m", "2d")
                                )
                            except Exception as _sh_e:
                                log.debug(f"[CandleRefresh] Shoonya hist failed for {sym}: {_sh_e}")
                                df_5m = None
                        if df_5m is None or df_5m.empty:
                            df_5m = await asyncio.get_running_loop().run_in_executor(
                                None, lambda s=sym: self.hist_feed.download(s, interval="5m", period="2d")
                            )
                        if df_5m is not None and not df_5m.empty:
                            df_5m = self.indicator_engine.add_all(df_5m)
                            # G1-FIX-F2: Keep only last 500 intraday candles
                            # (500 × 5min = ~41 hours of market data, more than enough)
                            if len(df_5m) > 500:
                                df_5m = df_5m.tail(500).copy()
                            self._intraday_data[sym] = df_5m
                            refreshed += 1
                    except Exception as e:
                        log.debug(f"[CandleRefresh] 5m {sym}: {e}")
                    # small sleep to avoid hammering Yahoo Finance
                    await asyncio.sleep(0.3)

                log.info(f"[CandleRefresh] ✅ {refreshed}/{len(tradeable)} intraday candles refreshed")
                await asyncio.sleep(INTRADAY_REFRESH_SECS)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[CandleRefresh] Error: {e}")
                await asyncio.sleep(60)

    async def _ml_retrain_loop(self):
        """
        - Incremental retrain: every 50 trades (fast, uses last 6 months)
        - Monthly retrain: last trading day of each month at 3:30 PM IST (after market close)
        - Weekly retrain: fallback every 7 days
        """
        last_full_retrain = datetime.now()
        last_monthly_month = None
        while self._running:
            try:
                now = now_ist()

                # Incremental: every 50 trades
                if self._trades_since_retrain >= self._retrain_threshold:
                    log.info(f"ML: {self._trades_since_retrain} trades → incremental retrain")
                    self._trades_since_retrain = 0
                    from models.trainer import ModelTrainer
                    trainer = ModelTrainer()
                    for sym, df in list(self._candle_data.items())[:3]:
                        if len(df) > 100:
                            await asyncio.get_running_loop().run_in_executor(
                                None, trainer.incremental_retrain, df, sym.replace(".NS", "")
                            )
                    self.predictor._load_models()
                    log.info("✅ Incremental retrain complete")
                    await alerter.ml_retrained(n_trades=50, full=False)

                # Monthly retrain: 3:30 PM on last trading day of month
                import calendar
                last_day = calendar.monthrange(now.year, now.month)[1]
                is_last_week = now.day >= last_day - 6  # within last week of month
                is_retrain_time = now.hour == 15 and now.minute == 30
                is_new_month = now.month != last_monthly_month
                if is_last_week and is_retrain_time and is_new_month:
                    log.info("📅 Monthly full ML retrain starting (end of month, after market close)...")
                    await self._train_ml_models()
                    last_monthly_month = now.month
                    last_full_retrain = datetime.now()
                    await alerter.ml_retrained(n_trades=0, full=True)
                    log.info("✅ Monthly retrain complete")

                # Weekly fallback
                elif (datetime.now() - last_full_retrain).days >= cfg.ml_retrain_interval_days:
                    log.info("ML: Weekly fallback full retrain starting...")
                    await self._train_ml_models()
                    last_full_retrain = datetime.now()

            except Exception as e:
                log.error(f"ML retrain loop: {e}")
            await asyncio.sleep(60)  # Check every minute (was 10 min — too coarse for 3:30 PM hit)

    # ── DAILY RESET ───────────────────────────────────────────────
    async def _daily_reset_loop(self):
        """
        Reset daily counters at 9:00 AM each trading day.
        P16 FIX: Also fires EOD report at 15:30 IST (was missing).
        """
        last_reset_date = None
        last_report_date = None
        while self._running:
            now = now_ist()
            today = now.date()

            # Morning reset at 9:00 AM
            if now.hour == 9 and now.minute == 0 and today != last_reset_date:
                self.state.reset_daily()
                if hasattr(self.broker, 'reset_daily'):
                    self.broker.reset_daily()
                last_reset_date = today
                log.info("Daily reset complete")

                # Send startup notification (P16)
                try:
                    summary = self.state.get_summary()
                    await alerter.startup_notification(
                        capital=summary.get("capital", cfg.initial_capital),
                        mode=cfg.mode,
                        strategies_count=len(self.strategies) + 1,
                        symbols_count=len(cfg.symbols),
                    )
                except Exception as _e:
                    log.debug(f"Startup notification failed: {_e}")

            # P16: EOD report at 15:30 IST — was never firing in _daily_report_loop
            if now.hour == 15 and 30 <= now.minute <= 31 and today != last_report_date:
                try:
                    summary = self.state.get_summary()
                    risk_summary = self.risk.get_portfolio_risk()
                    model_info = self.predictor.get_model_info()
                    await alerter.daily_report({
                        **summary, **risk_summary,
                        "ml_models": model_info.get("models", {}),
                    })
                    last_report_date = today
                    log.info("EOD daily report sent")
                except Exception as _e:
                    log.error(f"EOD daily report failed: {_e}")

            await asyncio.sleep(55)

    # ── EVENT HANDLERS ────────────────────────────────────────────
    async def _on_order_filled(self, data: dict):
        costs = data.get("costs", {})
        sym = data.get("symbol", "")
        side = data.get("side", "")
        qty = data.get("qty", 0)
        fill_price = data.get("fill_price", 0)
        total_cost = costs.get("total", 0)

        await alerter.trade_filled(sym, side, qty, fill_price, total_cost, stop_loss=data.get("stop_loss",0) or 0, target=data.get("target",0) or 0, confidence=data.get("confidence",0) or 0)

        # Save trade to DB
        await state_mgr.save_trade({
            "symbol": sym,
            "side": side,
            "qty": int(qty),
            "entry_price": float(fill_price),
            "entry_time": datetime.now(),
            "brokerage": float(costs.get("brokerage", 0) or 0),
            "stt": float(costs.get("stt", 0) or 0),
            "other_costs": float(costs.get("total", 0) or 0),
            "mode": cfg.mode,
            "order_id": data.get("order_id", ""),
            "strategy": data.get("strategy", ""),
            "status": "OPEN",
        })

        # Update open positions in state — FIX: include confidence, open_price
        if side == "BUY":
            # ── BUG-FIX: Detect if BUY is CLOSING an existing SHORT ──────────
            existing = self.state.state.open_positions.get(sym)
            is_closing_short = existing is not None and existing.get("side") == "SHORT"

            if is_closing_short:
                pos = existing
                entry_price = pos.get("avg_price", fill_price)
                gross_pnl = (entry_price - fill_price) * qty   # profit when price falls
                pnl = gross_pnl - total_cost
                locked = pos.get("short_margin_locked", qty * fill_price * 0.30)
                # Return locked margin + realized P&L
                self.state.state.available_margin += locked + pnl
                # Save closed SHORT trade record
                await state_mgr.save_trade({
                    "symbol": sym,
                    "side": "SHORT",           # position direction for history display
                    "qty": int(qty),
                    "entry_price": float(entry_price),
                    "exit_price": float(fill_price),
                    "entry_time": pos.get("opened_at") or datetime.now(),
                    "exit_time": datetime.now(),
                    "gross_pnl": round(gross_pnl, 2),
                    "net_pnl": round(pnl, 2),
                    "brokerage": round(total_cost, 2),
                    "strategy": pos.get("strategy", data.get("strategy", "")),
                    "confidence": float(pos.get("confidence", 0) or 0),
                    "stop_loss": float(pos.get("stop_loss", 0) or 0),
                    "target": float(pos.get("target", 0) or 0),
                    "status": "CLOSED",
                    "mode": cfg.mode,
                })
                # BUG-FIX: Update PnL directly so it is never skipped even when
                # risk engine is stubbed in tests or otherwise not available.
                self.state.state.update_pnl(pnl)
                # risk.update_after_trade handles wins/losses/consecutive — do NOT
                # duplicate the increment here to avoid double-counting.
                self.risk.update_after_trade(pnl)
                del self.state.state.open_positions[sym]
                self._pending_symbols.discard(sym)
            else:
                # Opening a new LONG position
                self.state.state.open_positions[sym] = {
                    "qty":               qty,
                    "avg_price":         fill_price,
                    "open_price":        fill_price,   # price at open (for day reference)
                    "side":              "LONG",        # position direction (not order side)
                    "stop_loss":         data.get("stop_loss"),
                    "target":            data.get("target"),
                    "strategy":          data.get("strategy", ""),
                    "confidence":        data.get("confidence", 0),
                    "current_price":     fill_price,
                    "unrealized_pnl":    0.0,
                    "mode":              cfg.mode,
                    "opened_at":         datetime.now().isoformat(),
                }
                self._pending_symbols.discard(sym)  # Fill confirmed — remove from pending
                self.state.state.available_margin -= (qty * fill_price + total_cost)
                # Note: daily_trades already incremented when order was placed in strategy cycle
        elif side == "SELL":
            if sym in self.state.state.open_positions:
                # Closing an existing LONG position
                pos = self.state.state.open_positions.get(sym, {})
                entry_price = pos.get("avg_price", fill_price)
                gross_pnl = (fill_price - entry_price) * qty
                pnl = gross_pnl - total_cost
                # Save closed trade record to history — P14: include all fields
                # BUG-FIX: save side="LONG" (position direction) not "SELL" (order direction)
                # so trade history tab shows "▲ BUY/LONG" not "▼ SHORT"
                await state_mgr.save_trade({
                    "symbol": sym,
                    "side": "LONG",            # position direction for history display
                    "qty": int(qty),
                    "entry_price": float(entry_price),
                    "exit_price": float(fill_price),
                    "entry_time": pos.get("opened_at") or datetime.now(),
                    "exit_time": datetime.now(),
                    "gross_pnl": round(gross_pnl, 2),
                    "net_pnl": round(pnl, 2),
                    "brokerage": round(total_cost, 2),
                    "strategy": pos.get("strategy", data.get("strategy", "")),
                    "confidence": float(pos.get("confidence", 0) or 0),
                    "stop_loss": float(pos.get("stop_loss", 0) or 0),
                    "target": float(pos.get("target", 0) or 0),
                    "status": "CLOSED",
                    "mode": cfg.mode,
                })
                # BUG-FIX: Update PnL directly so it is never skipped even when
                # risk engine is stubbed in tests or otherwise not available.
                self.state.state.update_pnl(pnl)
                # risk.update_after_trade handles wins/losses/consecutive — do NOT
                # duplicate the increment here to avoid double-counting.
                self.risk.update_after_trade(pnl)
                del self.state.state.open_positions[sym]
                self._pending_symbols.discard(sym)
                # Return entry margin (entry_price × qty) + realized P&L
                self.state.state.available_margin += (entry_price * qty - total_cost) + pnl
                should_retrain = self.predictor.record_trade_outcome(sym, pnl) if hasattr(self, 'predictor') else False
                if should_retrain:
                    self._trades_since_retrain = self._retrain_threshold
                # P16: Fire trade_closed Telegram alert (was missing)
                try:
                    await alerter.trade_closed(
                        symbol=sym,
                        pnl=gross_pnl,
                        net_pnl=pnl,
                        strategy=pos.get("strategy", data.get("strategy", "Unknown")),
                        entry_price=float(entry_price),
                        exit_price=float(fill_price),
                        qty=int(qty),
                        brokerage=round(total_cost, 2),
                    )
                except Exception as _te:
                    log.debug(f"trade_closed alert failed: {_te}")
            else:
                # Opening a new SHORT — deduct 30% SPAN margin (matches PaperBroker)
                short_margin = qty * fill_price * 0.30 + total_cost
                self.state.state.open_positions[sym] = {
                    "qty":               qty,
                    "avg_price":         fill_price,
                    "open_price":        fill_price,
                    "side":              "SHORT",
                    "stop_loss":         data.get("stop_loss"),
                    "target":            data.get("target"),
                    "strategy":          data.get("strategy", ""),
                    "confidence":        data.get("confidence", 0),
                    "current_price":     fill_price,
                    "unrealized_pnl":    0.0,
                    "mode":              cfg.mode,
                    "opened_at":         datetime.now().isoformat(),
                    "short_margin_locked": short_margin,
                }
                self._pending_symbols.discard(sym)
                self.state.state.available_margin -= short_margin

    async def _on_stop_hit(self, data: dict):
        sym = data.get("symbol", "")
        price = data.get("price", 0)
        stop = data.get("stop", 0)
        log.warning(f"STOP HIT: {sym} @ ₹{price:.2f} (stop was ₹{stop:.2f})")
        await alerter.risk_alert(f"Stop hit: {sym} @ ₹{price:.2f}")
        await state_mgr.save_risk_event("STOP_HIT", f"{sym} stop hit @ {price}", sym, "HIGH")

    async def _on_target_hit(self, data: dict):
        sym = data.get("symbol", "")
        price = data.get("price", 0)
        log.info(f"TARGET HIT: {sym} @ ₹{price:.2f}")
        await alerter.send(f"🎯 Target hit: {sym} @ ₹{price:.2f}", priority="HIGH")

    async def _on_risk_breach(self, data: dict):
        await alerter.risk_alert(str(data))
        await state_mgr.save_risk_event("RISK_BREACH", str(data), severity="CRITICAL")

    async def _on_halt(self, data: dict):
        self._running = False
        await alerter.system_halted(str(data))

    async def _on_tick(self, tick: dict):
        """
        Update latest price in candle data on every tick.
        [E4] If price moves >= 3% vs last known price, queue an immediate
        strategy scan for that symbol — don't wait 60 seconds.

        M4 FIX: _last_tick_price (engine) and rt_feed._last_prices previously
        could desync. Now _on_tick is the single authoritative writer: it
        updates both so _get_current_price() and the spike detector always
        see the same value.
        """
        symbol = tick.get("symbol")
        price  = tick.get("ltp", 0)
        if not symbol or price <= 0:
            return

        # Update OHLC in both timeframe stores
        for store in (self._candle_data, self._intraday_data):
            if symbol in store:
                df = store[symbol]
                if not df.empty:
                    df.at[df.index[-1], "close"] = price
                    if price > float(df.iloc[-1].get("high", price)):
                        df.at[df.index[-1], "high"] = price
                    if price < float(df.iloc[-1].get("low", price)):
                        df.at[df.index[-1], "low"] = price

        # M4: Sync rt_feed price cache so _get_current_price() is consistent
        # AngelOneRealtimeFeed uses _prices; PaperRealtimeFeed uses _prices too
        price_cache = getattr(self.rt_feed, '_prices', None) or getattr(self.rt_feed, '_last_prices', None)
        if price_cache is not None:
            price_cache[symbol] = price

        # [H4] VIX gate fix: write live india_vix to state so risk Gate 7 reads
        # a real value instead of the hardcoded 15.0 default.
        if symbol == "^VIX":
            self.state.state.market_data["india_vix"] = price
            log.debug(f"VIX state updated: {price:.1f}")

        # [E4] Spike detect: compare against last known price
        last_price = self._last_tick_price.get(symbol)
        if last_price and last_price > 0:
            move_pct = abs(price - last_price) / last_price * 100
            if move_pct >= _TICK_SPIKE_PCT:
                direction = "UP" if price > last_price else "DOWN"
                log.info(
                    f"[E4] Tick spike | {symbol} | {last_price:.2f} -> {price:.2f} "
                    f"({move_pct:.1f}% {direction}) — queuing urgent scan"
                )
                await self._urgent_scan_queue.put({
                    "sym": symbol,
                    "reason": f"tick_spike_{direction.lower()}_{move_pct:.1f}pct",
                })
        self._last_tick_price[symbol] = price

        # Update live position CMP and unrealized P&L on every tick
        if symbol in self.state.state.open_positions:
            pos = self.state.state.open_positions[symbol]
            pos["current_price"] = price
            avg = pos.get("avg_price", price)
            qty = pos.get("qty", 0)
            side = pos.get("side", "LONG")
            if side == "SHORT":
                pos["unrealized_pnl"] = round((avg - price) * qty, 2)
            else:
                pos["unrealized_pnl"] = round((price - avg) * qty, 2)
    # ── BACKGROUND TASKS ──────────────────────────────────────────
    async def _realtime_feed_loop(self):
        """Keep the realtime price feed alive — restart on any error."""
        while self._running:
            try:
                await self.rt_feed.start()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Realtime feed error: {e} — restarting in 30s")
                await asyncio.sleep(30)

    async def _state_save_loop(self):
        while self._running:
            try:
                await self.state.save()
            except Exception as e:
                log.error(f"State save error: {e}")
            await asyncio.sleep(cfg.state_save_interval)

    async def _watchdog_loop(self):
        """
        P5: Enhanced watchdog — checks drawdown every 60s.
        On breach:
          1. Logs CRITICAL
          2. Halts bot (sets is_halted=True, blocks all new trades)
          3. Sends Telegram alert
          4. Closes all open positions (emergency exit)
        """
        while self._running:
            try:
                await asyncio.sleep(60)
                ok, msg = self.drawdown_guard.check(self.state.state)
                if not ok and not self.state.state.is_halted:
                    log.critical(f"🛑 DRAWDOWN BREACH: {msg}")
                    # Step 1: Halt immediately
                    self.halt(f"Max drawdown breached: {msg}")
                    # Step 2: Alert
                    try:
                        from alerts.telegram_bot import alerter
                        await alerter.send(
                            f"🛑 ZeroBot HALTED — Drawdown breach!\n{msg}\n"
                            f"Capital: ₹{self.state.state.total_capital:,.0f}\n"
                            f"Open positions being closed...",
                            priority="CRITICAL", alert_type="halt"
                        )
                    except Exception:
                        pass
                    # Step 3: Emergency exit all open positions
                    open_syms = list(self.state.state.open_positions.keys())
                    if open_syms:
                        log.critical(f"Emergency closing {len(open_syms)} positions: {open_syms}")
                        for sym in open_syms:
                            try:
                                cmp = self._get_current_price(sym) or \
                                      self.state.state.open_positions[sym].get("avg_price", 0)
                                await self._emergency_exit(sym, reason="drawdown_breach")
                            except Exception as ex:
                                log.error(f"Emergency exit {sym} failed: {ex}")
                    log.critical(f"All positions closed after drawdown breach. Bot halted.")

                # Also check if we're already halted and log status
                elif self.state.state.is_halted:
                    log.debug(f"Bot halted — reason: {self.state.state.halted_reason}. "
                              f"Call /api/resume to restart.")

                # Update available margin from broker
                if hasattr(self.broker, 'get_portfolio_summary'):
                    try:
                        summary = self.broker.get_portfolio_summary()
                        if summary.get("available"):
                            self.state.state.available_margin = summary["available"]
                    except Exception:
                        pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Watchdog error: {e}")
                await asyncio.sleep(10)

    async def _telegram_polling_loop(self):
        """P16: Poll Telegram for inbound commands (/halt /resume /status etc.)"""
        try:
            if hasattr(alerter, "cmd_handler"):
                await alerter.cmd_handler.start_polling()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug(f"Telegram polling loop: {e}")

    async def _save_feature_importances(self):
        """P16: Extract and persist feature importances from trained models."""
        try:
            import json, glob
            from pathlib import Path
            model_dir = Path(__file__).parent.parent / "models" / "saved"
            fi_data = {}
            for pkl_path in sorted(glob.glob(str(model_dir / "*.pkl")))[:6]:
                try:
                    import pickle
                    with open(pkl_path, "rb") as fh:
                        model = pickle.load(fh)
                    sym = Path(pkl_path).stem.split("_")[0]
                    if hasattr(model, "feature_importances_"):
                        fi = model.feature_importances_
                        feat_names = getattr(model, "feature_names_in_", None)
                        if feat_names is not None:
                            fi_dict = dict(sorted(
                                zip(feat_names, fi.tolist()),
                                key=lambda x: x[1], reverse=True
                            )[:15])
                        else:
                            fi_dict = {f"feature_{i}": float(v) for i, v in enumerate(fi[:15])}
                        fi_data[sym] = fi_dict
                        top5 = list(fi_dict.items())[:5]
                        log.info(f"[P16] Feature importance {sym}: {top5}")
                except Exception:
                    continue
            if fi_data:
                out_path = Path(__file__).parent.parent / "data" / "feature_importance.json"
                out_path.parent.mkdir(exist_ok=True)
                with open(out_path, "w") as fh:
                    json.dump(fi_data, fh, indent=2)
                log.info(f"[P16] Feature importances saved ({len(fi_data)} symbols)")
        except Exception as e:
            log.debug(f"[P16] _save_feature_importances: {e}")

    async def _daily_report_loop(self):
        # [G2-FIX] EOD report is now handled by the P16 inline block in
        # _session_loop() which has a last_report_date guard preventing double-fire.
        # This loop is kept as a stub to avoid breaking the task list at startup.
        while self._running:
            await asyncio.sleep(3600)

    def halt(self, reason: str = "Manual halt"):
        # Note: RiskEngine has no halt() method — we set state directly
        self._running = False
        self.state.state.status = "HALTED"
        self.state.state.is_halted = True
        log.critical(f"EMERGENCY HALT: {reason}")

    def resume(self):
        # Note: RiskEngine has no resume() method — we set state directly
        self._running = True
        self.state.state.status = "RUNNING"
        self.state.state.is_halted = False
        log.info("Bot RESUMED")

    async def _close_position_manual(self, symbol: str, cmp: float):
        """
        P4-FIX-EXIT: Clean manual exit called from dashboard API.
        Places a SELL/BUY order through broker AND guarantees state cleanup.
        Works for both stocks and options (options get direct state cleanup
        since Yahoo Finance can't provide real options LTP).
        """
        pos = self.state.state.open_positions.get(symbol)
        if not pos:
            # Also check broker
            broker_positions = self.broker.get_positions()
            if broker_positions is None:
                raise ValueError(f"Broker disconnected, cannot find position {symbol}")
            broker_pos = broker_positions.get(symbol)
            if not broker_pos:
                raise ValueError(f"Position {symbol} not found")
            pos = broker_pos

        qty = pos.get("qty", 0)
        side_str = (pos.get("side") or "BUY").upper()
        is_short = side_str in ("SHORT", "SELL")
        close_side = "BUY" if is_short else "SELL"
        entry = pos.get("avg_price", cmp)

        if qty > 0:
            try:
                order = await self.broker.place_order(
                    symbol=symbol, side=close_side, qty=qty, cmp=cmp,
                    strategy="ManualExit", confidence=100.0
                )
                log.info(f"Manual exit: {symbol} {close_side} {qty}@{cmp:.2f} | order={getattr(order,'order_id','?')}")
            except Exception as e:
                log.warning(f"Broker order failed for manual exit {symbol}: {e} — doing direct state cleanup")

        # Always clean state regardless of broker order status
        if symbol in self.state.state.open_positions:
            pnl = ((entry - cmp) if is_short else (cmp - entry)) * qty
            self.risk.update_after_trade(pnl)
            # Note: update_after_trade already calls state.update_pnl(pnl)
            # so we do NOT call daily_pnl/total_pnl again to avoid double-counting
            self.state.state.available_margin += qty * cmp
            # Save closed trade record
            import asyncio as _asyncio
            try:
                await state_mgr.save_trade({
                    "symbol": symbol,
                    "side": close_side,
                    "qty": int(qty),
                    "entry_price": float(entry),
                    "exit_price": float(cmp),
                    "entry_time": pos.get("opened_at") or datetime.now(),
                    "exit_time": datetime.now(),
                    "gross_pnl": round(((entry - cmp) if is_short else (cmp - entry)) * qty, 2),
                    "net_pnl": round(pnl, 2),
                    "brokerage": 0.0,
                    "strategy": pos.get("strategy", "ManualExit"),
                    "confidence": float(pos.get("confidence", 0) or 0),
                    "stop_loss": float(pos.get("stop_loss", 0) or 0),
                    "target": float(pos.get("target", 0) or 0),
                    "status": "CLOSED",
                    "mode": cfg.mode,
                })
            except Exception as _e:
                log.debug(f"save_trade for manual exit failed: {_e}")
            del self.state.state.open_positions[symbol]
            self._pending_symbols.discard(symbol)
            log.info(f"Manual exit complete: {symbol} PnL={pnl:+.2f}")
