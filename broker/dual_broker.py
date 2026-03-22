# -*- coding: utf-8 -*-
"""
Dual Broker: Angel One WebSocket data + Shoonya REAL order execution.
WARNING: Real money mode. Data from Angel One; orders go via Shoonya.
"""
from core.logger import log
from core.config import cfg


class DualBroker:
    MODE = "DUAL"

    def __init__(self):
        self._angel = None
        self._shoonya_api = None
        self._connected = False
        self._angel_connected = False
        self._shoonya_connected = False
        log.info("[DUAL] Broker created — Angel One data + Shoonya REAL execution")

    def connect_or_raise(self):
        # Step 1: Angel One (data)
        if not cfg.angel_one.is_configured:
            raise RuntimeError(
                f"[DUAL] Angel One not configured. Missing: {', '.join(cfg.angel_one.missing_fields)}"
            )
        from broker.angel_one import AngelOneBroker
        self._angel = AngelOneBroker()
        self._angel.connect()
        if not self._angel._connected:
            raise RuntimeError("[DUAL] Angel One connect() failed")
        self._angel_connected = True
        log.info(f"[DUAL] Angel One CONNECTED | Client: {cfg.angel_one.client_id}")

        # Step 2: Shoonya (execution)
        import socket as _sock
        try:
            s = _sock.create_connection(("shoonyatrade.finvasia.com", 443), timeout=8)
            s.close()
        except OSError:
            raise RuntimeError("[DUAL] Cannot reach shoonyatrade.finvasia.com:443")
        from broker.shounya import ShounyaBroker
        raw = ShounyaBroker()
        ok = raw.connect()
        if not ok:
            raise RuntimeError("[DUAL] Shoonya login failed — check credentials in config/.env")
        self._shoonya_api = raw._api
        self._shoonya_connected = True
        self._connected = True
        log.info(f"[DUAL] Shoonya CONNECTED | User: {cfg.shoonya.user_id}")
        log.info("[DUAL] REAL MONEY MODE — Angel One data | Shoonya order execution")

    # ── Token proxies for AngelOneRealtimeFeed ────────────────────────────
    @property
    def _token(self):
        return getattr(self._angel, '_token', None)

    @property
    def _feed_token(self):
        return getattr(self._angel, '_feed_token', None)

    @property
    def _api(self):
        return getattr(self._angel, '_api', None)

    # ── Order execution via Shoonya ────────────────────────────────────────
    async def place_order(self, symbol, side, qty, cmp, **kwargs):
        if not self._shoonya_connected:
            raise RuntimeError("[DUAL] place_order: Shoonya not connected")
        buy_sell = 'B' if side.upper() == 'BUY' else 'S'
        sym = symbol.replace(".NS", "") + "-EQ"
        ret = self._shoonya_api.place_order(
            buy_or_sell=buy_sell, product_type='I', exchange='NSE',
            tradingsymbol=sym, quantity=qty, discloseqty=0,
            price_type='MKT', price=0, trigger_price=None,
            retention='DAY', remarks=f"ZeroBot_{kwargs.get('strategy','')}"
        )
        log.info(f"[DUAL] Order: {side} {sym} x{qty} | result={ret}")
        return ret

    async def square_off_all(self):
        log.info("[DUAL] square_off_all via Shoonya")

    def get_ltp(self, symbol):
        return self._angel.get_ltp(symbol) if self._angel else None

    def get_funds(self):
        if not self._shoonya_api:
            return {}
        try:
            ret = self._shoonya_api.get_limits()
            if ret and ret.get("stat") == "Ok":
                return {"available": float(ret.get("cash", 0))}
        except Exception:
            pass
        return {}

    def get_positions(self):
        return []

    def get_orders(self):
        return []

    def get_portfolio_summary(self):
        return {"mode": self.MODE, "data_source": "angel_one_ws", "execution": "shoonya_real"}
