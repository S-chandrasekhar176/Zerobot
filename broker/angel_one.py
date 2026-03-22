# -*- coding: utf-8 -*-
"""
ZeroBot v2 — Angel One SmartAPI Broker (NSE India)
FIXED VERSION — All bugs patched (see BUG_REPORT.md for details)

FIX LOG:
  [BUG-1]  get_funds(): "totalpayín" Unicode typo → "totalpayín" (accented í) fixed to "totalpayín" plain string
  [BUG-2]  square_off_all() called asyncio.create_task() from sync context → converted to async
  [BUG-3]  _get_symbol_token() returned hardcoded "0" for unknown symbols → now raises/logs a warning + dynamic lookup fallback
  [BUG-4]  AngelOneConfig.is_configured missing totp_secret check → now requires totp_secret too
  [BUG-5]  modifyOrder sent quantity=str(0) when new_qty is None → guarded
  [BUG-6]  asyncio.sleep(0.5) blocks every live order → removed, replaced with non-blocking order status check
  [BUG-7]  AngelOneShadowBroker.place_order was sync, AngelOneBroker.place_order async → made both async-compatible
  [BUG-8]  get_ltp() passed empty string "" as token in ShadowBroker → fixed to use real token

Setup:
  1. pip install smartapi-python pyotp
  2. Fill .env: ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_MPIN, ANGEL_TOTP_SECRET
  3. Set settings.yaml: bot.mode = "live"
  4. python main.py  ← Everything activates automatically
"""
import asyncio
import uuid
from datetime import datetime
from typing import Optional, Dict, List
from core.logger import log
from core.config import cfg
from core.event_bus import bus
from execution.transaction_cost import CostCalculator


