# -*- coding: utf-8 -*-
"""
ZeroBot G1 — Portfolio Optimizer (Correlation-Aware Sizing)
============================================================
Prevents "3 banking stocks down together" scenario.
Reduces position size for highly correlated bets.

MULTIPLIERS:
  corr > 0.85 → 0.30x (very high correlation, reduce aggressively)
  corr > 0.70 → 0.55x
  corr > 0.60 → 0.70x
  corr < 0.30 → 1.15x (diversifying — small bonus)
"""
import math, logging
from typing import Dict, List, Optional, Tuple
from collections import Counter

log = logging.getLogger(__name__)

_SECTOR_MAP: Dict[str, str] = {
    "RELIANCE.NS":"Energy",    "ONGC.NS":"Energy",
    "HDFCBANK.NS":"Banking",   "ICICIBANK.NS":"Banking",
    "AXISBANK.NS":"Banking",   "KOTAKBANK.NS":"Banking",
    "SBIN.NS":"Banking",       "INDUSINDBK.NS":"Banking",
    "TCS.NS":"IT",             "INFY.NS":"IT",
    "WIPRO.NS":"IT",           "HCLTECH.NS":"IT",
    "TECHM.NS":"IT",           "BAJFINANCE.NS":"Finance",
    "BAJAJFINSV.NS":"Finance", "MARUTI.NS":"Auto",
    "LT.NS":"Infrastructure",  "HINDUNILVR.NS":"FMCG",
    "ITC.NS":"FMCG",           "NESTLEIND.NS":"FMCG",
    "ASIANPAINT.NS":"Consumer","TITAN.NS":"Consumer",
    "ULTRACEMCO.NS":"Cement",  "TATASTEEL.NS":"Steel",
    "NTPC.NS":"Utilities",     "POWERGRID.NS":"Utilities",
}
_SAME_SECTOR_CORR = 0.80
_CROSS_SECTOR_CORR = 0.25


class PortfolioOptimizer:

    def __init__(self):
        self._cache: Dict[str, float] = {}

    def _sector(self, sym: str) -> str:
        return _SECTOR_MAP.get(sym, "Other")

    def _price_corr(self, a: str, b: str, candle_data: dict) -> Optional[float]:
        key = f"{a}:{b}"
        if key in self._cache:
            return self._cache[key]
        try:
            import pandas as pd
            da = candle_data.get(a)
            db = candle_data.get(b)
            if da is None or db is None or len(da)<20 or len(db)<20:
                return None
            ra = da["close"].pct_change().dropna().tail(60).values
            rb = db["close"].pct_change().dropna().tail(60).values
            n  = min(len(ra), len(rb))
            if n < 10: return None
            ra, rb = ra[-n:], rb[-n:]
            ma, mb = ra.mean(), rb.mean()
            num = ((ra-ma)*(rb-mb)).sum()
            den = math.sqrt(((ra-ma)**2).sum() * ((rb-mb)**2).sum())
            corr = float(num/den) if den>1e-9 else 0.0
            corr = max(-1.0, min(1.0, corr))
            self._cache[key] = corr
            return corr
        except Exception as e:
            log.debug(f"[OPT] corr error {a}/{b}: {e}")
            return None

    def correlation_multiplier(
        self,
        candidate: str,
        open_positions: Dict[str, dict],
        candle_data: Optional[dict] = None,
    ) -> float:
        if not open_positions:
            return 1.0
        corrs = []
        for sym in open_positions:
            if sym == candidate:
                continue
            corr = None
            if candle_data:
                corr = self._price_corr(candidate, sym, candle_data)
            if corr is None:
                corr = (_SAME_SECTOR_CORR if self._sector(candidate)==self._sector(sym)
                        else _CROSS_SECTOR_CORR)
            corrs.append(corr)
        if not corrs:
            return 1.0
        avg = sum(corrs)/len(corrs)
        mx  = max(corrs)
        if mx >= 0.85:    mult = 0.30
        elif mx >= 0.70:  mult = 0.55
        elif avg >= 0.60: mult = 0.70
        elif avg >= 0.40: mult = 0.85
        elif avg <= 0.30: mult = 1.15
        else:             mult = 1.00
        if mult != 1.0:
            log.info(f"[OPT] {candidate}: corr_mult={mult:.2f} (avg={avg:.2f} max={mx:.2f})")
        return mult

    def sector_concentration(self, open_positions: Dict[str, dict]) -> Dict[str, int]:
        return dict(Counter(_SECTOR_MAP.get(s,"Other") for s in open_positions))

    def invalidate_cache(self):
        self._cache.clear()


# ── Singleton ─────────────────────────────────────────────────────────────────
portfolio_optimizer = PortfolioOptimizer()
