"""
ZeroBot — Opening Range Breakout (ORB) Strategy
Used by top institutional traders and prop firms worldwide.

Logic:
  - Opening range = High/Low of first 15-minute candle (09:15–09:30)
  - BUY when price breaks above ORB high with strong volume + ADX trend
  - SELL when price breaks below ORB low with strong volume + ADX trend
  - Only fires between 09:30 and 14:00 (breakout window)

Edge: ORB captures institutional momentum at open. When price breaks
the first 15-min range, it often runs 1.5–2x the range's width.

Win-rate target: 55–65% with 2:1 R:R
"""
from typing import Optional
import pandas as pd
from datetime import datetime, time
from strategies.base_strategy import BaseStrategy
from risk.risk_engine import TradeSignal
import logging

log = logging.getLogger(__name__)


class ORBStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("ORB")
        self._orb_high: dict = {}   # symbol → ORB high
        self._orb_low: dict = {}    # symbol → ORB low
        self._orb_set: dict = {}    # symbol → date when ORB was set
        self._breakout_fired: dict = {}  # symbol → direction, prevent re-entry

    def _update_orb(self, df: pd.DataFrame, symbol: str):
        """Set the Opening Range from the first 15-min candle of the session."""
        today = datetime.now().date()
        if self._orb_set.get(symbol) == today:
            return  # Already set for today

        now_time = datetime.now().time()
        # ORB is set after 09:30 and uses first N candles (15-min window)
        if now_time < time(9, 30):
            return

        # Look for candles from today's open
        # Use the first 3 1-min candles or single 15-min candle
        if len(df) < 5:
            return

        # ORB = first 15 minutes. With 1-min data, take first 15 rows.
        # With 5-min data, take first 3 rows.
        orb_rows = min(15, max(3, len(df) // 10))  # adaptive
        orb_slice = df.head(orb_rows)
        orb_high = float(orb_slice["high"].max()) if "high" in df.columns else float(orb_slice["close"].max())
        orb_low  = float(orb_slice["low"].min())  if "low"  in df.columns else float(orb_slice["close"].min())

        if orb_high > orb_low:
            self._orb_high[symbol] = orb_high
            self._orb_low[symbol]  = orb_low
            self._orb_set[symbol]  = today
            self._breakout_fired[symbol] = None  # reset for today
            log.debug(f"ORB set for {symbol}: H={orb_high:.2f} L={orb_low:.2f}")

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> Optional[TradeSignal]:
        if len(df) < 20:
            return None

        # Only fire during breakout window: 09:30–14:00
        now_time = datetime.now().time()
        if not (time(9, 30) <= now_time <= time(14, 0)):
            return None

        # Update / confirm ORB
        self._update_orb(df, symbol)
        orb_h = self._orb_high.get(symbol)
        orb_l  = self._orb_low.get(symbol)
        if not orb_h or not orb_l:
            return None

        last = df.iloc[-1]
        prev = df.iloc[-2]
        cmp  = float(last["close"])
        atr  = float(last.get("ATRr_14", cmp * 0.005))

        # Volume confirmation
        vol_spike = float(last.get("vol_spike", 1.0))
        strong_vol = vol_spike >= 1.3

        # ADX trend filter — avoid choppy breakouts
        adx = float(last.get("ADX_14", 20))
        trending = adx >= 18

        # RSI not extreme
        rsi = float(last.get("RSI_14", 50))

        orb_range = orb_h - orb_l

        already_fired = self._breakout_fired.get(symbol)

        # ─── BULLISH BREAKOUT ───
        if (cmp > orb_h
                and float(prev["close"]) <= orb_h  # fresh breakout
                and strong_vol
                and trending
                and rsi < 75
                and already_fired != "BUY"):
            confidence = self._calc_confidence(last, df, orb_range, "BUY")
            self._breakout_fired[symbol] = "BUY"
            return TradeSignal(
                symbol=symbol,
                side="BUY",
                strategy=self.name,
                confidence=confidence,
                trigger=f"ORB breakout ↑ {orb_h:.1f} | vol {vol_spike:.1f}x | ADX {adx:.0f}",
                atr=atr,
                cmp=cmp,
            )

        # ─── BEARISH BREAKDOWN ───
        if (cmp < orb_l
                and float(prev["close"]) >= orb_l  # fresh breakdown
                and strong_vol
                and trending
                and rsi > 25
                and already_fired != "SELL"):
            confidence = self._calc_confidence(last, df, orb_range, "SELL")
            self._breakout_fired[symbol] = "SELL"
            return TradeSignal(
                symbol=symbol,
                side="SELL",
                strategy=self.name,
                confidence=confidence,
                trigger=f"ORB breakdown ↓ {orb_l:.1f} | vol {vol_spike:.1f}x | ADX {adx:.0f}",
                atr=atr,
                cmp=cmp,
            )

        return None

    def _calc_confidence(self, last, df, orb_range, side) -> float:
        score = 62.0  # base — ORB is high-quality setup

        # Volume
        vs = float(last.get("vol_spike", 1.0))
        if vs >= 2.5:   score += 15
        elif vs >= 2.0: score += 10
        elif vs >= 1.5: score += 5

        # ADX strength
        adx = float(last.get("ADX_14", 20))
        if adx >= 30:   score += 10
        elif adx >= 25: score += 6
        elif adx >= 20: score += 3

        # RSI alignment
        rsi = float(last.get("RSI_14", 50))
        if side == "BUY" and 50 < rsi < 70:   score += 8
        if side == "SELL" and 30 < rsi < 50:  score += 8

        # MACD alignment
        macd  = float(last.get("MACD_12_26_9",  0))
        macds = float(last.get("MACDs_12_26_9", 0))
        if side == "BUY"  and macd > macds:  score += 5
        if side == "SELL" and macd < macds:  score += 5

        # Tight range = better breakout quality
        cmp = float(last["close"])
        if cmp > 0 and orb_range / cmp < 0.008:
            score += 5  # tight ORB → bigger projected move

        return min(92, max(52, score))
