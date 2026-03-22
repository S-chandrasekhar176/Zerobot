# -*- coding: utf-8 -*-
"""
A-Paper Broker: Angel One WebSocket data + Paper execution.
Real NSE ticks from Angel One, zero financial risk.
"""
from core.config import cfg
from core.logger import log
from broker.paper_broker import PaperBroker
from broker.angel_one import AngelOneBroker


class AngelPaperBroker:
    """
    A-Paper mode: Angel One supplies live tick data.
    All order execution goes to PaperBroker (simulated, no real money).
    """
    MODE = "A-PAPER"

    def __init__(self):
        self._paper = PaperBroker(initial_capital=cfg.initial_capital)
        self._angel: AngelOneBroker | None = None
        self._connected = False
        log.info("[A-PAPER] Broker created — Angel One data + Paper execution")

    def connect_or_raise(self):
        """Connect Angel One. Raises RuntimeError if credentials missing or login fails."""
        if not cfg.angel_one.is_configured:
            missing = cfg.angel_one.missing_fields
            raise RuntimeError(
                f"[A-PAPER] Angel One not configured. Missing: {', '.join(missing)}\n"
                f"  Fill in config/.env:\n"
                f"    ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_MPIN, ANGEL_TOTP_SECRET"
            )
        self._angel = AngelOneBroker()
        self._angel.connect()   # raises on failure
        if not self._angel._connected:
            raise RuntimeError("[A-PAPER] Angel One connect() returned but _connected=False")
        self._connected = True
        log.info(f"[A-PAPER] Angel One CONNECTED | Client: {cfg.angel_one.client_id}")
        log.info("[A-PAPER] Paper execution active — NO real orders will be sent")

    # ── Proxy token attributes for AngelOneRealtimeFeed ───────────────────
    @property
    def _token(self):
        return getattr(self._angel, '_token', None)

    @property
    def _feed_token(self):
        return getattr(self._angel, '_feed_token', None)

    @property
    def _api(self):
        return getattr(self._angel, '_api', None)

    # ── Order interface → delegates to PaperBroker ────────────────────────
    async def place_order(self, symbol, side, qty, cmp, **kwargs):
        price = self._angel.get_ltp(symbol) if self._angel else cmp
        return await self._paper.place_order(symbol, side, qty, price or cmp, **kwargs)

    async def square_off_all(self):
        return await self._paper.square_off_all()

    def get_ltp(self, symbol):
        return (self._angel.get_ltp(symbol) if self._angel else None) or \
               self._paper.get_ltp(symbol)

    def get_funds(self):
        return self._paper.get_funds()

    def get_positions(self):
        return self._paper.get_positions()

    def get_orders(self):
        return self._paper.get_orders()

    def get_portfolio_summary(self):
        s = self._paper.get_portfolio_summary()
        s["mode"] = self.MODE
        s["data_source"] = "angel_one_ws"
        s["execution"] = "paper"
        return s
