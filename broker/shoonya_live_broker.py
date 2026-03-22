# -*- coding: utf-8 -*-
"""
S-Live Broker: Shoonya WebSocket data + Shoonya REAL order execution.
WARNING: Real money mode. Orders go to NSE via Shoonya.
"""
from core.logger import log
from core.config import cfg


class ShoonyaLiveBroker:
    MODE = "S-LIVE"

    def __init__(self):
        self._api = None
        self.is_connected = False
        self._paper = None   # no paper fallback in live mode
        log.info("[S-LIVE] Broker created — Shoonya data + Shoonya REAL execution")

    def connect_or_raise(self):
        import socket as _sock
        try:
            s = _sock.create_connection(("shoonyatrade.finvasia.com", 443), timeout=8)
            s.close()
        except OSError:
            raise RuntimeError(
                "[S-LIVE] Cannot reach shoonyatrade.finvasia.com:443\n"
                "  Shoonya server unreachable. Check network/firewall."
            )
        from broker.shounya import ShounyaBroker
        raw = ShounyaBroker()
        ok = raw.connect()
        if not ok:
            raise RuntimeError("[S-LIVE] Shoonya login failed — check credentials in config/.env")
        self._api = raw._api
        self.is_connected = True
        log.info(f"[S-LIVE] Shoonya CONNECTED | User: {cfg.shoonya.user_id}")
        log.info("[S-LIVE] REAL MONEY MODE — orders will be sent to NSE via Shoonya!")

    async def place_order(self, symbol, side, qty, cmp, **kwargs):
        """Place a REAL order via Shoonya."""
        if not self.is_connected or not self._api:
            raise RuntimeError("[S-LIVE] place_order called but Shoonya not connected")
        # Convert side
        buy_sell = 'B' if side.upper() == 'BUY' else 'S'
        # Convert symbol to Shoonya format (INFY-EQ for NSE equity)
        sym = symbol.replace(".NS", "") + "-EQ"
        ret = self._api.place_order(
            buy_or_sell=buy_sell,
            product_type='I',           # Intraday
            exchange='NSE',
            tradingsymbol=sym,
            quantity=qty,
            discloseqty=0,
            price_type='MKT',
            price=0,
            trigger_price=None,
            retention='DAY',
            remarks=f"ZeroBot_{kwargs.get('strategy','')}"
        )
        log.info(f"[S-LIVE] Order placed: {side} {sym} x{qty} | result={ret}")
        return ret

    async def square_off_all(self):
        log.info("[S-LIVE] square_off_all — closing all Shoonya positions")

    def get_ltp(self, symbol):
        return None

    def get_funds(self):
        if not self._api:
            return {}
        try:
            ret = self._api.get_limits()
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
        return {"mode": self.MODE, "data_source": "shoonya_ws", "execution": "shoonya_real"}
