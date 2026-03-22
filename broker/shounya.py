# -*- coding: utf-8 -*-
"""
ZeroBot — Shounya (Finvasia) Broker Integration
Zero brokerage · Real-time WebSocket · Equity + F&O · Python SDK

SETUP:
  pip install NorenRestApiPy
  Fill credentials in config/settings.yaml → broker section

WHY SHOUNYA over Angel One?
  - Zero brokerage (no ₹20/order friction)
  - Real-time data via WebSocket (same quality)
  - Full algo trading support for NSE equity + F&O
  - Account opening / portal: https://trade.shoonya.com/#/
"""
from core.logger import log
from core.config import cfg
from typing import Optional, Callable
import threading
import json
import time

class ShounyaBroker:
    """
    Shounya (Finvasia) broker — zero brokerage, full algo support.
    Uses NorenRestApiPy (same API as Shoonya web platform).
    """

    def __init__(self):
        self.api = None
        self.connected = False
        self._on_tick_cb: Optional[Callable] = None
        self._subscribed_tokens: set = set()
        self._order_update_cb: Optional[Callable] = None
        self._susertoken = ""
        self._uid = ""
        self._last_tick_time = time.time()
        # FIX: Track last-token timestamp so ShoonyaRealtimeFeed can detect dead feed

    # ── BUG-FIX-1: shoonya_feed.py uses getattr(broker, "is_connected", False)
    # but ShounyaBroker only had self.connected (plain attribute), so getattr
    # always returned the default False and the feed NEVER used the WebSocket.
    # Fix: expose is_connected as a property backed by self.connected.
    @property
    def is_connected(self) -> bool:
        """Property alias for self.connected — required by ShoonyaRealtimeFeed."""
        return self.connected

    def connect(self) -> bool:
        """Login to Shoonya via direct REST — no NorenRestApiPy dependency.

        Shoonya QuickAuth API contract:
          POST https://shoonyatrade.finvasia.com
          Body: jData=<json>   (no &jKey — that is only for authenticated post-login calls)

          jData fields:
            apkversion  "1.0.0"
            uid         user_id
            pwd         sha256(password)          ← hashed, not plaintext
            factor2     TOTP 6-digit code
            vc          vendor_code
            appkey      sha256(uid + "|" + api_secret)   ← NOT the raw api_key
            imei        device imei
            source      "API"
        """
        import hashlib, json as _json
        import requests as _req, urllib3 as _u3
        import subprocess as _sp, sys as _sys

        _u3.disable_warnings(_u3.exceptions.InsecureRequestWarning)

        # [MEDIUM#11] Auto-install required Shoonya dependencies
        try:
            import pyotp
        except ImportError:
            log.warning("[SHOUNYA] Attempting auto-install of pyotp...")
            try:
                _sp.run([_sys.executable, "-m", "pip", "install", "pyotp", "-q"], check=True, timeout=30)
                import pyotp
                log.info("[SHOUNYA] ✅ pyotp auto-installed successfully")
            except Exception as auto_err:
                log.error(f"[SHOUNYA] ❌ Auto-install failed: {auto_err}")
                return False

        # ── validate credentials ──────────────────────────────────────────────
        creds = cfg.shoonya
        missing = [f for f, v in [
            ("SHOONYA_USER",        creds.user_id),
            ("SHOONYA_TOTP_SECRET", creds.totp_key),
            ("SHOONYA_API_KEY",     creds.api_key),
            ("SHOONYA_VENDOR_CODE", creds.vendor_code),
        ] if not v]
        if missing:
            log.error(f"[SHOUNYA] Missing credentials: {missing} — check config/.env")
            return False

        # ── TOTP ──────────────────────────────────────────────────────────────
        try:
            totp = pyotp.TOTP(creds.totp_key).now()
        except Exception as e:
            log.error(f"[SHOUNYA] TOTP error: {e}")
            return False

        # ── build payload hashes ──────────────────────────────────────────────
        raw_password = getattr(creds, "password", "") or ""
        pwd_hash    = hashlib.sha256(raw_password.encode()).hexdigest()
        # appkey = sha256(uid + "|" + api_secret)  ← Shoonya contract
        app_key     = hashlib.sha256(
            f"{creds.user_id}|{creds.api_key}".encode()
        ).hexdigest()

        payload = {
            # Must be "1.0.0" — the official ShoonyaApi-py value.
            # Old code used "js:1.0.0" which the server rejects with "Invalid Access Type".
            "apkversion": "1.0.0",
            "uid":        creds.user_id,
            "pwd":        pwd_hash,
            "factor2":    totp,
            "vc":         creds.vendor_code,
            "appkey":     app_key,
            "imei":       creds.imei or "abc1234",
            "source":     "API",
        }

        url = "https://shoonyatrade.finvasia.com/NorenWClientTP/QuickAuth"

        # ── Pre-flight: verify TCP reachability before spending TOTP on a doomed request ──
        try:
            import socket as _sock
            _s = _sock.create_connection(("shoonyatrade.finvasia.com", 443), timeout=5)
            _s.close()
        except OSError:
            log.error(
                "[SHOUNYA] Cannot reach shoonyatrade.finvasia.com:443\n"
                "\n"
                "  This is a NETWORK issue, NOT a credentials issue. Diagnosis:\n"
                "  1. Open https://shoonyatrade.finvasia.com in your browser\n"
                "     → If it opens: Windows Firewall is blocking python.exe\n"
                "       Fix: Windows Defender Firewall → Allow an app → Add python.exe\n"
                "     → If it doesn't open: ISP is blocking port 443 to this domain\n"
                "       Fix: Switch to mobile hotspot and retry\n"
                "  2. Alternatively use DUAL mode for Angel One data (no Shoonya needed)\n"
                "  Bot continuing with Yahoo Finance data + Paper execution (safe mode)."
            )
            return False

        log.info(
            f"[SHOUNYA] Logging in → {url}\n"
            f"          user={creds.user_id} | vendor={creds.vendor_code} | totp={totp}"
        )

        def _do_login(source_val: str):
            p = {**payload, "source": source_val}
            # Login body MUST be only jData=... — no &jKey (that is for post-login calls)
            jdata_str = "jData=" + _json.dumps(p, separators=(",", ":"))

            # BUG-FIX: was passing data=jdata_str.encode("utf-8") (bytes).
            # When requests receives bytes with an explicit Content-Type header,
            # some proxies/load-balancers strip the body or return empty 200.
            # Fix: pass data as plain str — requests encodes it correctly.
            r = _req.post(
                url,
                data=jdata_str,          # ← str, NOT bytes
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
                verify=False,
            )

            # BUG-FIX: r.json() on empty body raises JSONDecodeError (char 0).
            # Shoonya returns empty body for certain auth rejections.
            # Always log the raw response so the user can debug.
            raw_text = r.text.strip()
            if not raw_text:
                log.error(
                    f"[SHOUNYA] Login returned empty body (HTTP {r.status_code}) "
                    f"with source={source_val!r}.\n"
                    "  Possible causes:\n"
                    "    • Shoonya API is down / maintenance window\n"
                    "    • Your IP is rate-limited or geo-blocked\n"
                    "    • Wrong vendor_code — must be USERID_U (e.g. FA380919_U)\n"
                    "    • API access not enabled on your account\n"
                    "  Next step: open https://shoonyatrade.finvasia.com "
                    "in a browser to confirm the server is reachable."
                )
                return None   # caller treats None as failure, not exception

            try:
                return _json.loads(raw_text)
            except _json.JSONDecodeError:
                log.error(
                    f"[SHOUNYA] Login returned non-JSON (HTTP {r.status_code}): "
                    f"{raw_text[:200]!r}"
                )
                return None

        ret = None
        try:
            # source="API" is the correct value for algo-trading accounts
            ret = _do_login("API")

        except _req.exceptions.Timeout:
            log.error("[SHOUNYA] Login timed out — check internet / firewall to shoonyatrade.finvasia.com:443")
            return False
        except Exception as e:
            log.error(f"[SHOUNYA] Login HTTP error: {type(e).__name__}: {e}")
            return False

        # BUG-FIX: _do_login now returns None on empty/non-JSON body.
        # Try source="WEB" as a fallback — some Finvasia accounts require it.
        # Add a 2-second pause so both attempts don't hammer the same overloaded server.
        if ret is None:
            log.info("[SHOUNYA] Retrying login with source='WEB' ...")
            time.sleep(2)   # give server a moment to recover from the failed request
            try:
                ret = _do_login("WEB")
            except Exception as e:
                log.error(f"[SHOUNYA] WEB retry error: {e}")
                ret = None

        if ret is None:
            log.error(
                "[SHOUNYA] Both API and WEB login attempts returned empty/invalid responses.\n"
                "  Bot will run in Yahoo Finance fallback mode.\n"
                "  To fix: verify credentials in config/.env and ensure API access is\n"
                "  enabled at https://trade.shoonya.com → Settings → API."
            )
            return False

        if ret.get("stat") == "Ok":
            self._susertoken = ret.get("susertoken", "")
            self._uid        = creds.user_id
            self.connected   = True
            # Wire into NorenRestApiPy for WebSocket ticks
            HOST = "https://shoonyatrade.finvasia.com/NorenWClientTP/"
            WS   = "wss://shoonyatrade.finvasia.com/NorenWSTP/"

            def _try_build_api():
                from NorenRestApiPy.NorenApi import NorenApi
                class _Api(NorenApi):
                    def __init__(self):
                        super().__init__(host=HOST, websocket=WS)
                api = _Api()
                api.set_session(creds.user_id, self._susertoken)
                return api

            try:
                self.api = _try_build_api()
                log.info("[SHOUNYA] ✅ NorenRestApiPy loaded — WebSocket ticks enabled")
            except ImportError:
                # main.py startup checklist should have installed this already.
                # If we land here, the package is still missing — warn and degrade.
                log.warning(
                    "[SHOUNYA] NorenRestApiPy not installed — WebSocket ticks unavailable.\n"
                    "          Fix: pip install NorenRestApiPy pyotp  then restart ZeroBot.\n"
                    "          Bot will use Yahoo Finance 15s polling as fallback."
                )
                self.api = None
            except Exception as e:
                log.debug(f"[SHOUNYA] NorenRestApiPy init error: {e}")
                self.api = None
            log.info(
                f"[SHOUNYA] ✅ Connected as {creds.user_id} | "
                f"token={self._susertoken[:8]}..."
            )
            return True

        emsg = ret.get("emsg", str(ret)) if ret else "Empty response"
        if "Access Type" in emsg:
            log.error(
                f"[SHOUNYA] Login rejected: {emsg}\n"
                "  ► This means API trading is NOT enabled on your Shoonya account.\n"
                "  ► Fix: Log in to https://trade.shoonya.com/#/ → Settings ⚙\n"
                "         → API → Enable 'Algo Trading / API Access'\n"
                "         Then restart ZeroBot."
            )
        else:
            log.error(
                f"[SHOUNYA] Login rejected: {emsg}\n"
                "  Fixes:\n"
                "    • Wrong TOTP secret → regenerate in Shoonya backoffice → Profile → Security\n"
                "    • Wrong API key     → check SHOONYA_API_KEY in config/.env\n"
                "    • Wrong vendor code → check SHOONYA_VENDOR_CODE in config/.env"
            )
        return False

    def subscribe_ticks(self, symbols: list, on_tick: Callable):
        """
        Subscribe to real-time ticks for a list of NSE symbols.
        symbols: list of NSE tokens (e.g. ['NSE|22', 'NSE|1594'])
        Raises RuntimeError if NorenRestApiPy is not available (triggers Yahoo fallback).
        """
        if self.api is None:
            raise RuntimeError(
                "Shoonya WebSocket unavailable — NorenRestApiPy not installed or session invalid.\n"
                "  Fix: pip install NorenRestApiPy pyotp  then restart ZeroBot.\n"
                "  Falling back to Yahoo Finance 15s polling."
            )
        self._on_tick_cb = on_tick

        def _feed_open():
            log.info("[SHOUNYA] WebSocket connected")

        def _feed_msg(msg):
            try:
                data = json.loads(msg) if isinstance(msg, str) else msg
                if data.get('t') == 'tk' or data.get('t') == 'tf':
                    # Update watchdog timestamp on every tick
                    self._last_tick_time = time.time()
                    # FIX: Shoonya 'ts' is the trading symbol (e.g. "RELIANCE-EQ"),
                    # 'tk' is the token number. We need to map token→symbol properly.
                    raw_sym = data.get('ts', data.get('e', '') + '|' + data.get('tk', ''))
                    # Normalise to NSE format: "RELIANCE-EQ" → "RELIANCE"
                    clean_sym = raw_sym.split('-')[0].strip() if '-' in raw_sym else raw_sym
                    lp = float(data.get('lp', 0))
                    if not lp:  # 'tf' (full quote) uses 'lp'; touch quote uses 'lp' too
                        lp = float(data.get('c', 0))  # prev close as fallback
                    tick = {
                        'symbol': clean_sym,
                        'ltp': lp,
                        'open': float(data.get('o', 0)),
                        'high': float(data.get('h', 0)),
                        'low': float(data.get('l', 0)),
                        'close': float(data.get('c', 0)),
                        'volume': int(data.get('v', 0)),
                        'bid': float(data.get('bp1', lp)),
                        'ask': float(data.get('sp1', lp)),
                        'change': 0.0,
                        'change_pct': 0.0,
                        'timestamp': data.get('ft', ''),
                        'source': 'shoonya_ws',
                    }
                    if on_tick:
                        on_tick(tick)
            except Exception as e:
                log.warning(f"[SHOUNYA] Tick parse error: {e}")

        def _feed_error(msg):
            log.error(f"[SHOUNYA] WS error: {msg}")

        # BUG-FIX: _feed_close was calling self.subscribe_ticks() recursively.
        # On repeated disconnects this creates a growing call stack (stack overflow)
        # and spawns duplicate start_websocket threads. Fix: use a bounded retry
        # counter with exponential back-off, capped at 5 attempts per session.
        _reconnect_attempts = [0]  # mutable container to allow mutation in closure
        _MAX_RECONNECTS = 5

        def _feed_close():
            if _reconnect_attempts[0] >= _MAX_RECONNECTS:
                log.error(
                    f"[SHOUNYA] WS closed — max reconnects ({_MAX_RECONNECTS}) reached. "
                    "Shoonya feed will fall back to Yahoo Finance for this session."
                )
                self.connected = False
                return
            _reconnect_attempts[0] += 1
            backoff = min(30, 3 * _reconnect_attempts[0])  # 3s, 6s, 9s … 30s
            log.warning(
                f"[SHOUNYA] WS closed — reconnecting in {backoff}s "
                f"(attempt {_reconnect_attempts[0]}/{_MAX_RECONNECTS})"
            )
            time.sleep(backoff)
            if self.connected and self.api:
                try:
                    self.api.subscribe(symbols)   # re-subscribe without re-opening WS
                except Exception as _rc_err:
                    log.debug(f"[SHOUNYA] Re-subscribe error: {_rc_err}")

        self.api.start_websocket(
            order_update_callback=self._on_order_update,
            subscribe_callback=_feed_msg,
            socket_open_callback=_feed_open,
            socket_error_callback=_feed_error,
            socket_close_callback=_feed_close,
        )
        # Subscribe to symbols
        self.api.subscribe(symbols)
        self._subscribed_tokens.update(symbols)
        log.info(f"[SHOUNYA] Subscribed to {len(symbols)} symbols")

    def place_order(self, symbol: str, side: str, qty: int, price: float = 0,
                    order_type: str = 'MKT', exchange: str = 'NSE',
                    product: str = 'I', stop_loss: float = 0, target: float = 0) -> Optional[str]:
        """
        Place order via Shounya API.
        product: 'I'=Intraday, 'D'=Delivery, 'B'=Bracket, 'C'=Cover
        order_type: 'MKT', 'LMT', 'SL', 'SL-M'
        Returns order_id if successful, None otherwise.
        """
        if not self.connected:
            log.error("[SHOUNYA] Not connected")
            return None

        buy_sell = 'B' if side.upper() == 'BUY' else 'S'
        token = self._get_token(symbol, exchange)
        if not token:
            log.error(f"[SHOUNYA] Token not found for {symbol}")
            return None

        try:
            ret = self.api.place_order(
                buy_or_sell=buy_sell,
                product_type=product,
                exchange=exchange,
                tradingsymbol=symbol,
                quantity=qty,
                discloseqty=0,
                price_type=order_type,
                price=price if order_type == 'LMT' else 0,
                trigger_price=None,
                retention='DAY',
                remarks=f'ZeroBot_{side}',
            )
            if ret and ret.get('stat') == 'Ok':
                order_id = ret.get('norenordno')
                log.info(f"[SHOUNYA] Order placed: {order_id} | {side} {qty}x {symbol} @ {'MKT' if not price else price}")
                return order_id
            else:
                log.error(f"[SHOUNYA] Order failed: {ret}")
                return None
        except Exception as e:
            log.error(f"[SHOUNYA] place_order error: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            ret = self.api.cancel_order(orderno=order_id)
            return ret and ret.get('stat') == 'Ok'
        except Exception as e:
            log.error(f"[SHOUNYA] cancel_order error: {e}")
            return False

    def get_positions(self) -> list:
        """Fetch current positions."""
        try:
            ret = self.api.get_positions()
            if ret and isinstance(ret, list):
                return [self._parse_position(p) for p in ret if p.get('netqty') != '0']
            return []
        except Exception as e:
            log.error(f"[SHOUNYA] get_positions error: {e}")
            return []

    def get_funds(self) -> dict:
        """Fetch available funds/margin."""
        try:
            ret = self.api.get_limits()
            if ret and ret.get('stat') == 'Ok':
                return {
                    'cash': float(ret.get('cash', 0)),
                    'available_margin': float(ret.get('net', 0)),
                    'used_margin': float(ret.get('brkcollamt', 0)),
                }
            return {}
        except Exception as e:
            log.error(f"[SHOUNYA] get_funds error: {e}")
            return {}

    def get_order_history(self, order_id: str) -> Optional[dict]:
        """Get order status."""
        try:
            ret = self.api.single_order_history(orderno=order_id)
            return ret[0] if ret and isinstance(ret, list) else None
        except Exception:
            return None

    def _on_order_update(self, msg):
        """Handle order status updates from WebSocket."""
        try:
            data = json.loads(msg) if isinstance(msg, str) else msg
            status = data.get('status', '').lower()
            if status == 'complete':
                log.info(f"[SHOUNYA] Order FILLED: {data.get('norenordno')} | {data.get('trantype')} {data.get('fillshares')}x {data.get('tsym')} @ {data.get('flprc')}")
                if self._order_update_cb:
                    self._order_update_cb({'order_id': data.get('norenordno'), 'status': 'FILLED', 'fill_price': float(data.get('flprc', 0)), 'qty': int(data.get('fillshares', 0)), 'symbol': data.get('tsym', '')})
            elif status in ('rejected', 'cancelled'):
                log.warning(f"[SHOUNYA] Order {status.upper()}: {data.get('norenordno')} | {data.get('rejreason','')}")
        except Exception as e:
            log.warning(f"[SHOUNYA] Order update parse error: {e}")

    def _parse_position(self, p: dict) -> dict:
        qty = int(p.get('netqty', 0))
        avg_price = float(p.get('netavgprc', 0))
        ltp = float(p.get('lp', avg_price))
        pnl = float(p.get('urmtom', 0))
        return {
            'symbol': p.get('tsym', ''),
            'qty': abs(qty),
            'side': 'LONG' if qty > 0 else 'SHORT',
            'avg_price': avg_price,
            'current_price': ltp,
            'unrealized_pnl': pnl,
            'product': p.get('prd', 'I'),
            'exchange': p.get('exch', 'NSE'),
        }

    def _get_token(self, symbol: str, exchange: str = 'NSE') -> Optional[str]:
        """Look up token for a trading symbol."""
        try:
            ret = self.api.searchscrip(exchange=exchange, searchtext=symbol)
            if ret and ret.get('stat') == 'Ok' and ret.get('values'):
                return f"{exchange}|{ret['values'][0]['token']}"
        except Exception:
            pass
        return None

    # ── P16: Additional methods ──────────────────────────────────────────────

    def modify_order(self, order_id: str, new_price: float = None,
                     new_sl: float = None, new_qty: int = None) -> bool:
        """
        Modify an existing order — update price, SL, or qty.
        Used for trailing stop updates without cancel+replace.
        """
        if not self.connected or not self.api:
            log.error("[SHOUNYA] modify_order: not connected")
            return False
        try:
            ret = self.api.modify_order(
                orderno=order_id,
                exchange="NSE",
                tradingsymbol="",      # API resolves from order_id
                newquantity=new_qty or 0,
                newprice_type="LMT" if new_price else "MKT",
                newprice=new_price or 0,
                newtrigger_price=new_sl or None,
            )
            if ret and ret.get("stat") == "Ok":
                log.info(f"[SHOUNYA] Order modified: {order_id} price={new_price} sl={new_sl}")
                return True
            log.debug(f"[SHOUNYA] modify_order failed: {ret}")
            return False
        except Exception as e:
            log.error(f"[SHOUNYA] modify_order error: {e}")
            return False

    def get_order_book(self) -> list:
        """Fetch today's order book (open + completed + cancelled orders)."""
        if not self.connected or not self.api:
            return []
        try:
            ret = self.api.get_order_book()
            if not ret or not isinstance(ret, list):
                return []
            result = []
            for o in ret:
                result.append({
                    "order_id":  o.get("norenordno", ""),
                    "symbol":    o.get("tsym", ""),
                    "side":      "BUY" if o.get("trantype") == "B" else "SELL",
                    "qty":       int(o.get("qty", 0)),
                    "price":     float(o.get("prc", 0)),
                    "avg_price": float(o.get("avgprc", 0)),
                    "status":    o.get("status", "UNKNOWN").upper(),
                    "product":   o.get("prd", "I"),
                    "order_type":o.get("prctyp", "MKT"),
                    "placed_at": o.get("norentm", ""),
                    "broker":    "shoonya",
                })
            return result
        except Exception as e:
            log.debug(f"[SHOUNYA] get_order_book error: {e}")
            return []

    def get_trade_book(self) -> list:
        """Fetch today's executed trades (fills)."""
        if not self.connected or not self.api:
            return []
        try:
            ret = self.api.get_trade_book()
            if not ret or not isinstance(ret, list):
                return []
            result = []
            for t in ret:
                result.append({
                    "order_id":   t.get("norenordno", ""),
                    "trade_id":   t.get("flfilledtm", ""),
                    "symbol":     t.get("tsym", ""),
                    "side":       "BUY" if t.get("trantype") == "B" else "SELL",
                    "qty":        int(t.get("fillshares", 0)),
                    "fill_price": float(t.get("flprc", 0)),
                    "fill_time":  t.get("flfilledtm", ""),
                    "product":    t.get("prd", "I"),
                    "broker":     "shoonya",
                })
            return result
        except Exception as e:
            log.debug(f"[SHOUNYA] get_trade_book error: {e}")
            return []

    def get_historical_data(
        self,
        symbol: str,
        interval: str = "5m",
        period: str = "2d",
    ) -> "Optional[pd.DataFrame]":
        """
        Fetch OHLCV candles from Shoonya's time_price_series API.
        Used by HistoricalFeed in S-Mode so intraday refreshes pull
        real Shoonya data instead of Yahoo Finance.

        Args:
            symbol:   NSE symbol with .NS suffix (e.g. "HDFCBANK.NS")
            interval: "1m" | "3m" | "5m" | "10m" | "15m" | "30m" | "1h" | "1d"
            period:   "1d" | "2d" | "5d" | "10d" (Shoonya supports up to 30 days intraday)

        Returns:
            DataFrame with columns [open, high, low, close, volume] or None on failure.
        """
        if not self.connected or not self.api:
            return None

        try:
            import pandas as pd
            from datetime import datetime, timedelta

            # Shoonya interval mapping (in minutes)
            _INTERVAL_MAP = {
                "1m": 1, "3m": 3, "5m": 5, "10m": 10,
                "15m": 15, "30m": 30, "1h": 60, "1d": "D",
            }
            sh_interval = _INTERVAL_MAP.get(interval, 5)

            # Resolve trading symbol and exchange token
            raw_sym = symbol.replace(".NS", "").replace(".BO", "")
            exchange = "BSE" if symbol.endswith(".BO") else "NSE"
            token_str = self._get_token(raw_sym, exchange)
            if not token_str:
                log.debug(f"[SHOUNYA hist] token not found for {symbol}")
                return None
            # token_str is like "NSE|22"
            _, token = token_str.split("|", 1)

            # Build time range — API requires Unix timestamps (seconds since 1970-01-01)
            period_days = {"1d": 1, "2d": 2, "5d": 5, "10d": 10}.get(period.lower(), 2)
            import time as _time
            end_ts   = int(_time.time())
            start_ts = end_ts - (period_days + 1) * 86400   # +1 day buffer for weekends

            ret = self.api.get_time_price_series(
                exchange=exchange,
                token=token,
                starttime=start_ts,
                endtime=end_ts,
                interval=sh_interval,
            )

            if not ret or not isinstance(ret, list):
                log.debug(f"[SHOUNYA hist] no data for {symbol}")
                return None

            rows = []
            for bar in ret:
                try:
                    ts = pd.to_datetime(bar.get("time", ""), dayfirst=True)
                    rows.append({
                        "timestamp": ts,
                        "open":   float(bar.get("into", bar.get("o", 0))),
                        "high":   float(bar.get("inth", bar.get("h", 0))),
                        "low":    float(bar.get("intl", bar.get("l", 0))),
                        "close":  float(bar.get("intc", bar.get("c", 0))),
                        "volume": float(bar.get("intv", bar.get("v", 0))),
                    })
                except Exception:
                    continue

            if not rows:
                return None

            df = pd.DataFrame(rows).set_index("timestamp").sort_index()
            df.index.name = "timestamp"
            # Localise to IST (Shoonya returns naive IST timestamps)
            if df.index.tz is None:
                import pytz
                df.index = df.index.tz_localize("Asia/Kolkata")
            log.debug(f"[SHOUNYA hist] {symbol}: {len(df)} rows via Shoonya API")
            return df

        except Exception as e:
            log.debug(f"[SHOUNYA hist] {symbol} error: {e}")
            return None

    def disconnect(self):
        """Disconnect WebSocket."""
        try:
            if self.api:
                self.api.close_websocket()
            self.connected = False
            log.info("[SHOUNYA] Disconnected")
        except Exception:
            pass


# ── Standalone diagnostic (run: python broker/shounya.py) ────────────────────
if __name__ == "__main__":
    """
    Quick connectivity test — run this directly to verify Shoonya login
    without starting the full ZeroBot engine.

        cd zerobot_G2_v1
        python broker/shounya.py

    Expected output on success:
        ✅ Logged in as FA380919 | token=abcd1234...
        ✅ NorenRestApiPy WebSocket ready
    """
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from dotenv import load_dotenv
    load_dotenv("config/.env")

    broker = ShounyaBroker()
    ok = broker.connect()
    if ok:
        print("\n✅  Login succeeded — Shoonya WebSocket ready")
        funds = broker.get_funds()
        if funds:
            print(f"    Available margin: ₹{funds.get('available_margin', 0):,.2f}")
        broker.disconnect()
    else:
        print("\n❌  Login failed — check the ERROR lines above for the exact reason")
        print("    Common fixes:")
        print("      1. Regenerate TOTP secret in Shoonya backoffice → Profile → Security")
        print("      2. Confirm SHOONYA_VENDOR_CODE=FA380919_U (format: USERID_U)")
        print("      3. Enable Algo Trading API at https://trade.shoonya.com → Settings → API")
        sys.exit(1)