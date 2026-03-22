# -*- coding: utf-8 -*-
"""
ZeroBot S-Mode — Shoonya Real-time Feed
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Replaces the Yahoo Finance 15-second polling feed with a real
Shoonya WebSocket feed for S-Mode operation.

Key differences vs PaperRealtimeFeed:
  • True push-based ticks (not poll-based)
  • Sub-second latency (not 15s delay)
  • Real bid/ask spread data
  • Automatic reconnect on disconnect
  • Falls back to PaperRealtimeFeed if Shoonya disconnects
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional, Callable
from core.config import cfg
from core.logger import log
from core.event_bus import bus


class ShoonyaRealtimeFeed:
    """
    Real-time market feed using Shoonya WebSocket.
    Used exclusively in S-Mode (real data + paper execution).

    Interface is identical to PaperRealtimeFeed so the engine
    can swap between the two with zero changes.
    """

    def __init__(self, shoonya_broker=None):
        """
        Args:
            shoonya_broker: A connected ShoonyaPaperBroker or ShounyaBroker instance.
                            If None, falls back to Yahoo Finance polling.
        """
        self._symbols = cfg.symbols
        self._broker = shoonya_broker
        self._running = False
        self._last_prices: Dict[str, float] = {}
        self._tick_count = 0
        self._fallback_active = False
        self._fallback_feed = None
        self._fallback_task = None   # single active Yahoo task handle

        # Set True the first time WS fails so the upgrade-retry loop is disabled.
        # NorenRestApiPy missing / session invalid won't self-heal during the session;
        # retrying every 30s just spawns duplicate PaperRealtimeFeed instances and
        # floods the log with the same ERROR + WARNING pair.
        self._ws_permanently_failed = False

        has_shoonya = (
            shoonya_broker is not None
            and getattr(shoonya_broker, "is_connected", False)
        )
        if has_shoonya:
            log.info(f"[S-MODE Feed] 📡 Shoonya WS feed — {len(self._symbols)} symbols (real ticks)")
        else:
            log.warning("[S-MODE Feed] ⚠️  Shoonya not connected — will use Yahoo Finance fallback")

    async def start(self):
        """
        Start the feed.
        - During market hours (9:15–15:30 IST): Shoonya WebSocket ticks
        - Outside market hours: Yahoo Finance polling (15s) — Shoonya has no data
        - If Shoonya not connected: Yahoo Finance polling always
        """
        self._running = True
        from core.clock import is_market_hours
        has_shoonya = (
            self._broker is not None
            and getattr(self._broker, "is_connected", False)
        )

        if has_shoonya and is_market_hours():
            log.info("[S-MODE Feed] 📡 Market hours — using Shoonya WebSocket ticks")
            await self._shoonya_feed_loop()
        elif has_shoonya:
            log.info("[S-MODE Feed] ⏰ Outside market hours — using Yahoo Finance (Shoonya WS inactive)")
            await self._start_fallback()
        else:
            await self._start_fallback()

    # Shoonya NSE scrip token table (from Shoonya master contract file)
    # Format: symbol_name → token_number
    # Download full list: https://api.shoonya.com/NSE_symbols.txt.zip
    _SHOONYA_TOKENS = {
        "RELIANCE": "2885",   "TCS": "11536",    "HDFCBANK": "1333",
        "INFY": "1594",       "ICICIBANK": "4963","SBIN": "3045",
        "AXISBANK": "5900",   "WIPRO": "3787",   "ITC": "1660",
        "KOTAKBANK": "1922",  "BAJFINANCE": "317","MARUTI": "10999",
        "HCLTECH": "7229",    "LT": "11483",     "HINDUNILVR": "356",
        "NESTLEIND": "17963", "TITAN": "3506",   "ASIANPAINT": "236",
        "BAJAJFINSV": "16675","TECHM": "13538",  "ULTRACEMCO": "11532",
        "TATASTEEL": "3499",  "ONGC": "2475",    "NTPC": "11630",
        "POWERGRID": "14977", "INDUSINDBK": "5258","BANDHANBNK": "2263",
        "NIFTY50": "26000",   "BANKNIFTY": "26009",
    }
    # Reverse map: token → clean symbol name (built at runtime)
    _TOKEN_TO_SYMBOL: dict = {}

    async def _shoonya_feed_loop(self):
        """Subscribe to Shoonya WS ticks and push to event bus."""
        log.info("[S-MODE Feed] 📡 Starting Shoonya WebSocket tick stream")

        # FIX: Shoonya subscribe() requires "NSE|TOKEN_NUMBER" not "NSE|SYMBOL_NAME"
        # Build token subscriptions and reverse lookup table
        ShoonyaRealtimeFeed._TOKEN_TO_SYMBOL = {}
        symbols_for_ws = []
        for sym in self._symbols:
            clean = sym.replace(".NS", "").replace(".BO", "").upper()
            token = ShoonyaRealtimeFeed._SHOONYA_TOKENS.get(clean)
            if token:
                nse_tok = f"NSE|{token}"
                symbols_for_ws.append(nse_tok)
                ShoonyaRealtimeFeed._TOKEN_TO_SYMBOL[token] = clean
                ShoonyaRealtimeFeed._TOKEN_TO_SYMBOL[nse_tok] = clean
            else:
                # Fallback: try to resolve via broker searchscrip
                try:
                    tok = self._broker._get_token(clean) if hasattr(self._broker, "_get_token") else None
                    if tok:
                        tok_num = tok.split("|", 1)[-1]
                        symbols_for_ws.append(f"NSE|{tok_num}")
                        ShoonyaRealtimeFeed._TOKEN_TO_SYMBOL[tok_num] = clean
                        ShoonyaRealtimeFeed._TOKEN_TO_SYMBOL[f"NSE|{tok_num}"] = clean
                    else:
                        # Last resort: use symbol name directly (may not work but won't crash)
                        symbols_for_ws.append(f"NSE|{clean}")
                        log.debug(f"[S-MODE Feed] No token found for {sym}, using name as fallback")
                except Exception:
                    symbols_for_ws.append(f"NSE|{clean}")

        log.info(f"[S-MODE Feed] 🔑 Resolved {len(ShoonyaRealtimeFeed._TOKEN_TO_SYMBOL)}/{len(self._symbols)} tokens")

        def _on_tick(tick: dict):
            raw_sym = tick.get("symbol", "")
            ltp = tick.get("ltp", 0.0) or tick.get("lp", 0.0)
            if not raw_sym or not ltp:
                return
            # FIX: Resolve token back to symbol name using reverse map
            # Shoonya sends token number as symbol in some tick formats
            sym = ShoonyaRealtimeFeed._TOKEN_TO_SYMBOL.get(raw_sym)
            if not sym:
                # Fallback: clean the raw symbol
                sym = raw_sym.split("-")[0].strip().upper()
            # Normalise symbol: "RELIANCE" → "RELIANCE.NS"
            ns_sym = sym if sym.endswith(".NS") else f"{sym}.NS"
            self._last_prices[ns_sym] = ltp
            self._tick_count += 1
            # FIX: _on_tick runs in Shoonya's background thread — must use publish_sync
            # Using async bus.publish() here would silently drop ticks
            bus.publish_sync("tick", {
                "symbol": ns_sym,
                "price": ltp,
                "ltp": ltp,
                "open": tick.get("open", ltp),
                "high": tick.get("high", ltp),
                "low": tick.get("low", ltp),
                "close": ltp,
                "volume": tick.get("volume", 0),
                "bid": tick.get("bid", ltp),
                "ask": tick.get("ask", ltp),
                "source": "shoonya_ws",
            })

        _FEED_TIMEOUT_SECS = 120   # If no tick arrives within 2 min → fallback
        _last_tick_time = time.time()
        _prev_tick_count = 0

        try:
            self._broker.subscribe_ticks(symbols_for_ws, _on_tick)
            log.info(f"[S-MODE Feed] ✅ Subscribed to {len(symbols_for_ws)} symbols via Shoonya WS")
            # Keep coroutine alive — WS runs in background thread
            while self._running:
                await asyncio.sleep(5)  # Check every 5 seconds
                now = time.time()

                # FIX: Feed timeout — track time since last tick, not tick_count==0
                # Handles: session expired mid-session, network drop, Shoonya server issue
                from core.clock import is_market_hours
                if is_market_hours():
                    if self._tick_count > _prev_tick_count:
                        # Ticks are flowing — update timestamp
                        _last_tick_time = now
                        _prev_tick_count = self._tick_count
                    elif now - _last_tick_time > _FEED_TIMEOUT_SECS:
                        log.warning(
                            f"[S-MODE Feed] ⏱️  No ticks for {int(now - _last_tick_time)}s — "
                            "Shoonya feed timed out. Switching to Yahoo Finance fallback."
                        )
                        self._ws_permanently_failed = True
                        await self._start_fallback()
                        return

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"[S-MODE Feed] Shoonya WS error: {e} — switching to fallback")
            # Mark permanently failed so _start_fallback won't retry the upgrade loop.
            self._ws_permanently_failed = True
            await self._start_fallback()

    async def _start_fallback(self):
        """
        Yahoo Finance polling fallback.

        Two modes:
          can_upgrade=True  — WS has never failed: poll Yahoo AND watch every 30s
                              for market open to upgrade back to Shoonya WS.
          can_upgrade=False — WS permanently failed: run Yahoo for the whole session,
                              no retries. One clean log line, no looping errors.
        """
        # Guard: if a Yahoo feed task is already alive don't start a second one.
        # This fires when _start_fallback is called after a WS error while a
        # previous PaperRealtimeFeed task is still running.
        if self._fallback_active and self._fallback_task and not self._fallback_task.done():
            log.debug("[S-MODE Feed] Fallback already running — skipping duplicate start")
            return

        log.warning("[S-MODE Feed] ⚠️  Using Yahoo Finance fallback (15s polling)")
        self._fallback_active = True

        has_shoonya = (
            self._broker is not None
            and getattr(self._broker, "is_connected", False)
        )
        can_upgrade = has_shoonya and not self._ws_permanently_failed

        from data.feeds.realtime_feed import PaperRealtimeFeed
        self._fallback_feed = PaperRealtimeFeed()

        if can_upgrade:
            # Run Yahoo in background; check every 30s for market open → upgrade
            from core.clock import is_market_hours
            self._fallback_task = asyncio.create_task(self._fallback_feed.start())
            try:
                while self._running:
                    await asyncio.sleep(30)
                    if is_market_hours() and not self._ws_permanently_failed:
                        log.info("[S-MODE Feed] 📡 Market opened — upgrading to Shoonya WebSocket ticks")
                        self._fallback_task.cancel()
                        self._fallback_active = False
                        await self._shoonya_feed_loop()
                        return
            except asyncio.CancelledError:
                self._fallback_task.cancel()
        else:
            # WS permanently unavailable — Yahoo for the rest of the session, no retries
            if self._ws_permanently_failed:
                log.info(
                    "[S-MODE Feed] ℹ️  WS unavailable — Yahoo Finance active for this session.\n"
                    "              Fix for next session: pip install NorenRestApiPy pyotp"
                )
            self._fallback_task = asyncio.create_task(self._fallback_feed.start())
            try:
                await self._fallback_task
            except asyncio.CancelledError:
                pass

    def stop(self):
        self._running = False
        if self._fallback_task and not self._fallback_task.done():
            self._fallback_task.cancel()
        if self._fallback_feed and hasattr(self._fallback_feed, "stop"):
            self._fallback_feed.stop()

    def get_last_price(self, symbol: str) -> Optional[float]:
        # Check our own cache first (Shoonya WS prices)
        p = self._last_prices.get(symbol)
        if p and p > 0:
            return p
        # Fall through to Yahoo fallback feed if active
        if self._fallback_feed and hasattr(self._fallback_feed, 'get_last_price'):
            p2 = self._fallback_feed.get_last_price(symbol)
            if p2 and p2 > 0:
                # Sync back into our cache
                self._last_prices[symbol] = p2
                return p2
        return None

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def is_fallback(self) -> bool:
        return self._fallback_active
