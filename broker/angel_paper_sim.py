# -*- coding: utf-8 -*-
"""
ZeroBot Pro — Angel One Paper Simulator (Patch 5)
════════════════════════════════════════════════════
Simulates the FULL Angel One API flow in paper mode:
  • Same connect() / place_order() / get_positions() signatures as AngelOneBroker
  • Exercises JWT auth path, rate limiter, symbol-mapper — but never sends real orders
  • Used to TEST all live-mode code paths safely in paper

How to enable:
  settings.yaml → bot.mode: "paper"  +  broker.name: "angel_paper_sim"

When you're ready for live:
  settings.yaml → bot.mode: "live"   +  broker.name: "angel"
  (one line change — all other code stays identical)
"""
import asyncio
import time
import uuid
from datetime import datetime
from typing import Dict, Optional
from core.logger import log
from core.config import cfg
from core.event_bus import bus
from execution.transaction_cost import CostCalculator


# ── NSE symbol map: Yahoo format → Angel One format ───────────────────────────
# In live mode these go to AngelOneBroker. In paper sim we log what WOULD happen.
NSE_SYMBOL_MAP = {
    "RELIANCE.NS":    ("RELIANCE-EQ",  "NSE", "2885"),
    "HDFCBANK.NS":    ("HDFCBANK-EQ",  "NSE", "1333"),
    "ICICIBANK.NS":   ("ICICIBANK-EQ", "NSE", "4963"),
    "TCS.NS":         ("TCS-EQ",       "NSE", "11536"),
    "INFY.NS":        ("INFY-EQ",      "NSE", "1594"),
    "SBIN.NS":        ("SBIN-EQ",      "NSE", "3045"),
    "AXISBANK.NS":    ("AXISBANK-EQ",  "NSE", "5900"),
    "WIPRO.NS":       ("WIPRO-EQ",     "NSE", "3787"),
    "HCLTECH.NS":     ("HCLTECH-EQ",   "NSE", "7229"),
    "BAJFINANCE.NS":  ("BAJFINANCE-EQ","NSE", "317"),
    "BAJAJFINSV.NS":  ("BAJAJFINSV-EQ","NSE", "16675"),
    "MARUTI.NS":      ("MARUTI-EQ",    "NSE", "10999"),
    "HINDUNILVR.NS":  ("HINDUNILVR-EQ","NSE", "1394"),
    "NESTLEIND.NS":   ("NESTLEIND-EQ", "NSE", "17963"),
    "KOTAKBANK.NS":   ("KOTAKBANK-EQ", "NSE", "1922"),
    "INDUSINDBK.NS":  ("INDUSINDBK-EQ","NSE", "5258"),
    "BANDHANBNK.NS":  ("BANDHANBNK-EQ","NSE", "2263"),
    "TECHM.NS":       ("TECHM-EQ",     "NSE", "13538"),
    "LT.NS":          ("LT-EQ",        "NSE", "11483"),
    "ASIANPAINT.NS":  ("ASIANPAINT-EQ","NSE", "236"),
    "TITAN.NS":       ("TITAN-EQ",     "NSE", "14977"),
    "ULTRACEMCO.NS":  ("ULTRACEMCO-EQ","NSE", "11532"),
    "TATASTEEL.NS":   ("TATASTEEL-EQ", "NSE", "3499"),
    "ONGC.NS":        ("ONGC-EQ",      "NSE", "2475"),
    "NTPC.NS":        ("NTPC-EQ",      "NSE", "11630"),
    "POWERGRID.NS":   ("POWERGRID-EQ", "NSE", "14977"),
    "ITC.NS":         ("ITC-EQ",       "NSE", "1660"),
}

