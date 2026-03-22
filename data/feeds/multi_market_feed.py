# -*- coding: utf-8 -*-
"""
ZeroBot v2 — Multi-Market Data Feed
Real-time and historical data for:
  - NSE/BSE Equities (Yahoo Finance → Angel One WebSocket when live)
  - Crypto (Binance WebSocket)
  - Forex (Alpha Vantage / Twelve Data)
  - US Stocks (Alpaca Data Stream)
"""
import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable
import yfinance as yf
from core.config import cfg
from core.logger import log
from core.event_bus import bus


# ── Symbol format converters ──────────────────────────────────────
def to_yfinance_symbol(symbol: str, market: str = "NSE") -> str:
    """Convert internal symbol to Yahoo Finance format."""
    if market == "NSE" and not symbol.endswith(".NS") and not symbol.startswith("^"):
        return f"{symbol}.NS"
    if market == "CRYPTO":
        # BTC → BTC-USD
        if "/" in symbol:
            return symbol.replace("/", "-")
        if not symbol.endswith("-USD") and not symbol.endswith("USDT"):
            return f"{symbol}-USD"
    return symbol


def from_yfinance_symbol(symbol: str) -> str:
    """Strip exchange suffix for internal use."""
    return symbol.replace(".NS", "").replace("-USD", "").replace("^", "")


