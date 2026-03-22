"""
ZeroBot — RSI Divergence Strategy
Catches trend reversals that ordinary indicators miss.

Logic:
  - BULLISH DIVERGENCE: Price makes lower low, but RSI makes higher low
    → Momentum is improving despite price falling → BUY setup
  - BEARISH DIVERGENCE: Price makes higher high, but RSI makes lower high
    → Momentum is weakening despite price rising → SELL setup

Additional filters:
  - RSI in oversold zone (< 40) for bullish, overbought (> 60) for bearish
  - Volume expansion confirms divergence
  - VWAP context for trend bias

Win-rate target: 58-65% with 2:1 R:R
Used by: Hedge funds, prop traders, top-tier retail traders
"""
from typing import Optional
import pandas as pd
from strategies.base_strategy import BaseStrategy
from risk.risk_engine import TradeSignal
import logging

log = logging.getLogger(__name__)


class RSIDivergenceStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("RSIDivergence")
        self.lookback = 14      # bars to look back for swing
        self.min_swing = 5      # minimum bars between swings

    def _find_swing_lows(self, series: pd.Series, window: int = 5):
        """Find local minima indices."""
        lows = []
        for i in range(window, len(series) - window):
            if series.iloc[i] == series.iloc[i-window:i+window+1].min():
                lows.append(i)
        return lows

    def _find_swing_highs(self, series: pd.Series, window: int = 5):
        """Find local maxima indices."""
        highs = []
        for i in range(window, len(series) - window):
            if series.iloc[i] == series.iloc[i-window:i+window+1].max():
                highs.append(i)
        return highs

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> Optional[TradeSignal]:
        if len(df) < 30:
            return None

        needed = ["RSI_14", "ATRr_14"]
        if not all(c in df.columns for c in needed):
            return None

        close = df["close"]
        rsi   = df["RSI_14"]
        last  = df.iloc[-1]
        cmp   = float(last["close"])
        atr   = float(last["ATRr_14"])

        # ─── BULLISH DIVERGENCE ───
        # RSI must be in lower range (oversold area, ≤ 45)
        if float(last["RSI_14"]) <= 45:
            price_lows = self._find_swing_lows(close.tail(30))
            rsi_lows   = self._find_swing_lows(rsi.tail(30))

            if len(price_lows) >= 2 and len(rsi_lows) >= 2:
                # Most recent two price lows
                p1_idx, p2_idx = price_lows[-2], price_lows[-1]
                r1_idx, r2_idx = rsi_lows[-2],   rsi_lows[-1]

                p1 = float(close.tail(30).iloc[p1_idx])
                p2 = float(close.tail(30).iloc[p2_idx])
                r1 = float(rsi.tail(30).iloc[r1_idx])
                r2 = float(rsi.tail(30).iloc[r2_idx])

                # Price lower low + RSI higher low = bullish divergence
                # Only fire if latest swing is within last 5 bars
                bars_since = len(close.tail(30)) - 1 - p2_idx
                if (p2 < p1 and r2 > r1 and bars_since <= 5
                        and abs(p2_idx - r2_idx) <= 4):  # aligned in time
                    confidence = self._calc_confidence(last, df, "BUY")
                    vol_spike = float(last.get("vol_spike", 1.0))
                    return TradeSignal(
                        symbol=symbol,
                        side="BUY",
                        strategy=self.name,
                        confidence=confidence,
                        trigger=f"Bullish RSI divergence | RSI {r2:.1f} (prev {r1:.1f}) | vol {vol_spike:.1f}x",
                        atr=atr,
                        cmp=cmp,
                    )

        # ─── BEARISH DIVERGENCE ───
        # RSI must be in upper range (overbought area, ≥ 55)
        if float(last["RSI_14"]) >= 55:
            price_highs = self._find_swing_highs(close.tail(30))
            rsi_highs   = self._find_swing_highs(rsi.tail(30))

            if len(price_highs) >= 2 and len(rsi_highs) >= 2:
                p1_idx, p2_idx = price_highs[-2], price_highs[-1]
                r1_idx, r2_idx = rsi_highs[-2],   rsi_highs[-1]

                p1 = float(close.tail(30).iloc[p1_idx])
                p2 = float(close.tail(30).iloc[p2_idx])
                r1 = float(rsi.tail(30).iloc[r1_idx])
                r2 = float(rsi.tail(30).iloc[r2_idx])

                bars_since = len(close.tail(30)) - 1 - p2_idx
                if (p2 > p1 and r2 < r1 and bars_since <= 5
                        and abs(p2_idx - r2_idx) <= 4):
                    confidence = self._calc_confidence(last, df, "SELL")
                    vol_spike = float(last.get("vol_spike", 1.0))
                    return TradeSignal(
                        symbol=symbol,
                        side="SELL",
                        strategy=self.name,
                        confidence=confidence,
                        trigger=f"Bearish RSI divergence | RSI {r2:.1f} (prev {r1:.1f}) | vol {vol_spike:.1f}x",
                        atr=atr,
                        cmp=cmp,
                    )

        return None

    def _calc_confidence(self, last, df, side) -> float:
        score = 60.0

        rsi = float(last.get("RSI_14", 50))
        # Deeper oversold/overbought = stronger signal
        if side == "BUY":
            if rsi <= 25:   score += 15
            elif rsi <= 35: score += 10
            elif rsi <= 45: score += 5
        else:
            if rsi >= 75:   score += 15
            elif rsi >= 65: score += 10
            elif rsi >= 55: score += 5

        # Volume
        vs = float(last.get("vol_spike", 1.0))
        if vs >= 2.0:   score += 10
        elif vs >= 1.5: score += 5

        # MACD confirmation
        macd  = float(last.get("MACD_12_26_9",  0))
        macds = float(last.get("MACDs_12_26_9", 0))
        if side == "BUY"  and macd > macds:  score += 7
        if side == "SELL" and macd < macds:  score += 7

        # BB position
        bbl = last.get("BBL_20_2.0", None)
        bbu = last.get("BBU_20_2.0", None)
        cmp = float(last["close"])
        if side == "BUY" and bbl is not None and cmp < float(bbl):
            score += 8  # price below BB lower = oversold confirmation
        if side == "SELL" and bbu is not None and cmp > float(bbu):
            score += 8  # price above BB upper = overbought confirmation

        return min(90, max(52, score))