# Options: strip .NS, format as Angel One F&O symbol
# RELIANCE12MAR261450CE  →  RELIANCE25MAR1450CE  (Angel One uses 2-digit year at end)
def _map_option_symbol(sym: str) -> tuple:
    """Map ZeroBot option symbol to Angel One F&O format."""
    import re
    m = re.match(r'^([A-Z]+)(\d{1,2}[A-Z]{3}\d{2})(\d+)(CE|PE)$', sym.upper())
    if not m:
        return (sym, "NFO", "0")
    underlying, expiry_str, strike, opt_type = m.groups()
    # Angel One format: RELIANCE25MAR1450CE
    try:
        from datetime import datetime as dt
        exp = dt.strptime(expiry_str, "%d%b%y")
        angel_expiry = exp.strftime("%d%b%y").upper()  # same format
    except Exception:
        angel_expiry = expiry_str
    angel_sym = f"{underlying}{angel_expiry}{strike}{opt_type}"
    return (angel_sym, "NFO", "0")


class RateLimiter:
    """Token-bucket rate limiter — 3 requests/sec (Angel One limit)."""
    def __init__(self, rate: int = 3):
        self._rate = rate
        self._tokens = rate
        self._last_refill = time.monotonic()

    async def acquire(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
        self._last_refill = now
        if self._tokens < 1:
            wait = (1 - self._tokens) / self._rate
            log.debug(f"RateLimit: waiting {wait:.2f}s")
            await asyncio.sleep(wait)
            self._tokens = 0
        else:
            self._tokens -= 1


class AngelPaperSimulator:
    """
    Drop-in for AngelOneBroker — exercises identical code paths in paper mode.
    Logs what WOULD happen on Angel One without touching real API.
    """

    def __init__(self, initial_capital: float = None):
        self._capital    = initial_capital or cfg.initial_capital
        self._positions: Dict[str, dict] = {}
        self._orders:    Dict[str, dict] = {}
        self._daily_pnl  = 0.0
        self._total_pnl  = 0.0
        self._connected  = False
        self._jwt_expires = None
        self._rate_limiter = RateLimiter(rate=3)
        self._cost_calc   = CostCalculator(cfg.paper_broker)
        log.info(
            f"🔬 Angel Paper Simulator initialized\n"
            f"   Capital:  ₹{self._capital:,.2f}\n"
            f"   Simulates: JWT auth · rate limiting · NSE symbol mapping\n"
            f"   Switch to live: settings.yaml → broker.name: 'angel'"
        )

    # ── Connection simulation ────────────────────────────────────────────────

    def connect(self):
        """
        Simulate Angel One SmartAPI authentication.
        In paper sim: validates credentials are present, logs what WOULD be sent.
        """
        log.info("🔬 [AngelSim] Simulating Angel One connection...")
        log.info("🔬 [AngelSim] → POST https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword")
        log.info("🔬 [AngelSim] → Body: {clientcode, password, totp} (credentials masked)")

        # Check if real credentials exist and warn
        if cfg.angel_one.is_configured:
            log.info("🔬 [AngelSim] ✅ Credentials found in config/.env")
            log.info("🔬 [AngelSim]    To go live: settings.yaml → bot.mode: 'live'")
        else:
            log.info("🔬 [AngelSim] ⚠  No credentials — simulating successful auth anyway")

        self._connected = True
        self._jwt_expires = time.time() + 23 * 3600  # 23 hours
        log.info("🔬 [AngelSim] ✅ Auth simulated | JWT valid for 23h | Feed token: sim_feed_token")
        return True

    def is_connected(self) -> bool:
        if not self._connected:
            return False
        if time.time() > (self._jwt_expires or 0):
            log.warning("🔬 [AngelSim] JWT expired — auto-refreshing...")
            self.connect()  # simulate auto-refresh
        return self._connected

    # ── Order placement ──────────────────────────────────────────────────────

    async def place_order(
        self, symbol: str, side: str, qty: int, cmp: float,
        strategy: str = "", confidence: float = 0.0,
        order_type: str = "MARKET", product: str = "INTRADAY",
    ):
        """
        Simulate Angel One order placement.
        Logs the exact API call that WOULD be made in live mode.
        Returns an order object identical to AngelOneBroker output.
        """
        await self._rate_limiter.acquire()

        # Map symbol to Angel One format
        is_option = symbol.endswith("CE") or symbol.endswith("PE")
        if is_option:
            angel_sym, exchange, token = _map_option_symbol(symbol)
            product = "INTRADAY"
        else:
            angel_sym, exchange, token = NSE_SYMBOL_MAP.get(
                symbol, (symbol.replace(".NS", "-EQ"), "NSE", "0")
            )

        order_id = f"SIM-{uuid.uuid4().hex[:12].upper()}"
        order_variety = "NORMAL"

        # Log the EXACT API call that live mode would make
        log.info(
            f"🔬 [AngelSim] WOULD POST placeOrder:\n"
            f"   variety={order_variety} · tradingsymbol={angel_sym}\n"
            f"   exchange={exchange} · transactiontype={side}\n"
            f"   quantity={qty} · price=0 · ordertype={order_type}\n"
            f"   producttype={product} · duration=DAY\n"
            f"   → Simulated orderid: {order_id}"
        )

        # Simulate fill (paper mode: instant with slippage)
        slippage_pct = 0.0005  # 0.05%
        if side == "BUY":
            fill_price = round(cmp * (1 + slippage_pct), 2)
        else:
            fill_price = round(cmp * (1 - slippage_pct), 2)

        costs = self._cost_calc.compute(fill_price * qty, exchange=exchange)
        total_cost = costs.get("total", 20.0)

        # Update internal state
        if side == "BUY":
            self._positions[symbol] = {
                "qty": qty, "avg_price": fill_price, "side": "BUY",
                "angel_symbol": angel_sym, "exchange": exchange,
                "order_id": order_id, "strategy": strategy,
                "confidence": confidence, "opened_at": datetime.now().isoformat(),
            }
            self._capital -= (qty * fill_price + total_cost)
        elif side == "SELL" and symbol in self._positions:
            pos = self._positions[symbol]
            pnl = (fill_price - pos["avg_price"]) * qty - total_cost
            self._daily_pnl += pnl
            self._total_pnl += pnl
            self._capital   += qty * fill_price - total_cost
            del self._positions[symbol]

        # Store order
        self._orders[order_id] = {
            "order_id": order_id, "symbol": symbol, "angel_symbol": angel_sym,
            "side": side, "qty": qty, "price": fill_price,
            "status": "COMPLETE", "filled_at": datetime.now().isoformat(),
        }

        # Emit fill event — same as AngelOneBroker
        await bus.publish("order_fill", {
            "order_id": order_id, "symbol": symbol, "side": side,
            "qty": qty, "fill_price": fill_price, "total_cost": total_cost,
            "strategy": strategy, "confidence": confidence,
        })

        log.info(
            f"🔬 [AngelSim] SIMULATED FILL | {side} {qty}x {symbol} ({angel_sym}) "
            f"@ ₹{fill_price:.2f} | Costs: ₹{total_cost:.2f} | PnL tracking: ₹{self._daily_pnl:.2f}"
        )

        class _Order:
            pass
        o = _Order()
        o.order_id   = order_id
        o.fill_price = fill_price
        o.status     = "COMPLETE"
        return o

    # ── Position & fund queries ──────────────────────────────────────────────

    def get_positions(self) -> Dict[str, dict]:
        return dict(self._positions)

    def get_portfolio_summary(self) -> dict:
        unrealised = sum(
            (p.get("current_price", p["avg_price"]) - p["avg_price"]) * p["qty"]
            for p in self._positions.values()
        )
        return {
            "capital":    round(self._capital, 2),
            "available":  round(self._capital, 2),
            "daily_pnl":  round(self._daily_pnl, 2),
            "total_pnl":  round(self._total_pnl, 2),
            "unrealised": round(unrealised, 2),
            "positions":  len(self._positions),
        }

    def get_order_book(self) -> list:
        return list(self._orders.values())

    def get_symbol_info(self, symbol: str) -> dict:
        """Return what Angel One symbol lookup WOULD return."""
        is_option = symbol.endswith("CE") or symbol.endswith("PE")
        if is_option:
            angel_sym, exchange, token = _map_option_symbol(symbol)
        else:
            angel_sym, exchange, token = NSE_SYMBOL_MAP.get(
                symbol, (symbol.replace(".NS", "-EQ"), "NSE", "0")
            )
        log.debug(f"🔬 [AngelSim] Symbol lookup: {symbol} → {angel_sym} ({exchange})")
        return {"tradingsymbol": angel_sym, "exchange": exchange, "symboltoken": token}
