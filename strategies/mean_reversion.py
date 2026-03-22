"""
ZeroBot — Mean Reversion Strategy
Buy when: RSI oversold (<30) + price near lower Bollinger Band
Sell when: RSI overbought (>70) + price near upper Bollinger Band
"""
from typing import Optional
import pandas as pd
from strategies.base_strategy import BaseStrategy
from risk.risk_engine import TradeSignal


class MeanReversionStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("MeanReversion")
        self.rsi_oversold = 30
        self.rsi_overbought = 70
        self.bb_threshold = 0.05  # 5% from band

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> Optional[TradeSignal]:
        if len(df) < 21:
            return None

        last = df.iloc[-1]
        rsi = last.get("RSI_14")
        cmp = last.get("close")
        atr = last.get("ATRr_14")
        bbl = last.get("BBL_20_2.0")
        bbu = last.get("BBU_20_2.0")

        if any(v is None for v in [rsi, cmp, atr, bbl, bbu]):
            return None

        near_lower = cmp <= bbl * (1 + self.bb_threshold)
        near_upper = cmp >= bbu * (1 - self.bb_threshold)

        # BUY: oversold + near lower band
        if rsi < self.rsi_oversold and near_lower:
            conf = 60 + (self.rsi_oversold - rsi) * 1.0
            return TradeSignal(
                symbol=symbol, side="BUY", strategy=self.name,
                confidence=min(90, conf),
                trigger=f"RSI={rsi:.1f} oversold + lower BB touch",
                atr=float(atr), cmp=float(cmp),
            )

        # SELL: overbought + near upper band
        if rsi > self.rsi_overbought and near_upper:
            conf = 60 + (rsi - self.rsi_overbought) * 1.0
            return TradeSignal(
                symbol=symbol, side="SELL", strategy=self.name,
                confidence=min(90, conf),
                trigger=f"RSI={rsi:.1f} overbought + upper BB touch",
                atr=float(atr), cmp=float(cmp),
            )

        return None
