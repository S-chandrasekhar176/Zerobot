# -*- coding: utf-8 -*-
"""
ZeroBot v2 — Statistical Arbitrage Strategy
Pairs trading / cointegration-based arbitrage.

Approach:
  1. Find cointegrated pairs (run once daily)
  2. Monitor Z-score of spread
  3. Enter when |Z| > 2.0, exit when |Z| < 0.5
  4. Fully market-neutral (long one, short the other)

Used by: Renaissance Technologies, Two Sigma, AQR
"""
from typing import Optional, List, Tuple, Dict
import pandas as pd
import numpy as np
from strategies.base_strategy import BaseStrategy
from risk.risk_engine import TradeSignal
from core.logger import log


class StatArbStrategy(BaseStrategy):
    """
    Pairs trading via cointegration.
    Requires at least 2 symbols with price data.
    """

    def __init__(self):
        super().__init__("StatArb")
        self.zscore_entry = 2.0       # Enter when Z-score exceeds this
        self.zscore_exit = 0.5        # Exit when Z-score reverts to this
        self.lookback = 60            # Days for cointegration test
        self.pairs: List[Tuple[str, str]] = []
        self._spreads: Dict[str, pd.Series] = {}
        self._hedge_ratios: Dict[str, float] = {}
        self._active_pairs: Dict[str, str] = {}  # pair_key → "LONG_A_SHORT_B" | "LONG_B_SHORT_A"
        self._calibrated: bool = False  # Set True after find_pairs() runs (P16: fix perpetual Calibrating)

    def find_pairs(self, data: Dict[str, pd.DataFrame]) -> List[Tuple[str, str]]:
        """
        Find cointegrated pairs from price data dict.
        Returns list of (sym_a, sym_b) tuples — equity only, no indices.
        """
        from itertools import combinations

        # Never include indices in pairs — they are not tradeable
        NON_TRADEABLE = {"^NSEI","^NSEBANK","^CNXIT","^VIX","^SENSEX","^BSESN","^NIFTYIT"}
        symbols = [
            s for s, df in data.items()
            if len(df) >= self.lookback and s not in NON_TRADEABLE
        ]
        cointegrated = []

        # Sector groupings — only test pairs within same or adjacent sectors
        # This eliminates economically nonsensical pairs (e.g. RELIANCE/NESTLEIND)
        _SECTOR_GROUPS = {
            "BANK":    {"HDFCBANK.NS","ICICIBANK.NS","SBIN.NS","AXISBANK.NS","KOTAKBANK.NS",
                        "INDUSINDBK.NS","BANDHANBNK.NS"},
            "NBFC":    {"BAJFINANCE.NS","BAJAJFINSV.NS"},
            "IT":      {"TCS.NS","INFY.NS","WIPRO.NS","HCLTECH.NS","TECHM.NS"},
            "ENERGY":  {"RELIANCE.NS","ONGC.NS","NTPC.NS","POWERGRID.NS"},
            "FMCG":    {"ITC.NS","HINDUNILVR.NS","NESTLEIND.NS"},
            "AUTO":    {"MARUTI.NS"},
            "CEMENT":  {"ULTRACEMCO.NS"},
            "METAL":   {"TATASTEEL.NS"},
            "INFRA":   {"LT.NS"},
            "CONS":    {"ASIANPAINT.NS","TITAN.NS"},
        }
        def _same_group(a, b):
            for syms in _SECTOR_GROUPS.values():
                if a in syms and b in syms:
                    return True
            return False

        for sym_a, sym_b in combinations(symbols, 2):  # equity pairs only — indices already filtered
            try:
                # Gate 1: Must be same economic sector — avoids spurious statistical pairs
                if not _same_group(sym_a, sym_b):
                    continue

                # Gate 2: Minimum Pearson correlation pre-filter (fast, O(n) vs O(n^2) coint)
                # Skip expensive cointegration test if prices barely correlate
                aligned = data[sym_a]["close"].align(data[sym_b]["close"], join="inner")[0]
                if len(aligned) < self.lookback:
                    continue
                close_a = data[sym_a]["close"].reindex(aligned.index)
                close_b = data[sym_b]["close"].reindex(aligned.index)
                corr = float(close_a.corr(close_b))
                if abs(corr) < 0.65:
                    log.debug(f"StatArb: {sym_a}/{sym_b} skipped — low corr {corr:.2f}")
                    continue

                pair_ok, hedge_ratio = self._test_cointegration(
                    data[sym_a]["close"], data[sym_b]["close"]
                )
                if pair_ok:
                    pair_key = f"{sym_a}|{sym_b}"
                    self._hedge_ratios[pair_key] = hedge_ratio
                    cointegrated.append((sym_a, sym_b))
                    log.info(f"StatArb: Cointegrated pair found: {sym_a} / {sym_b} (hedge_ratio={hedge_ratio:.4f}, corr={corr:.2f}, p<0.05)")
            except Exception as e:
                log.debug(f"Pair test error {sym_a}/{sym_b}: {e}")

        self.pairs = cointegrated
        return cointegrated

    def _test_cointegration(self, price_a: pd.Series, price_b: pd.Series) -> Tuple[bool, float]:
        """
        Engle-Granger cointegration test on log prices.
        Using log prices gives a more stable hedge ratio (less sensitive to price level).
        Returns (is_cointegrated, hedge_ratio)
        """
        try:
            from statsmodels.tsa.stattools import coint
            from statsmodels.regression.linear_model import OLS
            import statsmodels.api as sm

            # Align series
            aligned = pd.concat([price_a, price_b], axis=1).dropna()
            if len(aligned) < self.lookback:
                return False, 1.0

            # Use LOG prices — more stable ratio, avoids level-dependency
            log_a = np.log(aligned.iloc[:, 0].values)
            log_b = np.log(aligned.iloc[:, 1].values)

            # OLS: log_a = alpha + hedge_ratio * log_b + epsilon
            b_const = sm.add_constant(log_b)
            model = OLS(log_a, b_const).fit()
            hedge_ratio = float(model.params[1])

            # Sanity check — ratio should be positive and reasonable (0.1 to 10x)
            if not (0.1 <= abs(hedge_ratio) <= 10.0):
                hedge_ratio = 1.0

            # Cointegration test (p-value < 0.05 = cointegrated)
            _, pvalue, _ = coint(log_a, log_b)
            return pvalue < 0.05, hedge_ratio

        except ImportError:
            # statsmodels not installed — compute OLS manually using numpy
            aligned = pd.concat([price_a, price_b], axis=1).dropna()
            if len(aligned) < self.lookback:
                return False, 1.0
            log_a = np.log(aligned.iloc[:, 0].values)
            log_b = np.log(aligned.iloc[:, 1].values)
            # Manual OLS: hedge_ratio = cov(a,b) / var(b)
            hedge_ratio = float(np.cov(log_a, log_b)[0, 1] / np.var(log_b))
            hedge_ratio = max(0.1, min(10.0, abs(hedge_ratio)))
            # Use rolling correlation as cointegration proxy
            corr = float(np.corrcoef(log_a, log_b)[0, 1])
            return abs(corr) > 0.85, hedge_ratio

    def _compute_zscore(self, sym_a: str, sym_b: str, data: Dict[str, pd.DataFrame]) -> Optional[float]:
        """Compute current Z-score of log-price spread using the stored hedge ratio."""
        pair_key = f"{sym_a}|{sym_b}"
        ratio = self._hedge_ratios.get(pair_key, 1.0)

        if sym_a not in data or sym_b not in data:
            return None

        close_a = data[sym_a]["close"]
        close_b = data[sym_b]["close"]

        aligned = pd.concat([close_a, close_b], axis=1).dropna()
        if len(aligned) < 20:
            return None

        # Log-price spread — consistent with how hedge ratio was computed
        log_a = np.log(aligned.iloc[:, 0])
        log_b = np.log(aligned.iloc[:, 1])
        spread = log_a - ratio * log_b

        spread_mean = spread.mean()
        spread_std = spread.std()
        if spread_std < 1e-10:
            return None

        zscore = (spread.iloc[-1] - spread_mean) / spread_std
        return float(zscore)

    def generate_signal_for_pair(
        self, sym_a: str, sym_b: str, data: Dict[str, pd.DataFrame]
    ) -> Optional[List[TradeSignal]]:
        """
        Generate paired signals (one long, one short).
        Returns [signal_a, signal_b] or None.
        """
        zscore = self._compute_zscore(sym_a, sym_b, data)
        if zscore is None:
            return None

        if sym_a not in data or sym_b not in data:
            return None

        pair_key = f"{sym_a}|{sym_b}"
        active = self._active_pairs.get(pair_key)
        cmp_a = float(data[sym_a]["close"].iloc[-1])
        cmp_b = float(data[sym_b]["close"].iloc[-1])
        atr_a = float(data[sym_a].get("ATRr_14", pd.Series([cmp_a * 0.01])).iloc[-1])

        signals = []

        # Entry conditions
        if abs(zscore) > self.zscore_entry and not active:
            if zscore > 0:
                # Spread too wide: short A, long B
                self._active_pairs[pair_key] = "SHORT_A_LONG_B"
                signals = [
                    TradeSignal(
                        symbol=sym_a, side="SELL", strategy=self.name,
                        confidence=75.0,
                        trigger=f"StatArb: Z={zscore:.2f} short {sym_a}",
                        atr=atr_a, cmp=cmp_a,
                    ),
                    TradeSignal(
                        symbol=sym_b, side="BUY", strategy=self.name,
                        confidence=75.0,
                        trigger=f"StatArb: Z={zscore:.2f} long {sym_b}",
                        atr=atr_a, cmp=cmp_b,
                    )
                ]
            else:
                # Spread too narrow: long A, short B
                self._active_pairs[pair_key] = "LONG_A_SHORT_B"
                signals = [
                    TradeSignal(
                        symbol=sym_a, side="BUY", strategy=self.name,
                        confidence=75.0,
                        trigger=f"StatArb: Z={zscore:.2f} long {sym_a}",
                        atr=atr_a, cmp=cmp_a,
                    ),
                    TradeSignal(
                        symbol=sym_b, side="SELL", strategy=self.name,
                        confidence=75.0,
                        trigger=f"StatArb: Z={zscore:.2f} short {sym_b}",
                        atr=atr_a, cmp=cmp_b,
                    )
                ]

        # Exit condition
        elif active and abs(zscore) < self.zscore_exit:
            del self._active_pairs[pair_key]
            side_a = "SELL" if "LONG_A" in active else "BUY"
            side_b = "BUY" if "SHORT_B" in active else "SELL"
            signals = [
                TradeSignal(
                    symbol=sym_a, side=side_a, strategy=self.name,
                    confidence=70.0,
                    trigger=f"StatArb EXIT: Z={zscore:.2f}",
                    atr=atr_a, cmp=cmp_a,
                ),
                TradeSignal(
                    symbol=sym_b, side=side_b, strategy=self.name,
                    confidence=70.0,
                    trigger=f"StatArb EXIT: Z={zscore:.2f}",
                    atr=atr_a, cmp=cmp_b,
                )
            ]

        return signals if signals else None

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> Optional[TradeSignal]:
        """Single-symbol interface (required by base). For pairs, use generate_signal_for_pair."""
        return None  # Pairs only — use generate_signal_for_pair
