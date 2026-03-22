"""
ZeroBot — Momentum Strategy
Signal: EMA crossover + Volume spike + Price above VWAP
Buy when: EMA9 crosses above EMA21, volume > 1.5x average, price > VWAP
Sell when: EMA9 crosses below EMA21 OR stop loss hit
"""
from typing import Optional
import pandas as pd
from strategies.base_strategy import BaseStrategy
from risk.risk_engine import TradeSignal


class MomentumStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("Momentum")
        self.vol_multiplier = 1.5
        self.ema_fast = "EMA_9"
        self.ema_slow = "EMA_21"

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> Optional[TradeSignal]:
        if len(df) < 22:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # Required columns
        needed = [self.ema_fast, self.ema_slow, "vol_spike", "ATRr_14"]
        if not all(c in df.columns for c in needed):
            return None

        ema_fast_now = last[self.ema_fast]
        ema_slow_now = last[self.ema_slow]
        ema_fast_prev = prev[self.ema_fast]
        ema_slow_prev = prev[self.ema_slow]

        vol_spike = last["vol_spike"]
        cmp = last["close"]
        atr = last["ATRr_14"]

        # VWAP check
        above_vwap = True
        if "vwap_dev" in df.columns:
            above_vwap = last["vwap_dev"] > 0

        # Bullish crossover
        bullish_cross = (ema_fast_now > ema_slow_now) and (ema_fast_prev <= ema_slow_prev)
        # Volume confirmation
        strong_volume = vol_spike >= self.vol_multiplier

        if bullish_cross and strong_volume and above_vwap:
            confidence = self._calc_confidence(last, df)
            return TradeSignal(
                symbol=symbol,
                side="BUY",
                strategy=self.name,
                confidence=confidence,
                trigger=f"EMA cross + vol spike {vol_spike:.1f}x + above VWAP",
                atr=float(atr),
                cmp=float(cmp),
            )

        # Bearish crossover → close long / short
        bearish_cross = (ema_fast_now < ema_slow_now) and (ema_fast_prev >= ema_slow_prev)
        if bearish_cross and strong_volume:
            confidence = 70.0
            return TradeSignal(
                symbol=symbol,
                side="SELL",
                strategy=self.name,
                confidence=confidence,
                trigger=f"Bearish EMA cross + vol spike {vol_spike:.1f}x",
                atr=float(atr),
                cmp=float(cmp),
            )

        return None

    def _calc_confidence(self, last: pd.Series, df: pd.DataFrame) -> float:
        """Score confidence 0-100 based on signal strength."""
        score = 60.0  # base

        # Volume bonus
        if last.get("vol_spike", 0) > 2.0:
            score += 10
        elif last.get("vol_spike", 0) > 1.5:
            score += 5

        # RSI confirmation
        rsi = last.get("RSI_14", 50)
        if 40 < rsi < 65:
            score += 8
        elif rsi > 70:
            score -= 10  # Overbought

        # MACD confirmation
        macd = last.get("MACD_12_26_9", 0)
        macds = last.get("MACDs_12_26_9", 0)
        if macd > macds and macd > 0:
            score += 7

        return min(95, max(50, score))
