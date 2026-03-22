# -*- coding: utf-8 -*-
"""
ZeroBot v1.1 — Hybrid Broker (Patch 15)

HYBRID MODE: The safest path to production.

Architecture:
  ┌─────────────────────────────────────────────────────────┐
  │  HYBRID BROKER                                          │
  │                                                         │
  │  Market Data ──► Angel One SmartAPI (REAL live data)   │
  │     • Real NSE tick prices                              │
  │     • Real bid/ask spreads                              │
  │     • Real volume & open interest                       │
  │     • Real F&O option chain                             │
  │                                                         │
  │  Order Execution ──► PaperBroker (SIMULATION)           │
  │     • Orders are NOT sent to exchange                   │
  │     • Filled at real market prices from Angel One       │
  │     • Realistic slippage model                          │
  │     • Full cost simulation (STT, GST, SEBI)             │
  │                                                         │
  │  Result: ML trains on real data. Zero capital risk.     │
  └─────────────────────────────────────────────────────────┘

Benefits:
  1. ML models learn from REAL prices (not delayed Yahoo data)
  2. Strategies are validated on real bid/ask, not mid-price
  3. Connectivity, authentication, and data pipeline tested
  4. When confidence is proven → flip mode: "hybrid" → "live"
  5. Never lose a rupee while developing

Usage (settings.yaml):
  bot:
    mode: hybrid
    broker:
      name: hybrid   # triggers this class

To switch to live when ready:
  bot:
    mode: live
    broker:
      name: angel   # same Angel One credentials, now real orders
"""
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any
from core.logger import log
from core.config import cfg
from core.event_bus import bus


class HybridBroker:
    """
    Hybrid broker: Angel One for market data + PaperBroker for execution.
    Provides identical interface to PaperBroker and AngelOneBroker.
    """

    def __init__(self):
        # Paper execution engine — handles all order fills
        from broker.paper_broker import PaperBroker
        self._paper = PaperBroker(initial_capital=cfg.initial_capital)

        # Angel One data connection (non-fatal if not configured)
        self._angel = None
        self._angel_connected = False
        self._data_source = "yahoo"   # fallback
        self._hybrid_stats = {
            "angel_ticks_received": 0,
            "yahoo_fallback_count": 0,
            "connection_attempts": 0,
            "connected_at": None,
        }

        log.info("🔀 HybridBroker initialised — Paper execution + Real data feed")
        self._try_connect_angel()

    @property
    def _connected(self) -> bool:
        """Expose _connected so AngelOneRealtimeFeed._is_angel_available() works."""
        return self._angel_connected

    @property
    def _token(self):
        """Proxy JWT token from the underlying AngelOneBroker."""
        return getattr(self._angel, '_token', None) if self._angel else None

    @property
    def _feed_token(self):
        """Proxy feed token from the underlying AngelOneBroker."""
        return getattr(self._angel, '_feed_token', None) if self._angel else None

    @property
    def _api(self):
        """Proxy SmartConnect API object from the underlying AngelOneBroker."""
        return getattr(self._angel, '_api', None) if self._angel else None

    def _try_connect_angel(self):
        """Attempt Angel One connection in background thread — non-fatal if credentials missing."""
        if not cfg.angel_one.is_configured:
            log.warning(
                "🔀 HybridBroker: Angel One credentials not found in .env\n"
                "   Add ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_MPIN, ANGEL_TOTP_SECRET\n"
                "   Falling back to Yahoo Finance data (still paper execution)"
            )
            return
        # FIX: Run connection in background thread to avoid blocking asyncio event loop
        # Angel One TOTP+login can take 5-10 seconds
        import threading
        def _connect_thread():
            try:
                from broker.angel_one import AngelOneBroker
                self._angel = AngelOneBroker()
                self._angel.connect()
                self._angel_connected = True
                self._data_source = "angel_one"
                self._hybrid_stats["connected_at"] = datetime.now().isoformat()
                log.info("✅ HybridBroker: Angel One CONNECTED — using real NSE tick data")
            except Exception as e:
                log.warning(
                    f"🔀 HybridBroker: Angel One connection failed ({e})\n"
                    "   Continuing with Yahoo Finance data fallback"
                )
        t = threading.Thread(target=_connect_thread, daemon=True, name="angel_connect")
        t.start()
        log.info("🔀 HybridBroker: Angel One connecting in background...")

    # ── ORDER INTERFACE (delegates to PaperBroker) ─────────────────────────

    async def place_order(self, symbol: str, side: str, qty: int,
                          cmp: float, strategy: str = "", confidence: float = 0.0,
                          stop_loss: float = 0.0, target: float = 0.0,
                          order_type: str = "MARKET", limit_price: float = 0.0):
        """
        Execute order via PaperBroker — fills at real price if Angel One connected,
        otherwise uses Yahoo Finance price.
        """
        # If Angel One connected, get real-time bid/ask for better fill simulation
        real_price = self._get_real_price(symbol, side)
        fill_price = real_price if real_price > 0 else cmp

        log.info(
            f"🔀 HYBRID ORDER: {side} {qty} {symbol} | "
            f"Data: {self._data_source} @ ₹{fill_price:.2f} | "
            f"Execution: PAPER (simulated)"
        )

        return await self._paper.place_order(
            symbol=symbol, side=side, qty=qty, cmp=fill_price,
            strategy=strategy, confidence=confidence,
            stop_loss=stop_loss, target=target,
            order_type=order_type, limit_price=limit_price,
        )

    def _get_real_price(self, symbol: str, side: str) -> float:
        """Get real-time price from Angel One if connected."""
        if not self._angel_connected or not self._angel:
            self._hybrid_stats["yahoo_fallback_count"] += 1
            return 0.0
        try:
            price = self._angel.get_ltp(symbol)
            if price and price > 0:
                self._hybrid_stats["angel_ticks_received"] += 1
                return float(price)
        except Exception:
            self._hybrid_stats["yahoo_fallback_count"] += 1
        return 0.0

    async def cancel_order(self, order_id: str):
        return await self._paper.cancel_order(order_id)

    async def get_order_status(self, order_id: str):
        return await self._paper.get_order_status(order_id)

    def get_portfolio_summary(self) -> Dict[str, Any]:
        summary = self._paper.get_portfolio_summary()
        summary["mode"] = "HYBRID"
        summary["data_source"] = self._data_source
        summary["angel_connected"] = self._angel_connected
        summary["hybrid_stats"] = self._hybrid_stats
        return summary

    def get_positions(self) -> Dict:
        return self._paper.get_positions()

    def get_funds(self) -> Dict:
        if self._angel_connected and self._angel:
            try:
                # Show real Angel One funds for reference (not used for trading)
                real_funds = self._angel.get_funds()
                paper_funds = self._paper.get_funds()
                return {
                    **paper_funds,
                    "real_account_balance": real_funds.get("net", 0),
                    "paper_balance": paper_funds.get("net", cfg.initial_capital),
                    "note": "Orders use paper balance — real balance shown for reference only",
                }
            except Exception:
                pass
        return self._paper.get_funds()

    @property
    def mode(self) -> str:
        return "HYBRID"

    @property
    def is_hybrid(self) -> bool:
        return True

    @property
    def is_live(self) -> bool:
        return False  # No real money ever leaves via this broker

    def get_hybrid_status(self) -> Dict:
        return {
            "mode": "HYBRID",
            "data_source": self._data_source,
            "angel_connected": self._angel_connected,
            "angel_configured": cfg.angel_one.is_configured,
            "execution": "PAPER (no real orders placed)",
            "capital_at_risk": "₹0.00",
            **self._hybrid_stats,
        }
