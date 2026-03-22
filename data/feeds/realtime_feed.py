# -*- coding: utf-8 -*-
"""
ZeroBot G2 — Realtime Feed Dispatcher
Clean per-mode feeds, no silent fallbacks.

PaperRealtimeFeed      : Yahoo Finance polling (P-mode only)
AngelOneRealtimeFeed   : Angel One SmartWebSocketV2 (A-paper, A-live, Dual)
ShoonyaRealtimeFeed    : Shoonya NorenRestApiPy WebSocket (S-paper, S-live)
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Dict, Optional

from core.config import cfg
from core.logger import log
from core.event_bus import bus


# ─── NSE Token map (symbol → Angel One token string) ──────────────────────
ANGEL_TOKENS: Dict[str, str] = {
    "RELIANCE": "2885", "HDFCBANK": "1333", "ICICIBANK": "4963",
    "TCS": "11536", "INFY": "1594", "SBIN": "3045", "AXISBANK": "5900",
    "WIPRO": "3787", "ITC": "1660", "LT": "11483", "KOTAKBANK": "1922",
    "BAJFINANCE": "317", "MARUTI": "10999", "HCLTECH": "7229",
    "NESTLEIND": "17963", "ADANIENT": "25", "SUNPHARMA": "3351",
    "TATAMOTORS": "3456", "ULTRACEMCO": "11532", "POWERGRID": "14977",
    "NTPC": "11630", "BAJAJFINSV": "16675", "TITAN": "3506",
    "BHARTIARTL": "10604", "HINDUNILVR": "1394", "ASIANPAINT": "236",
    "INDUSINDBK": "5258", "BANDHANBNK": "2263", "TECHM": "13538",
    "TATASTEEL": "3499", "ONGC": "2475", "JSWSTEEL": "11723",
    "COALINDIA": "1023", "DRREDDY": "881", "CIPLA": "694",
    "BRITANNIA": "547", "EICHERMOT": "910", "TATACONSUM": "3432",
    "APOLLOHOSP": "157", "M&M": "2031",
    # Indices (data only, cannot be traded)
    "NIFTY50": "26000", "BANKNIFTY": "26009",
}

# Shoonya token format: 'NSE|TOKEN'
SHOONYA_TOKENS: Dict[str, str] = {
    sym: f"NSE|{tok}" for sym, tok in ANGEL_TOKENS.items()
    if not sym.startswith("NIFTY") and not sym.startswith("BANK")
}
# Add indices with Shoonya format
SHOONYA_TOKENS["NIFTY50"] = "NSE|26000"
SHOONYA_TOKENS["BANKNIFTY"] = "NSE|26009"


def _clean_symbol(sym: str) -> str:
    """RELIANCE.NS → RELIANCE"""
    return sym.replace(".NS", "").replace("^", "").upper()


# ─────────────────────────────────────────────────────────────────────────────
# P-MODE: Yahoo Finance polling
# ─────────────────────────────────────────────────────────────────────────────
class PaperRealtimeFeed:
    """Yahoo Finance 15-second polling. P-mode only."""

    def __init__(self):
        self._symbols = cfg.symbols
        self._prices: Dict[str, float] = {}
        self._running = False
        log.info(f"[P-MODE Feed] Yahoo Finance polling — {len(self._symbols)} symbols")

    async def start(self):
        self._running = True
        log.info("[P-MODE Feed] Started — polling Yahoo Finance every 15s")
        await self._poll_loop()

    async def _poll_loop(self):
        while self._running:
            try:
                await self._fetch_batch()
            except Exception as e:
                log.debug(f"[P-MODE Feed] Yahoo poll error: {e}")
            await asyncio.sleep(15)

    async def _fetch_batch(self):
        import yfinance as yf
        loop = asyncio.get_event_loop()
        syms = [s if s.endswith(".NS") or s.startswith("^") else s + ".NS"
                for s in self._symbols if not s.startswith("^")][:20]
        if not syms:
            return
        tickers = await loop.run_in_executor(None, lambda: yf.download(
            syms, period="1d", interval="1m", progress=False, auto_adjust=True
        ))
        for sym in syms:
            raw = sym.replace(".NS", "")
            try:
                if "Close" in tickers.columns:
                    val = float(tickers["Close"][sym].dropna().iloc[-1])
                else:
                    val = float(tickers["Close"].dropna().iloc[-1])
                if val > 0:
                    self._prices[raw] = val
                    await bus.publish("tick", {
                        "symbol": raw, "ltp": val, "source": "yahoo_finance",
                        "timestamp": datetime.now().isoformat()
                    })
            except Exception:
                pass

    def get_last_price(self, symbol: str) -> Optional[float]:
        return self._prices.get(_clean_symbol(symbol))



    def stop(self):
        self._running = False


# ─────────────────────────────────────────────────────────────────────────────
# ANGEL ONE WebSocket Feed (A-paper, A-live, Dual)
# API: SmartWebSocketV2 — connect() takes NO args; subscribe() in on_open
# ─────────────────────────────────────────────────────────────────────────────
class AngelOneRealtimeFeed:
    """
    Angel One SmartWebSocketV2 real-time tick feed.
    Used in A-paper, A-live, and Dual modes.

    Correct API (from https://github.com/angel-one/smartapi-python):
      sws = SmartWebSocketV2(auth_token, api_key, client_code, feed_token)
      sws.on_open = callback   # subscribe() called HERE
      sws.on_data = callback
      sws.on_error = callback
      sws.on_close = callback
      sws.connect()            # NO ARGUMENTS — blocks until disconnected
    """
    _RECONNECT_DELAYS = [5, 10, 30, 60, 120]

    def __init__(self, broker):
        """
        broker: AngelOneBroker, AngelPaperBroker, or DualBroker.
                Must expose _token, _feed_token, _api, _connected.
        """
        self._broker = broker
        self._ws = None
        self._running = False
        self._prices: Dict[str, float] = {}
        self._tick_count = 0
        self._reconnect_attempt = 0
        self._subscribed = False
        self._connection_opened = False   # set True in _on_open; False = WS closed before open
        self._rest_polling_active = False  # True when fallen back to REST polling

        # Build token map from cfg.symbols
        self._token_map: Dict[str, str] = {}    # symbol → token string
        self._reverse_map: Dict[str, str] = {}  # token string → symbol
        self._build_token_map()

        # Subscription params stored for use in on_open callback
        self._correlation_id = "zerobot_v2"
        self._mode = 3   # SnapQuote: LTP + OHLC + volume + OI

        log.info(f"[AngelFeed] Init — {len(self._token_map)}/{len(cfg.symbols)} tokens mapped")

    def _build_token_map(self):
        for sym in cfg.symbols:
            clean = _clean_symbol(sym)
            if clean in ANGEL_TOKENS:
                tok = ANGEL_TOKENS[clean]
                self._token_map[sym] = tok
                self._reverse_map[tok] = sym
            # Dynamic lookup if broker is connected
            elif self._broker and getattr(self._broker, '_connected', False):
                try:
                    api = getattr(self._broker, '_api', None)
                    if api:
                        resp = api.searchScrip("NSE", clean)
                        if resp and resp.get("status"):
                            for sc in resp.get("data", []):
                                if sc.get("tradingsymbol", "").upper() == clean:
                                    tok = sc.get("symboltoken")
                                    if tok:
                                        self._token_map[sym] = tok
                                        self._reverse_map[tok] = sym
                except Exception:
                    pass

    async def start(self):
        if not self._broker or not getattr(self._broker, '_connected', False):
            raise RuntimeError(
                "[AngelFeed] Broker not connected. Cannot start Angel One WebSocket.\n"
                "  Ensure broker.connect_or_raise() succeeded before starting feed."
            )

        auth_token  = getattr(self._broker, '_token', None)
        feed_token  = getattr(self._broker, '_feed_token', None)
        api_key     = cfg.angel_one.api_key
        client_code = cfg.angel_one.client_id

        if not all([auth_token, feed_token, api_key, client_code]):
            missing = []
            if not auth_token: missing.append("_token (JWT)")
            if not feed_token: missing.append("_feed_token")
            if not api_key: missing.append("api_key")
            if not client_code: missing.append("client_id")
            raise RuntimeError(
                f"[AngelFeed] Missing auth params: {', '.join(missing)}\n"
                f"  Broker type: {type(self._broker).__name__}"
            )

        # Rebuild token map now that broker is confirmed connected
        self._build_token_map()

        self._running = True
        self._reconnect_attempt = 0

        log.info(f"[AngelFeed] Starting — {len(self._token_map)} tokens | mode=SnapQuote(3)")

        while self._running:
            try:
                await self._connect_ws(auth_token, api_key, client_code, feed_token)
            except asyncio.CancelledError:
                log.info("[AngelFeed] Feed cancelled")
                break
            except Exception as e:
                delay = self._RECONNECT_DELAYS[
                    min(self._reconnect_attempt, len(self._RECONNECT_DELAYS) - 1)
                ]
                self._reconnect_attempt += 1
                log.error(
                    f"[AngelFeed] WebSocket error: {e} — "
                    f"reconnect #{self._reconnect_attempt} in {delay}s"
                )

                # After 2 failed WS attempts, switch to REST polling
                # Angel One WebSocket may be blocked by IP whitelist even when REST works
                # REST polling gives real Angel One prices at ~10s intervals (not sub-second)
                if self._reconnect_attempt >= 2 and not self._rest_polling_active:
                    log.warning(
                        "[AngelFeed] WebSocket rejected after 2 attempts.\n"
                        "  LIKELY CAUSE: Angel One WebSocket requires your IP to be\n"
                        "  whitelisted separately from REST API. Current app IP may be stale.\n"
                        "  ACTION: Go to smartapi.angelone.in → My Apps → Edit App →\n"
                        "          update Primary Static IP to your current public IP.\n"
                        "  FALLBACK: Switching to Angel One REST API polling (10s interval)\n"
                        "           This gives real Angel One prices, just not sub-second ticks."
                    )
                    asyncio.ensure_future(self._rest_poll_loop())
                    self._rest_polling_active = True

                await asyncio.sleep(delay)

    async def _connect_ws(self, auth_token, api_key, client_code, feed_token):
        SmartWebSocketV2 = None
        for mod_path in ("SmartApi.smartWebSocketV2", "smartapi.smartWebSocketV2"):
            try:
                import importlib
                mod = importlib.import_module(mod_path)
                SmartWebSocketV2 = getattr(mod, "SmartWebSocketV2")
                break
            except (ImportError, AttributeError):
                continue

        if SmartWebSocketV2 is None:
            raise ImportError(
                "[AngelFeed] smartapi-python not installed.\n"
                "  Run: pip install smartapi-python pycryptodome"
            )

        # Build token list: [{"exchangeType": 1, "tokens": ["2885", "1333", ...]}]
        nse_tokens = list(self._token_map.values())
        if not nse_tokens:
            raise RuntimeError("[AngelFeed] No tokens to subscribe — check symbol list")

        token_list = [{"exchangeType": 1, "tokens": nse_tokens}]

        self._ws = SmartWebSocketV2(
            auth_token, api_key, client_code, feed_token,
            max_retry_attempt=3
        )

        # Store subscription params for on_open callback
        self._pending_token_list = token_list

        # Register callbacks
        self._ws.on_open  = self._on_open
        self._ws.on_data  = self._on_data
        self._ws.on_error = self._on_error
        self._ws.on_close = self._on_close

        log.info(
            f"[AngelFeed] Connecting SmartWebSocketV2 — {len(nse_tokens)} NSE tokens\n"
            f"  auth_token : {'OK (' + str(len(str(auth_token))) + ' chars)' if auth_token else '*** MISSING ***'}\n"
            f"  feed_token : {'OK (' + str(len(str(feed_token))) + ' chars)' if feed_token else '*** MISSING ***'}\n"
            f"  api_key    : {'OK' if api_key else '*** MISSING ***'}\n"
            f"  client_code: {client_code or '*** MISSING ***'}"
        )

        # connect() takes NO arguments — blocks until disconnected
        # If "Attempting to resubscribe/reconnect..." appears without our CONNECTED log,
        # it means the WebSocket handshake failed (likely expired feed_token or rate limit)
        self._connection_opened = False
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._ws.connect)

        if not self._connection_opened:
            raise RuntimeError(
                "[AngelFeed] WebSocket closed without connecting.\n"
                "  'Attempting to resubscribe/reconnect...' = Angel One rejected the connection.\n"
                "  Causes: (1) feed_token expired, (2) API rate limit, (3) session invalid.\n"
                "  Will retry with backoff."
            )

    def _on_open(self, wsapp=None):
        """WebSocket opened — subscribe() MUST be called here per Angel One API spec."""
        self._connection_opened = True
        self._reconnect_attempt = 0
        log.info(
            f"[AngelFeed] WebSocket CONNECTED — "
            f"subscribing {len(self._pending_token_list[0]['tokens'])} tokens"
        )
        try:
            self._ws.subscribe(
                self._correlation_id,
                self._mode,
                self._pending_token_list
            )
            self._subscribed = True
            log.info(f"[AngelFeed] Subscribed ✅ — streaming {len(self._token_map)} symbols live")
        except Exception as e:
            log.error(f"[AngelFeed] subscribe() failed: {e}")

    def _on_data(self, wsapp, data):
        try:
            token_str = str(data.get("token", data.get("tk", "")))
            symbol = self._reverse_map.get(token_str)
            if not symbol:
                return

            ltp = float(data.get("last_traded_price", data.get("ltp", data.get("lp", 0))) or 0)
            if ltp <= 0:
                return

            # Angel One returns prices in paisa for some fields — normalise if >5x expected
            if ltp > 1_000_000:
                ltp /= 100.0

            volume = int(data.get("volume_trade_for_the_day", data.get("v", 0)) or 0)
            oi     = float(data.get("open_interest", data.get("oi", 0)) or 0)
            open_p = float(data.get("open_price_of_the_day", data.get("o", ltp)) or ltp)
            high   = float(data.get("high_price_of_the_day",  data.get("h", ltp)) or ltp)
            low    = float(data.get("low_price_of_the_day",   data.get("l", ltp)) or ltp)
            close  = float(data.get("closed_price",           data.get("c", ltp)) or ltp)

            prev = self._prices.get(symbol, ltp)
            self._prices[symbol] = ltp
            self._tick_count += 1

            tick = {
                "symbol": symbol, "ltp": round(ltp, 2),
                "open": round(open_p, 2), "high": round(high, 2),
                "low": round(low, 2), "close": round(close, 2),
                "volume": volume, "oi": oi,
                "change": round(ltp - prev, 4),
                "change_pct": round((ltp - prev) / prev * 100, 4) if prev > 0 else 0.0,
                "timestamp": datetime.now().isoformat(),
                "source": "angel_one_ws",
            }
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(bus.publish("tick", tick), loop)
            except Exception:
                pass
        except Exception as e:
            log.debug(f"[AngelFeed] _on_data error: {e}")

    def _on_error(self, wsapp, err=None, *args):
        log.error(f"[AngelFeed] WebSocket error: {err}")

    def _on_close(self, wsapp, *args):
        log.warning("[AngelFeed] WebSocket closed — reconnect loop will restart")

    def get_last_price(self, symbol: str) -> Optional[float]:
        return self._prices.get(_clean_symbol(symbol))

    def get_tick_count(self) -> int:
        return self._tick_count



    async def _rest_poll_loop(self):
        """
        Fallback: Poll Angel One REST API (ltpData) every 10s when WebSocket is blocked.
        Gives real Angel One prices without sub-second latency.
        Called automatically after 2 failed WebSocket attempts.
        """
        log.info("[AngelFeed] REST polling mode ACTIVE — real Angel One prices every 10s")
        symbols = list(self._token_map.keys())

        while self._running:
            try:
                api = getattr(self._broker, '_api', None)
                if not api:
                    break

                fetched = 0
                for sym in symbols:
                    try:
                        clean = sym.replace(".NS", "").replace("^", "").upper()
                        token = self._token_map.get(sym)
                        if not token:
                            continue
                        resp = api.ltpData("NSE", f"{clean}-EQ", token)
                        if resp and resp.get("status"):
                            ltp = float(resp.get("data", {}).get("ltp", 0))
                            if ltp > 0:
                                prev = self._prices.get(sym, ltp)
                                self._prices[sym] = ltp
                                self._tick_count += 1
                                fetched += 1

                                tick = {
                                    "symbol": sym, "ltp": round(ltp, 2),
                                    "open": round(ltp, 2), "high": round(ltp, 2),
                                    "low": round(ltp, 2), "close": round(ltp, 2),
                                    "volume": 0, "oi": 0,
                                    "change": round(ltp - prev, 4),
                                    "change_pct": round((ltp - prev) / prev * 100, 4) if prev > 0 else 0.0,
                                    "timestamp": datetime.now().isoformat(),
                                    "source": "angel_one_rest",
                                }
                                await bus.publish("tick", tick)
                    except Exception:
                        pass

                if fetched > 0:
                    log.debug(f"[AngelFeed] REST poll: {fetched}/{len(symbols)} prices updated")
                await asyncio.sleep(10)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug(f"[AngelFeed] REST poll error: {e}")
                await asyncio.sleep(15)

        log.info("[AngelFeed] REST polling stopped")

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close_connection()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# SHOONYA WebSocket Feed (S-paper, S-live)
# API: api.start_websocket() + api.subscribe('NSE|TOKEN')
# ─────────────────────────────────────────────────────────────────────────────
class ShoonyaRealtimeFeed:
    """
    Shoonya NorenRestApiPy WebSocket real-time tick feed.
    Used in S-paper and S-live modes.

    Correct API (from https://github.com/rahulmr/NorenRestApiPy):
      api.start_websocket(
          order_update_callback=on_order,
          subscribe_callback=on_tick,
          socket_open_callback=on_open    # subscribe() called in on_open
      )
      api.subscribe('NSE|2885')           # token format: 'EXCHANGE|TOKEN'
    """

    def __init__(self, broker):
        """
        broker: ShoonyaPaperBroker or ShoonyaLiveBroker.
                Must expose ._api (NorenApiPy instance) and .is_connected.
        """
        self._broker = broker
        self._api = getattr(broker, '_api', None) or getattr(
            getattr(broker, '_shoonya_raw', None), '_api', None
        )
        self._prices: Dict[str, float] = {}
        self._tick_count = 0
        self._running = False
        self._ws_opened = False

        # Build token list: ['NSE|2885', 'NSE|1333', ...]
        self._tokens: list[str] = []
        self._token_to_sym: Dict[str, str] = {}
        self._build_token_list()

        log.info(f"[ShoonyaFeed] Init — {len(self._tokens)} tokens to subscribe")

    def _build_token_list(self):
        for sym in cfg.symbols:
            clean = _clean_symbol(sym)
            shoonya_tok = SHOONYA_TOKENS.get(clean)
            if shoonya_tok:
                self._tokens.append(shoonya_tok)
                self._token_to_sym[shoonya_tok] = sym
                # Also map just the token number
                tok_num = shoonya_tok.split("|")[1]
                self._token_to_sym[tok_num] = sym

    async def start(self):
        if not self._broker or not getattr(self._broker, 'is_connected', False):
            raise RuntimeError(
                "[ShoonyaFeed] Broker not connected. Cannot start Shoonya WebSocket.\n"
                "  Ensure broker.connect_or_raise() succeeded."
            )
        if not self._api:
            raise RuntimeError(
                "[ShoonyaFeed] No Shoonya API object available.\n"
                "  broker._api is None — connection may have failed silently."
            )
        if not self._tokens:
            raise RuntimeError("[ShoonyaFeed] No tokens to subscribe — check symbol list")

        self._running = True
        log.info(f"[ShoonyaFeed] Starting WebSocket — {len(self._tokens)} tokens")

        # start_websocket is blocking — run in executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._start_ws_blocking)

    def _start_ws_blocking(self):
        """Called in thread executor — blocks until disconnected."""
        self._api.start_websocket(
            order_update_callback=self._on_order,
            subscribe_callback=self._on_tick,
            socket_open_callback=self._on_open
        )

    def _on_open(self):
        """Called when WS connects — subscribe to all tokens here."""
        self._ws_opened = True
        log.info(f"[ShoonyaFeed] WebSocket CONNECTED — subscribing {len(self._tokens)} tokens")
        try:
            # Shoonya accepts list of tokens
            self._api.subscribe(self._tokens)
            log.info(f"[ShoonyaFeed] Subscribed ✅ — streaming {len(self._tokens)} symbols live")
        except Exception as e:
            log.error(f"[ShoonyaFeed] subscribe() failed: {e}")

    def _on_tick(self, tick_data: dict):
        """Called for each price update from Shoonya."""
        try:
            # Shoonya tick keys: t (type), e (exchange), tk (token), lp (last price)
            # t='tk' = acknowledgement (first tick), t='tf' = field update
            tick_type = tick_data.get("t", "")
            if tick_type not in ("tk", "tf"):
                return

            token_num = str(tick_data.get("tk", ""))
            symbol = self._token_to_sym.get(token_num) or \
                     self._token_to_sym.get(f"NSE|{token_num}")
            if not symbol:
                return

            ltp = float(tick_data.get("lp", tick_data.get("c", 0)) or 0)
            if ltp <= 0:
                return

            volume = int(tick_data.get("v", 0) or 0)
            oi     = float(tick_data.get("oi", 0) or 0)
            open_p = float(tick_data.get("o", ltp) or ltp)
            high   = float(tick_data.get("h", ltp) or ltp)
            low    = float(tick_data.get("l", ltp) or ltp)
            close  = float(tick_data.get("c", ltp) or ltp)

            prev = self._prices.get(symbol, ltp)
            self._prices[symbol] = ltp
            self._tick_count += 1

            tick = {
                "symbol": symbol, "ltp": round(ltp, 2),
                "open": round(open_p, 2), "high": round(high, 2),
                "low": round(low, 2), "close": round(close, 2),
                "volume": volume, "oi": oi,
                "change": round(ltp - prev, 4),
                "change_pct": round((ltp - prev) / prev * 100, 4) if prev > 0 else 0.0,
                "timestamp": datetime.now().isoformat(),
                "source": "shoonya_ws",
            }
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.run_coroutine_threadsafe(bus.publish("tick", tick), loop)
            except Exception:
                pass
        except Exception as e:
            log.debug(f"[ShoonyaFeed] _on_tick error: {e}")

    def _on_order(self, order_data: dict):
        """Order update callback — publish to event bus."""
        try:
            asyncio.run_coroutine_threadsafe(
                bus.publish("order_update", order_data),
                asyncio.get_event_loop()
            )
        except Exception:
            pass

    def get_last_price(self, symbol: str) -> Optional[float]:
        return self._prices.get(_clean_symbol(symbol))

    def get_tick_count(self) -> int:
        return self._tick_count



    def stop(self):
        self._running = False
        if self._api:
            try:
                self._api.close_websocket()
            except Exception:
                pass
