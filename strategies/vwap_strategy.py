"""
ZeroBot — VWAP Reversion Strategy
Trades pullbacks to VWAP with volume confirmation.
"""
from typing import Optional
import pandas as pd
from strategies.base_strategy import BaseStrategy
from risk.risk_engine import TradeSignal


class VWAPStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("VWAP")
        self.dev_threshold = 0.8   # % deviation from VWAP to trigger (raised from 0.3 — prevents excessive signals on tight oscillations)

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> Optional[TradeSignal]:
        if len(df) < 5 or "vwap_dev" not in df.columns:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]

        vwap_dev = last.get("vwap_dev", 0)
        prev_dev = prev.get("vwap_dev", 0)
        cmp = last["close"]
        atr = last.get("ATRr_14", cmp * 0.01)
        vol_spike = last.get("vol_spike", 1.0)

        # Price was below VWAP and is now crossing back above (pullback buy)
        if prev_dev < -self.dev_threshold and vwap_dev > prev_dev and vol_spike > 1.2:
            return TradeSignal(
                symbol=symbol, side="BUY", strategy=self.name,
                confidence=68.0,
                trigger=f"VWAP pullback buy | dev={vwap_dev:.2f}%",
                atr=float(atr), cmp=float(cmp),
            )

        # Price was above VWAP and is now falling back (pullback sell)
        if prev_dev > self.dev_threshold and vwap_dev < prev_dev and vol_spike > 1.2:
            return TradeSignal(
                symbol=symbol, side="SELL", strategy=self.name,
                confidence=65.0,
                trigger=f"VWAP pullback sell | dev={vwap_dev:.2f}%",
                atr=float(atr), cmp=float(cmp),
            )

        return None
