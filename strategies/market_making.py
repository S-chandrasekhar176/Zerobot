# -*- coding: utf-8 -*-
"""
ZeroBot v2 — Market Making Strategy
BlackRock/Citadel-style market making:
- Dynamic bid/ask spread based on volatility (ATR)
- Inventory skew (reduce exposure as position grows)
- Quote cancellation on adverse price moves
- Works best in paper mode or with low-latency live connections

Reference: Avellaneda-Stoikov optimal market making model
"""
from typing import Optional, Tuple
import pandas as pd
from strategies.base_strategy import BaseStrategy
from risk.risk_engine import TradeSignal
from core.logger import log


class MarketMakingStrategy(BaseStrategy):
    """
    Avellaneda-Stoikov market making with inventory management.

    Posts limit quotes around mid-price.
    Skews quotes based on inventory to avoid directional risk.
    """

    def __init__(self):
        super().__init__("MarketMaking")
        self.target_spread_bps = 10      # 10 basis points target spread
        self.max_inventory_pct = 0
        self.max_active_symbols = 1      # [FIX] One symbol at a time — MM needs clean inventory management
        self._active_symbols: set = set()
        self._last_signal_ts: dict = {}
        self._min_signal_gap_s = 600     # [FIX] 10 min between signals — MM needs time for spread to reset
        self.max_inventory_pct = 0.15    # Max 15% of capital in one side
        self.gamma = 0.1                 # Risk aversion parameter
        self.vol_multiplier = 1.5        # Widen spreads when volatile
        self._inventory: dict = {}       # symbol → net qty

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> Optional[TradeSignal]:
        """Generate market making quotes — rate-limited, max 3 concurrent symbols."""
        if len(df) < 20:
            return None

        # [FIX] Rate limit: min 5 min between signals on same symbol
        import time as _t
        now = _t.monotonic()
        if now - self._last_signal_ts.get(symbol, 0) < self._min_signal_gap_s:
            return None

        # [FIX] Max concurrent MM positions — prevent bulk-opening all 30 symbols
        if len(self._active_symbols) >= self.max_active_symbols and symbol not in self._active_symbols:
            return None

        last = df.iloc[-1]
        mid_price = float(last["close"])
        atr = float(last.get("ATRr_14", mid_price * 0.01))
        vol_spike = float(last.get("vol_spike", 1.0))

        # P16-FIX: Block MarketMaking when VIX is elevated (≥ 20) or vol_spike is
        # severe (≥ 2.5). MM strategies are designed for calm, liquid markets —
        # in high-volatility regimes (panic selling, news events) the spread
        # widens uncontrollably and inventory risk becomes extreme.
        from core.state_manager import state_mgr
        live_vix = state_mgr.state.market_data.get("india_vix", 18.0)
        if live_vix >= 20.0:
            return None
        if vol_spike >= 2.5:
            return None

        # Inventory-adjusted reservation price
        inventory = self._inventory.get(symbol, 0)
        sigma = atr / mid_price  # normalized volatility

        # Avellaneda-Stoikov: reservation price skew
        reservation_price = mid_price - inventory * self.gamma * sigma ** 2

        # Dynamic spread = f(volatility)
        base_spread = mid_price * (self.target_spread_bps / 10000)
        adjusted_spread = base_spread * (1 + (vol_spike - 1) * self.vol_multiplier)

        bid_price = reservation_price - adjusted_spread / 2
        ask_price = reservation_price + adjusted_spread / 2

        # Only quote if spread is profitable after costs (~5bps all-in)
        min_spread = mid_price * 0.0005
        if adjusted_spread < min_spread:
            return None

        # Decide which side to quote based on inventory
        import time as _t2
        if inventory <= 0:
            # Need to buy (or flat): post bid
            confidence = 72.0 - abs(inventory) * 0.5
            self._last_signal_ts[symbol] = _t2.monotonic()
            self._active_symbols.add(symbol)
            return TradeSignal(
                symbol=symbol,
                side="BUY",
                strategy=self.name,
                confidence=min(90, max(60, confidence)),
                trigger=f"MM Bid @ {bid_price:.2f} | spread={adjusted_spread:.2f} | inv={inventory}",
                atr=atr,
                cmp=bid_price,
            )
        else:
            # Need to sell: post ask
            confidence = 72.0 - inventory * 0.5
            self._last_signal_ts[symbol] = _t2.monotonic()
            self._active_symbols.add(symbol)
            return TradeSignal(
                symbol=symbol,
                side="SELL",
                strategy=self.name,
                confidence=min(90, max(60, confidence)),
                trigger=f"MM Ask @ {ask_price:.2f} | spread={adjusted_spread:.2f} | inv={inventory}",
                atr=atr,
                cmp=ask_price,
            )

    def update_inventory(self, symbol: str, qty_delta: int):
        """Update inventory after fill."""
        self._inventory[symbol] = self._inventory.get(symbol, 0) + qty_delta

    def get_quotes(self, symbol: str, mid_price: float, atr: float) -> Tuple[float, float]:
        """
        Return current bid/ask quotes.
        Returns (bid_price, ask_price)
        """
        inventory = self._inventory.get(symbol, 0)
        sigma = atr / mid_price if mid_price > 0 else 0.01
        reservation_price = mid_price - inventory * self.gamma * sigma ** 2
        spread = mid_price * (self.target_spread_bps / 10000)
        return (
            round(reservation_price - spread / 2, 2),
            round(reservation_price + spread / 2, 2)
        )