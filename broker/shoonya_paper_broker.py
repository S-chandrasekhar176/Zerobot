# -*- coding: utf-8 -*-
"""
S-Paper Broker: Shoonya WebSocket data + Paper execution.
Real NSE ticks from Shoonya, zero financial risk.
"""
from core.config import cfg
from core.logger import log
from broker.paper_broker import PaperBroker


class ShoonyaPaperBroker:
    """
    S-Paper mode: Shoonya supplies live tick data.
    All order execution goes to PaperBroker (simulated, no real money).
    """
    MODE = "S-PAPER"

    def __init__(self):
        self._paper = PaperBroker(initial_capital=cfg.initial_capital)
        self._api = None
        self.is_connected = False
        log.info("[S-PAPER] Broker created — Shoonya data + Paper execution")

    def connect_or_raise(self):
        """Connect Shoonya. Raises RuntimeError if credentials missing or unreachable."""
        sc = cfg.shoonya
        if not all([sc.user_id, sc.password, sc.totp_secret, sc.vendor_code, sc.api_key]):
            missing = [k for k, v in {
                "SHOONYA_USER": sc.user_id, "SHOONYA_PASSWORD": sc.password,
                "SHOONYA_TOTP_SECRET": sc.totp_secret, "SHOONYA_VENDOR_CODE": sc.vendor_code,
                "SHOONYA_API_KEY": sc.api_key
            }.items() if not v]
            raise RuntimeError(
                f"[S-PAPER] Shoonya not configured. Missing: {', '.join(missing)}\n"
                f"  Fill in config/.env"
            )
        self._connect_shoonya_or_raise()

    def _connect_shoonya_or_raise(self):
        import socket as _sock
        try:
            s = _sock.create_connection(("shoonyatrade.finvasia.com", 443), timeout=8)
            s.close()
        except OSError:
            raise RuntimeError(
                "[S-PAPER] Cannot reach shoonyatrade.finvasia.com:443\n"
                "  Network unreachable. Steps to diagnose:\n"
                "  1. Open https://shoonyatrade.finvasia.com in your browser\n"
                "     -> If it opens: Add python.exe to Windows Firewall exceptions\n"
                "     -> If it doesn't: Your ISP is blocking it — try mobile hotspot\n"
                "  2. Use A-PAPER mode instead (Angel One data, same paper execution)"
            )
        try:
            import pyotp
            from NorenRestApiPy.NorenApi import NorenApi
        except ImportError as e:
            raise RuntimeError(
                f"[S-PAPER] Missing dependency: {e}\n"
                f"  Run: pip install NorenRestApiPy pyotp"
            )

        sc = cfg.shoonya
        import hashlib
        pwd_hash = hashlib.sha256(sc.password.encode()).hexdigest()
        totp = pyotp.TOTP(sc.totp_secret).now()

        from broker.shounya import ShounyaBroker
        self._shoonya_raw = ShounyaBroker()
        ok = self._shoonya_raw.connect()
        if not ok:
            raise RuntimeError(
                "[S-PAPER] Shoonya login failed — check credentials:\n"
                "  SHOONYA_USER, SHOONYA_PASSWORD, SHOONYA_TOTP_SECRET,\n"
                "  SHOONYA_VENDOR_CODE, SHOONYA_API_KEY in config/.env"
            )
        self._api = self._shoonya_raw._api
        self.is_connected = True
        log.info(f"[S-PAPER] Shoonya CONNECTED | User: {sc.user_id}")
        log.info("[S-PAPER] Paper execution active — NO real orders will be sent")

    # ── Order interface → delegates to PaperBroker ────────────────────────
    async def place_order(self, symbol, side, qty, cmp, **kwargs):
        return await self._paper.place_order(symbol, side, qty, cmp, **kwargs)

    async def square_off_all(self):
        return await self._paper.square_off_all()

    def get_ltp(self, symbol):
        return self._paper.get_ltp(symbol)

    def get_funds(self):
        return self._paper.get_funds()

    def get_positions(self):
        return self._paper.get_positions()

    def get_orders(self):
        return self._paper.get_orders()

    def get_portfolio_summary(self):
        s = self._paper.get_portfolio_summary()
        s["mode"] = self.MODE
        s["data_source"] = "shoonya_ws"
        s["execution"] = "paper"
        return s