# ── Multi-Market Realtime Feed ────────────────────────────────────
class MultiMarketFeed:
    """
    Unified real-time feed for all markets.
    Paper: polls Yahoo Finance every N seconds (all markets).
    Live:  routes to Angel One WS / Binance WS / Alpaca Stream.
    """

    POLL_INTERVALS = {
        "NSE": 10,       # NSE: 10s during market hours
        "CRYPTO": 5,     # Crypto: 5s (24/7)
        "FOREX": 15,     # Forex: 15s
        "US": 10,        # US Stocks: 10s
    }

    def __init__(self, market_symbols: Dict[str, List[str]] = None):
        """
        Args:
            market_symbols: {"NSE": ["RELIANCE.NS", "TCS.NS"], "CRYPTO": ["BTC-USD"]}
        """
        if market_symbols:
            self._market_symbols = market_symbols
        else:
            # Default from config
            self._market_symbols = {"NSE": cfg.symbols}

        self._running = False
        self._last_prices: Dict[str, float] = {}
        self._last_update: Dict[str, datetime] = {}
        self._tick_count = 0
        self._subscribers: List[Callable] = []

        all_count = sum(len(v) for v in self._market_symbols.values())
        log.info(f"📡 MultiMarket Feed — {all_count} symbols across {len(self._market_symbols)} markets")

    async def start(self):
        """Start all market feeds concurrently."""
        self._running = True
        tasks = []
        for market, symbols in self._market_symbols.items():
            if not symbols:
                continue
            if cfg.is_paper:
                tasks.append(asyncio.create_task(
                    self._yahoo_poll_loop(market, symbols)
                ))
            else:
                tasks.append(asyncio.create_task(
                    self._live_feed(market, symbols)
                ))

        log.info(f"📡 Starting {len(tasks)} feed tasks")
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _yahoo_poll_loop(self, market: str, symbols: List[str]):
        """Poll Yahoo Finance for a specific market."""
        interval = self.POLL_INTERVALS.get(market, 10)
        log.info(f"📡 Yahoo feed: {market} ({len(symbols)} symbols, {interval}s interval)")

        while self._running:
            try:
                prices = await asyncio.get_event_loop().run_in_executor(
                    None, self._fetch_batch, symbols
                )
                for symbol, price_data in prices.items():
                    await self._emit_tick(symbol, price_data, market)

                self._tick_count += 1
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Feed error [{market}]: {e}")
                await asyncio.sleep(5)

    def _fetch_batch(self, symbols: List[str]) -> Dict[str, Dict]:
        """Batch fetch prices from Yahoo Finance."""
        result = {}
        try:
            # Fetch all at once for efficiency
            data = yf.download(
                tickers=symbols,
                period="2d",
                interval="1m",
                auto_adjust=True,
                progress=False,
                group_by="ticker"
            )

            for sym in symbols:
                try:
                    if len(symbols) == 1:
                        df = data
                    else:
                        df = data[sym] if sym in data.columns.get_level_values(0) else None

                    if df is not None and not df.empty:
                        last = df.dropna().iloc[-1]
                        prev = df.dropna().iloc[-2] if len(df.dropna()) > 1 else last
                        result[sym] = {
                            "open": float(last.get("Open", 0)),
                            "high": float(last.get("High", 0)),
                            "low": float(last.get("Low", 0)),
                            "close": float(last.get("Close", 0)),
                            "volume": float(last.get("Volume", 0)),
                            "prev_close": float(prev.get("Close", 0)),
                        }
                except Exception:
                    pass
        except Exception as e:
            log.debug(f"Yahoo batch fetch error: {e}")
            # Fallback: individual tickers
            for sym in symbols[:5]:  # limit to avoid rate limit
                try:
                    t = yf.Ticker(sym)
                    info = t.fast_info
                    price = float(info.last_price or 0)
                    if price > 0:
                        result[sym] = {
                            "close": price,
                            "open": price, "high": price, "low": price,
                            "volume": 0, "prev_close": price
                        }
                except Exception:
                    pass
        return result

    async def _emit_tick(self, symbol: str, price_data: Dict, market: str):
        """Emit structured tick event."""
        price = price_data.get("close", 0)
        if price <= 0:
            return

        prev = self._last_prices.get(symbol, price)
        change = price - prev
        change_pct = (change / prev * 100) if prev > 0 else 0

        spread_pct = 0.0002
        bid = price * (1 - spread_pct / 2)
        ask = price * (1 + spread_pct / 2)

        tick = {
            "symbol": symbol,
            "market": market,
            "ltp": round(price, 4),
            "bid": round(bid, 4),
            "ask": round(ask, 4),
            "open": price_data.get("open", price),
            "high": price_data.get("high", price),
            "low": price_data.get("low", price),
            "volume": price_data.get("volume", 0),
            "change": round(change, 4),
            "change_pct": round(change_pct, 4),
            "prev_close": price_data.get("prev_close", prev),
            "timestamp": datetime.now().isoformat(),
            "source": "yahoo_paper",
        }

        self._last_prices[symbol] = price
        self._last_update[symbol] = datetime.now()
        await bus.publish("tick", tick)

    async def _live_feed(self, market: str, symbols: List[str]):
        """Route to appropriate live feed based on market."""
        if market == "NSE":
            await self._angel_one_feed(symbols)
        elif market == "CRYPTO":
            await self._binance_feed(symbols)
        elif market == "US":
            await self._alpaca_feed(symbols)
        else:
            # Fallback to Yahoo polling for unsupported live markets
            await self._yahoo_poll_loop(market, symbols)

    async def _angel_one_feed(self, symbols: List[str]):
        """Angel One WebSocket feed (activate with credentials)."""
        # Uncomment after connecting Angel One:
        # from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        # token_map = await _resolve_angel_tokens(symbols)
        # ws = SmartWebSocketV2(auth_token, api_key, client_code, feed_token)
        # ws.on_data = lambda data: asyncio.create_task(bus.publish("tick", _parse_angel_tick(data)))
        # ws.connect()
        log.warning("Angel One live feed: activate by adding SmartAPI WebSocket code")
        await self._yahoo_poll_loop("NSE", symbols)  # Fallback

    async def _binance_feed(self, symbols: List[str]):
        """Binance WebSocket ticker stream."""
        try:
            from binance import AsyncClient, BinanceSocketManager
            client = await AsyncClient.create(
                api_key=getattr(cfg, 'binance_api_key', ''),
                api_secret=getattr(cfg, 'binance_secret_key', '')
            )
            bm = BinanceSocketManager(client)
            streams = [f"{s.replace('-', '').lower()}@ticker" for s in symbols]
            async with bm.multiplex_socket(streams) as stream:
                while self._running:
                    msg = await stream.recv()
                    data = msg.get("data", {})
                    symbol = data.get("s", "")
                    price = float(data.get("c", 0))
                    if symbol and price > 0:
                        await bus.publish("tick", {
                            "symbol": symbol,
                            "market": "CRYPTO",
                            "ltp": price,
                            "bid": float(data.get("b", price)),
                            "ask": float(data.get("a", price)),
                            "volume": float(data.get("v", 0)),
                            "change_pct": float(data.get("P", 0)),
                            "timestamp": datetime.now().isoformat(),
                            "source": "binance_ws",
                        })
        except Exception as e:
            log.error(f"Binance WS feed failed: {e}. Falling back to polling.")
            await self._yahoo_poll_loop("CRYPTO", symbols)

    async def _alpaca_feed(self, symbols: List[str]):
        """Alpaca data stream for US stocks."""
        try:
            from alpaca.data.live import StockDataStream
            stream = StockDataStream(
                api_key=getattr(cfg, 'alpaca_api_key', ''),
                secret_key=getattr(cfg, 'alpaca_secret_key', '')
            )
            async def on_quote(quote):
                await bus.publish("tick", {
                    "symbol": quote.symbol,
                    "market": "US",
                    "ltp": float(quote.ask_price or 0),
                    "bid": float(quote.bid_price or 0),
                    "ask": float(quote.ask_price or 0),
                    "timestamp": datetime.now().isoformat(),
                    "source": "alpaca_stream",
                })
            stream.subscribe_quotes(on_quote, *symbols)
            await stream.run()
        except Exception as e:
            log.error(f"Alpaca stream failed: {e}. Falling back to polling.")
            await self._yahoo_poll_loop("US", symbols)

    def stop(self):
        self._running = False
        log.info("📡 MultiMarket Feed stopped")

    def get_last_price(self, symbol: str) -> Optional[float]:
        return self._last_prices.get(symbol)

    def get_stats(self) -> Dict:
        return {
            "tick_count": self._tick_count,
            "symbols_tracked": len(self._last_prices),
            "markets": list(self._market_symbols.keys()),
            "last_updates": {s: t.isoformat() for s, t in self._last_update.items()},
        }