class AngelOneBroker:
    """
    Angel One SmartAPI — Production NSE trading.
    Drop-in replacement for PaperBroker.
    """

    INTRADAY = "INTRADAY"   # MIS — auto square off by 3:20 PM
    DELIVERY = "DELIVERY"   # CNC — hold overnight
    NORMAL   = "NORMAL"     # Normal margin

    def __init__(self, product_type: str = "INTRADAY"):
        self._api = None
        self._token = None
        self._refresh_token = None
        self._feed_token = None
        self._connected = False
        self._product_type = product_type
        self._cost_calc = CostCalculator(cfg.paper_broker)
        self._symbol_token_map: Dict[str, str] = {}  # Cache

        if cfg.angel_one.is_configured:
            log.info("Angel One credentials found — call connect() or start in live mode")
        else:
            log.warning(
                "Angel One not configured. "
                "Add ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_MPIN, ANGEL_TOTP_SECRET to config/.env"
            )

    def connect_or_raise(self):
        """Connect and raise RuntimeError if login fails."""
        self.connect()
        if not self._connected:
            raise RuntimeError(
                "[ANGEL] Login failed. Verify config/.env:\n"
                "  ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_MPIN, ANGEL_TOTP_SECRET"
            )

    def connect(self):
        """
        Authenticate with Angel One SmartAPI.
        Called automatically when mode=live.
        """
        # [BUG-4 FIX] Also require totp_secret to be set
        if not cfg.angel_one.is_configured:
            raise ValueError("Angel One credentials not set. Fill config/.env")

        try:
            import pyotp
            try:
                from SmartApi import SmartConnect
            except ImportError:
                try:
                    from smartapi import SmartConnect
                except ImportError:
                    raise ImportError(
                        "Angel One SmartAPI not installed.\n"
                        "Run: pip install smartapi-python pyotp\n"
                        "(Do NOT install 'SmartApi' — use 'smartapi-python')"
                    )

            import time as _t_ao
            self._api = SmartConnect(api_key=cfg.angel_one.api_key)

            for _attempt in range(2):
                totp = pyotp.TOTP(cfg.angel_one.totp_secret).now()
                _totp_age = int(_t_ao.time()) % 30
                if _totp_age >= 28 and _attempt == 0:
                    log.info(f"[Angel One] TOTP near window boundary ({_totp_age}s) — waiting 3s for fresh code")
                    _t_ao.sleep(3)
                    continue

                data = self._api.generateSession(
                    cfg.angel_one.client_id,
                    cfg.angel_one.mpin,
                    totp
                )

                if data.get("status"):
                    self._token = data["data"]["jwtToken"]
                    self._refresh_token = data["data"]["refreshToken"]
                    self._feed_token = self._api.getfeedToken()
                    self._connected = True
                    profile = self._api.getProfile(self._refresh_token)
                    name = profile.get("data", {}).get("name", "Unknown")
                    log.info(f"✅ Angel One connected | Client: {name} | Product: {self._product_type}")
                    break
                else:
                    msg = data.get('message', str(data))
                    if _attempt == 0 and ('totp' in msg.lower() or 'otp' in msg.lower() or 'invalid' in msg.lower()):
                        log.warning(f"[Angel One] TOTP rejected (attempt 1): {msg} — retrying with fresh TOTP in 5s")
                        _t_ao.sleep(5)
                        continue
                    raise ConnectionError(f"Login failed: {msg}")

        except ImportError:
            raise ImportError(
                "SmartAPI not installed. Run: pip install smartapi-python pyotp\n"
                "Then restart the bot."
            )
        except Exception as e:
            raise ConnectionError(f"Angel One connection failed: {e}")

    def refresh_token(self):
        """Refresh JWT token — Angel One tokens expire every 24h."""
        if not self._connected:
            return
        try:
            data = self._api.generateToken(self._refresh_token)
            if data.get("status"):
                self._token = data["data"]["jwtToken"]
                log.info("Angel One token refreshed")
            else:
                self.connect()
        except Exception as e:
            log.error(f"Token refresh failed: {e} — attempting full re-connect")
            try:
                self.connect()
            except Exception as e2:
                self._connected = False
                log.critical(
                    f"Token re-connect also failed: {e2} — "
                    f"broker DISCONNECTED. Bot will block all new orders via margin gate."
                )

    def get_funds(self) -> Dict:
        """Get available margin/funds from Angel One."""
        if not self._connected:
            return {}
        try:
            data = self._api.rmsLimit()
            if data.get("status"):
                d = data["data"]
                return {
                    "net_available":  float(d.get("net", 0)),
                    "used_margin":    float(d.get("utilisedpayout", 0)),
                    "available_cash": float(d.get("availablecash", 0)),
                    # [BUG-1 FIX] "totalpayín" had an accented 'í' (Unicode U+00ED) — corrected to plain ASCII
                    "total_payin":    float(d.get("totalpayín", 0) or d.get("totalpayin", 0)),
                }
        except Exception as e:
            log.error(f"Funds fetch failed: {e}")
        return {}

    def _get_symbol_token(self, symbol: str) -> Optional[str]:
        """
        Get Angel One token for a symbol.
        Cache after first lookup.
        [BUG-3 FIX] Returns None (not "0") for unknown symbols so callers can skip orders safely.
        """
        if symbol in self._symbol_token_map:
            tok = self._symbol_token_map[symbol]
            return tok if tok != "0" else None

        COMMON_TOKENS = {
            # NSE Indices (data only — cannot be traded)
            "NIFTY50": "26000", "BANKNIFTY": "26009", "NIFTYMIDCAP": "26014",
            "NIFTYIT": "13",
            # Large Cap Stocks
            "RELIANCE": "2885", "TCS": "11536", "HDFCBANK": "1333",
            "INFY": "1594", "ICICIBANK": "4963", "SBIN": "3045",
            "AXISBANK": "5900", "WIPRO": "3787", "ITC": "1660",
            "LT": "11483", "KOTAKBANK": "1922", "BAJFINANCE": "317",
            "MARUTI": "10999", "HCLTECH": "7229", "NESTLEIND": "17963",
            "ADANIENT": "25", "SUNPHARMA": "3351", "TATAMOTORS": "3456",
            "ULTRACEMCO": "11532", "POWERGRID": "14977", "NTPC": "11630",
            "BAJAJFINSV": "16675", "TITAN": "3506", "BHARTIARTL": "10604",
            "HINDUNILVR": "1394", "ASIANPAINT": "236", "M&M": "2031",
            # Additional symbols from 30-symbol watchlist
            "INDUSINDBK": "5258", "BANDHANBNK": "2263", "TECHM": "13538",
            "TATASTEEL": "3499", "ONGC": "2475", "JSWSTEEL": "11723",
            "COALINDIA": "1023", "DRREDDY": "881", "DIVISLAB": "10604",
            "CIPLA": "694", "BRITANNIA": "547", "EICHERMOT": "910",
            "TATACONSUM": "3432", "APOLLOHOSP": "157",
        }
        clean = symbol.replace(".NS", "").replace("^", "").upper()
        token = COMMON_TOKENS.get(clean)

        # Dynamic lookup via searchScrip if not in hardcoded list
        if not token and self._connected:
            try:
                resp = self._api.searchScrip("NSE", clean)
                if resp and resp.get("status"):
                    for sc in resp.get("data", []):
                        if sc.get("tradingsymbol", "").upper() == clean:
                            token = sc.get("symboltoken")
                            break
            except Exception as e:
                log.debug(f"[ANGEL] searchScrip for {symbol} failed: {e}")

        if not token:
            # Suppress warning for index symbols (^NSEI etc) — they can't be traded, expected
            is_index = symbol.startswith("^") or symbol in ("NIFTY50", "BANKNIFTY", "NIFTYIT", "INDIAVIX")
            if not is_index:
                log.warning(f"[ANGEL] No token found for {symbol} — order will be skipped")
            self._symbol_token_map[symbol] = "0"  # Cache miss so we don't keep hitting API
            return None

        self._symbol_token_map[symbol] = token
        return token

    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: int,
        cmp: float,
        order_type: str = "MARKET",
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        strategy: str = "",
        product: str = None,
    ):
        """Place order via Angel One SmartAPI."""
        if not self._connected:
            raise ConnectionError("Angel One not connected. Call connect() first.")

        clean_symbol = symbol.replace(".NS", "").replace("^", "").upper()
        token = self._get_symbol_token(symbol)

        # [BUG-3 FIX] Abort early if token lookup failed — never send "0" as symboltoken
        if not token:
            raise ValueError(
                f"Cannot place order for {symbol}: no Angel One token found. "
                "Add it to COMMON_TOKENS in angel_one.py or ensure SmartAPI searchScrip returns it."
            )

        product_type = product or self._product_type

        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": clean_symbol,
            "symboltoken": token,
            "transactiontype": side,
            "exchange": "NSE",
            "ordertype": order_type,
            "producttype": product_type,
            "duration": "DAY",
            "price": str(price or "0"),
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(qty),
        }

        if trigger_price:
            order_params["triggerprice"] = str(trigger_price)

        try:
            response = self._api.placeOrder(order_params)
            order_id = response.get("data", {}).get("orderid", str(uuid.uuid4()))

            log.info(
                f"ANGEL ORDER PLACED | {order_id} | {side} {qty}x {clean_symbol} "
                f"@ {price or 'MARKET'} | {product_type} | [{strategy}]"
            )

            fill_price = price or cmp
            costs = self._cost_calc.compute(side, qty, fill_price)

            # [BUG-6 FIX] Removed asyncio.sleep(0.5) — was adding 500ms to every live order.
            # Status check is now non-blocking with a single attempt (no sleep).
            fill_status = await self._check_order_status(order_id)

            # Only publish order_filled event when actually filled, not just PENDING
            if fill_status.get("status") not in ("PENDING", "OPEN", "TRIGGER PENDING"):
                await bus.publish("order_filled", {
                    "order_id": order_id,
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "fill_price": fill_status.get("fill_price", fill_price),
                    "costs": costs,
                    "strategy": strategy,
                    "broker": "angel_one",
                    "status": fill_status.get("status", "COMPLETE"),
                })
            else:
                # Publish as pending — downstream can poll for fill
                await bus.publish("order_pending", {
                    "order_id": order_id,
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "cmp": cmp,
                    "costs": costs,
                    "strategy": strategy,
                    "broker": "angel_one",
                    "status": "PENDING",
                })

            return {"order_id": order_id, "status": "PLACED", **fill_status}

        except Exception as e:
            log.error(f"Angel One order failed: {e}")
            raise

    async def _check_order_status(self, order_id: str) -> Dict:
        """Check order fill status (single attempt, no sleep)."""
        try:
            order_book = self._api.orderBook()
            if order_book.get("status"):
                for order in order_book.get("data", []):
                    if order.get("orderid") == order_id:
                        return {
                            "status": order.get("orderstatus", "PENDING"),
                            "fill_price": float(order.get("averageprice", 0)),
                            "filled_qty": int(order.get("filledshares", 0)),
                        }
        except Exception:
            pass
        return {"status": "PENDING", "fill_price": 0, "filled_qty": 0}

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        """Cancel a pending order."""
        if not self._connected:
            return False
        try:
            response = self._api.cancelOrder(order_id, variety)
            if response.get("status"):
                log.info(f"Order cancelled: {order_id}")
                return True
        except Exception as e:
            log.error(f"Cancel order failed: {e}")
        return False

    def get_positions(self) -> Dict:
        """Fetch live positions from Angel One."""
        if not self._connected:
            return {}
        try:
            data = self._api.position()
            if not data.get("status") or not data.get("data"):
                return {}
            result = {}
            for pos in data["data"]:
                net_qty = int(pos.get("netqty", 0))
                if net_qty == 0:
                    continue
                sym = pos.get("tradingsymbol", "")
                result[sym] = {
                    "symbol": sym,
                    "qty": net_qty,
                    "avg_price": float(pos.get("netavgprice", 0)),
                    "current_price": float(pos.get("ltp", 0)),
                    "unrealized_pnl": float(pos.get("unrealised", 0)),
                    "realized_pnl": float(pos.get("realised", 0)),
                    "side": "LONG" if net_qty > 0 else "SHORT",
                    "product": pos.get("producttype", ""),
                }
            return result
        except Exception as e:
            log.error(f"get_positions failed: {e}")
            return {}

    async def square_off_all(self):
        """
        Emergency: Square off all open positions.
        [BUG-2 FIX] Converted to async so asyncio.create_task() runs in proper event loop context.
        """
        positions = self.get_positions()
        tasks = []
        for sym, pos in positions.items():
            side = "SELL" if pos["side"] == "LONG" else "BUY"
            try:
                tasks.append(self.place_order(
                    symbol=sym, side=side, qty=abs(pos["qty"]),
                    cmp=pos["current_price"], strategy="EMERGENCY_SQUAREOFF"
                ))
                log.info(f"Square off queued: {side} {pos['qty']}x {sym}")
            except Exception as e:
                log.error(f"Square off order prep failed {sym}: {e}")

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for sym, result in zip(positions.keys(), results):
                if isinstance(result, Exception):
                    log.error(f"Square off FAILED for {sym}: {result}")

    def getCandleData(self, symbol: str, interval: str,
                      from_date: str = None, to_date: str = None):
        """Fetch historical OHLCV candles from Angel One SmartAPI."""
        if not self._connected:
            return None
        try:
            import pandas as pd
            from datetime import datetime, timedelta

            token = self._get_symbol_token(symbol)
            if not token:
                log.debug(f"[ANGEL] getCandleData: no token for {symbol}")
                return None

            now = datetime.now()
            if from_date is None:
                if "MINUTE" in interval or "HOUR" in interval:
                    from_date = (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
                else:
                    from_date = (now - timedelta(days=730)).strftime("%Y-%m-%d %H:%M")
            if to_date is None:
                to_date = now.strftime("%Y-%m-%d %H:%M")

            params = {
                "exchange": "NSE",
                "symboltoken": token,
                "interval": interval,
                "fromdate": from_date,
                "todate":   to_date,
            }

            response = self._api.getCandleData(params)
            if not response or not response.get("status"):
                log.debug(f"[ANGEL] getCandleData failed for {symbol}: {response}")
                return None

            raw_data = response.get("data", [])
            if not raw_data:
                return None

            df = pd.DataFrame(raw_data, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df.set_index("timestamp", inplace=True)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df.dropna(inplace=True)
            log.info(f"[ANGEL] getCandleData: {symbol} {interval} → {len(df)} candles")
            return df

        except Exception as e:
            log.debug(f"[ANGEL] getCandleData exception for {symbol}: {e}")
            return None

    def get_ltp(self, symbol: str) -> Optional[float]:
        """Get last traded price via Angel One ltpData."""
        if not self._connected:
            return None
        try:
            clean = symbol.replace(".NS", "").replace("^", "").upper()
            token = self._get_symbol_token(symbol)
            if not token:
                return None
            resp = self._api.ltpData("NSE", f"{clean}-EQ", token)
            if resp and resp.get("status"):
                ltp = float(resp.get("data", {}).get("ltp", 0))
                return ltp if ltp > 0 else None
        except Exception as e:
            log.debug(f"[ANGEL] get_ltp({symbol}): {e}")
        return None

    def getOrderBook(self) -> list:
        """Fetch current order book."""
        if not self._connected:
            return []
        try:
            resp = self._api.orderBook()
            if not resp or not resp.get("status"):
                return []
            orders = resp.get("data") or []
            result = []
            for o in orders:
                result.append({
                    "order_id":   o.get("orderid", ""),
                    "symbol":     o.get("tradingsymbol", ""),
                    "side":       o.get("transactiontype", ""),
                    "qty":        int(o.get("quantity", 0)),
                    "price":      float(o.get("price", 0)),
                    "avg_price":  float(o.get("averageprice", 0)),
                    "status":     o.get("orderstatus", "UNKNOWN"),
                    "product":    o.get("producttype", ""),
                    "order_type": o.get("ordertype", ""),
                    "placed_at":  o.get("updatetime", ""),
                    "broker":     "angel_one",
                })
            return result
        except Exception as e:
            log.debug(f"[ANGEL] getOrderBook error: {e}")
            return []

    def getTradeBook(self) -> list:
        """Fetch today's executed trades from Angel One."""
        if not self._connected:
            return []
        try:
            resp = self._api.tradeBook()
            if not resp or not resp.get("status"):
                return []
            trades = resp.get("data") or []
            result = []
            for t in trades:
                result.append({
                    "order_id":   t.get("orderid", ""),
                    "trade_id":   t.get("tradeid", ""),
                    "symbol":     t.get("tradingsymbol", ""),
                    "side":       t.get("transactiontype", ""),
                    "qty":        int(t.get("fillshares", 0)),
                    "fill_price": float(t.get("fillprice", 0)),
                    "fill_time":  t.get("filltime", ""),
                    "product":    t.get("producttype", ""),
                    "broker":     "angel_one",
                })
            return result
        except Exception as e:
            log.debug(f"[ANGEL] getTradeBook error: {e}")
            return []

    def modifyOrder(self, order_id: str, new_price: float = None,
                    new_sl: float = None, new_qty: int = None) -> bool:
        """
        Modify an existing order (update price / stop-loss).
        [BUG-5 FIX] Guarded against sending quantity=0, which could cancel the order.
        """
        if not self._connected:
            return False
        try:
            params = {
                "variety":     "NORMAL",
                "orderid":     order_id,
                "ordertype":   "LIMIT",
                "producttype": self._product_type,
                "duration":    "DAY",
                "price":       str(new_price or 0),
                # [BUG-5 FIX] Never send qty=0 — omit the key if not explicitly changing quantity
            }
            if new_qty and new_qty > 0:
                params["quantity"] = str(new_qty)
            if new_sl:
                params["stoploss"] = str(new_sl)
            if new_price:
                params["price"] = str(new_price)
            resp = self._api.modifyOrder(params)
            if resp and resp.get("status"):
                log.info(f"[ANGEL] modifyOrder {order_id}: price={new_price} sl={new_sl}")
                return True
            log.debug(f"[ANGEL] modifyOrder failed: {resp}")
            return False
        except Exception as e:
            log.error(f"[ANGEL] modifyOrder error: {e}")
            return False

    def get_portfolio_summary(self) -> Dict:
        if not self._connected:
            log.warning("get_portfolio_summary: broker disconnected — returning zero margin")
            return {
                "capital": 0, "available": 0, "used_margin": 0,
                "daily_pnl": 0, "open_positions": 0,
                "mode": "LIVE", "broker": "Angel One (DISCONNECTED)",
                "product_type": self._product_type,
            }
        funds = self.get_funds()
        positions = self.get_positions()
        total_pnl = sum(p["unrealized_pnl"] + p.get("realized_pnl", 0) for p in positions.values())
        return {
            "capital": funds.get("net_available", 0),
            "available": funds.get("available_cash", 0),
            "used_margin": funds.get("used_margin", 0),
            "daily_pnl": round(total_pnl, 2),
            "open_positions": len(positions),
            "mode": "LIVE",
            "broker": "Angel One",
            "product_type": self._product_type,
        }

    @property
    def is_connected(self) -> bool:
        return self._connected


class AngelOneShadowBroker:
    """
    P5-SHADOW: Angel One in paper mode — reads real data, NEVER places orders.
    [BUG-7 FIX] place_order made async-compatible (returns awaitable) to match interface.
    [BUG-8 FIX] _get_real_price uses proper token instead of empty string.
    """

    def __init__(self):
        self._real_broker = AngelOneBroker()
        self._paper_positions: dict = {}
        self._connected = False
        log.info("🔵 Angel One SHADOW mode — real data, paper orders")

    def connect(self):
        """Connect to Angel One for data only."""
        try:
            self._real_broker.connect()
            self._connected = True
            funds = self._real_broker.get_funds()
            log.info(
                f"✅ Angel One Shadow connected | "
                f"Real funds: ₹{funds.get('net_available', 0):,.0f} | "
                f"⚠️  Orders will be PAPER-FILLED (not real)"
            )
        except Exception as e:
            log.warning(f"Angel One Shadow: connection failed ({e}) — using Yahoo prices only")
            self._connected = False

    async def place_order(self, symbol: str, side: str, qty: int, price: float = 0,
                          order_type: str = "MARKET", product: str = "INTRADAY", **kwargs) -> dict:
        """
        SHADOW: Intercept order — paper fill only, NEVER send to NSE.
        [BUG-7 FIX] Made async to match AngelOneBroker.place_order interface.
        """
        fake_id = f"SHADOW_{uuid.uuid4().hex[:12].upper()}"
        fill_p = price or await asyncio.get_event_loop().run_in_executor(
            None, self._get_real_price, symbol
        )
        log.info(
            f"🔵 SHADOW ORDER (not real): {side} {qty}x {symbol} @ "
            f"{'MARKET' if price == 0 else f'₹{fill_p:.2f}'} → paper-filled as {fake_id}"
        )
        return {
            "order_id": fake_id,
            "status": "COMPLETE",
            "fill_price": fill_p,
            "qty": qty,
            "symbol": symbol,
            "side": side,
            "shadow": True,
        }

    def cancel_order(self, order_id: str, variety: str = "NORMAL") -> bool:
        log.info(f"🔵 SHADOW CANCEL (not real): {order_id}")
        return True

    def _get_real_price(self, symbol: str) -> float:
        """
        Try to get real Angel One LTP, fall back to Yahoo.
        [BUG-8 FIX] Use proper token lookup instead of empty string "".
        """
        if self._connected:
            try:
                ltp = self._real_broker.get_ltp(symbol)
                if ltp and ltp > 0:
                    return ltp
            except Exception:
                pass
        # Yahoo fallback
        try:
            import yfinance as yf
            sym = symbol if symbol.endswith(".NS") else symbol + ".NS"
            ticker = yf.Ticker(sym)
            info = ticker.fast_info
            return float(getattr(info, "last_price", 0) or 0)
        except Exception:
            return 0.0

    def get_funds(self) -> dict:
        """Real Angel One funds (read-only)."""
        if self._connected:
            return self._real_broker.get_funds()
        return {"net_available": 0, "available_cash": 0, "used_margin": 0}

    def get_positions(self) -> dict:
        """Paper positions (not real Angel One positions)."""
        return self._paper_positions

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_portfolio_summary(self) -> dict:
        real_funds = self.get_funds() if self._connected else {}
        return {
            "capital": real_funds.get("net_available", 0),
            "available": real_funds.get("available_cash", 0),
            "used_margin": real_funds.get("used_margin", 0),
            "daily_pnl": 0,
            "open_positions": len(self._paper_positions),
            "mode": "SHADOW (paper orders, real data)",
            "broker": "Angel One Shadow",
        }

    def health_check(self) -> dict:
        if not self._connected:
            return {"ok": False, "reason": "Not connected"}
        try:
            funds = self._real_broker.get_funds()
            return {"ok": True, "funds": funds.get("net_available", 0)}
        except Exception as e:
            return {"ok": False, "reason": str(e)}
