"""
ZeroBot — Breakout Strategy (Support/Resistance Breakout)
Captures explosive moves when price breaks key levels.

Logic:
  - Resistance = highest close in last N bars (default 20)
  - Support    = lowest close in last N bars (default 20)
  - BUY when: Close > resistance + ATR buffer + volume surge
  - SELL when: Close < support - ATR buffer + volume surge

Extra filters:
  - ADX > 20 (trending market, not ranging)
  - Volume > 2x average (institutional participation)
  - BB squeeze: width < 2% of price (consolidation before breakout)
  - MACD momentum aligned

Used by: Breakout traders, hedge funds, CTA systems.
Win-rate: 45-52% but with 3:1 R:R (big wins, small stops)
"""
from typing import Optional
import pandas as pd
from strategies.base_strategy import BaseStrategy
from risk.risk_engine import TradeSignal
import logging

log = logging.getLogger(__name__)


class BreakoutStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("Breakout")
        self.lookback = 20      # bars for S/R calculation
        self.atr_buffer = 0.3   # ATR multiplier buffer above resistance
        self._last_fired: dict = {}  # symbol → last breakout level (avoid re-entry)

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> Optional[TradeSignal]:
        if len(df) < self.lookback + 5:
            return None

        needed = ["ATRr_14", "vol_spike"]
        if not all(c in df.columns for c in needed):
            return None

        last   = df.iloc[-1]
        prev   = df.iloc[-2]
        cmp    = float(last["close"])
        atr    = float(last["ATRr_14"])
        vol_spike = float(last.get("vol_spike", 1.0))

        # S/R levels from previous N bars (exclude current candle)
        history = df.iloc[-(self.lookback + 1):-1]
        if "high" in df.columns:
            resistance = float(history["high"].max())
            support    = float(history["low"].min())
        else:
            resistance = float(history["close"].max())
            support    = float(history["close"].min())

        # Quality filters
        adx        = float(last.get("ADX_14", 20))
        trending   = adx >= 20
        vol_strong = vol_spike >= 2.0  # Strong volume needed for breakout

        # BB squeeze check — best breakouts come from consolidation
        bbu = last.get("BBU_20_2.0")
        bbl = last.get("BBL_20_2.0")
        bb_squeeze = False
        if bbu is not None and bbl is not None and cmp > 0:
            bb_width = (float(bbu) - float(bbl)) / cmp
            bb_squeeze = bb_width < 0.03  # BB bandwidth < 3% = squeeze

        # Avoid re-entering at same level
        last_level = self._last_fired.get(symbol, 0)

        # ─── BULLISH BREAKOUT ───
        breakout_level = resistance + (atr * self.atr_buffer)
        if (cmp > breakout_level
                and float(prev["close"]) <= resistance  # fresh break
                and vol_strong
                and trending
                and abs(cmp - last_level) > atr * 2):  # not the same level
            confidence = self._calc_confidence(last, df, "BUY", bb_squeeze)
            self._last_fired[symbol] = cmp
            return TradeSignal(
                symbol=symbol,
                side="BUY",
                strategy=self.name,
                confidence=confidence,
                trigger=f"Breakout ↑₹{resistance:.1f} | vol {vol_spike:.1f}x | ADX {adx:.0f}" +
                        (" | BB squeeze" if bb_squeeze else ""),
                atr=atr,
                cmp=cmp,
            )

        # ─── BEARISH BREAKDOWN ───
        breakdown_level = support - (atr * self.atr_buffer)
        if (cmp < breakdown_level
                and float(prev["close"]) >= support  # fresh break
                and vol_strong
                and trending
                and abs(cmp - last_level) > atr * 2):
            confidence = self._calc_confidence(last, df, "SELL", bb_squeeze)
            self._last_fired[symbol] = cmp
            return TradeSignal(
                symbol=symbol,
                side="SELL",
                strategy=self.name,
                confidence=confidence,
                trigger=f"Breakdown ↓₹{support:.1f} | vol {vol_spike:.1f}x | ADX {adx:.0f}" +
                        (" | BB squeeze" if bb_squeeze else ""),
                atr=atr,
                cmp=cmp,
            )

        return None

    def _calc_confidence(self, last, df, side, bb_squeeze) -> float:
        score = 58.0

        # Volume multiplier
        vs = float(last.get("vol_spike", 1.0))
        if vs >= 4.0:   score += 18
        elif vs >= 3.0: score += 13
        elif vs >= 2.5: score += 9
        elif vs >= 2.0: score += 5

        # BB squeeze bonus (consolidation before breakout = higher quality)
        if bb_squeeze:
            score += 12

        # ADX
        adx = float(last.get("ADX_14", 20))
        if adx >= 35:   score += 10
        elif adx >= 28: score += 7
        elif adx >= 22: score += 3

        # MACD alignment
        macd  = float(last.get("MACD_12_26_9",  0))
        macds = float(last.get("MACDs_12_26_9", 0))
        if side == "BUY"  and macd > macds and macd > 0:  score += 8
        if side == "SELL" and macd < macds and macd < 0:  score += 8

        # RSI not exhausted
        rsi = float(last.get("RSI_14", 50))
        if side == "BUY"  and 45 < rsi < 72:  score += 5
        if side == "SELL" and 28 < rsi < 55:  score += 5

        return min(93, max(52, score))
