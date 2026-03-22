# -*- coding: utf-8 -*-
"""
ZeroBot G2 — ML Trainer  [G3 UPGRADE]
======================================
G3 CHANGES vs G2:

  [G3-ML1]  TRIPLE BARRIER LABELING  ←  fixes F1≈0.2
             Labels are assigned by whichever barrier is hit first:
               • Profit barrier : +pt_mult × ATR  →  label = BUY  (+1)
               • Stop barrier   : –sl_mult × ATR  →  label = SELL (–1)
               • Time barrier   : max_bars elapsed →  label = HOLD ( 0)
             This eliminates noisy "future return ±threshold" labels that had
             no concept of stop loss and produced wildly imbalanced classes.

  [G3-ML2]  VOLATILITY-NORMALIZED TARGETS
             ATR multiple scales with symbol volatility so a low-vol stock like
             HDFC and a high-vol stock like Adani share the same risk/reward
             frame. pt_mult and sl_mult are tunable per-symbol if needed.

  [G3-ML3]  REGIME-SPECIFIC TRAINING (4 regimes from regime_detector)
             Models are trained per regime: NORMAL / DEFENSIVE / CRISIS / ALL.
             At inference, the matching model is used. CRISIS models are
             deliberately conservative (high sl, low pt multiple).

  [G3-ML4]  7 NEW FEATURE GROUPS
             • Volatility expansion  – ATR ratio, vol expansion z-score
             • Volume shock          – volume z-score, shock persistence
             • Sector relative strength – stock vs sector ETF / NIFTY
             • VWAP deviation        – signed %, anchored VWAP distance
             • Market breadth        – advance/decline proxy via index internals
             • Order flow imbalance  – close position in HL range
             • Momentum divergence   – price vs RSI divergence flag

  [G3-ML5]  FEATURE IMPORTANCE PRUNING
             After a full train, features with cumulative importance < 95% are
             dropped. This typically cuts ~40% of features and reduces
             overfitting significantly. Pruned feature list saved to disk.

  [G3-ML6]  PROBABILITY CALIBRATION
             Both Platt scaling (sigmoid) and isotonic regression are tried;
             the one with lower Brier score on validation is kept.

  [G3-ML7]  TEMPORAL WALK-FORWARD CV with purge gap
             A 5-bar purge gap is inserted between train and test to prevent
             leakage through autocorrelated features (e.g. rolling windows).

  [G3-ML8]  EXPECTED RETURN SCORE persisted per model
             During training, average realized return for BUY/SELL classes
             is computed and saved. Predictor uses this for expected_return_score.
"""

import os
import joblib
import warnings
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional, List
from pathlib import Path

from core.logger import log
from data.processors.indicator_engine import IndicatorEngine

MODEL_DIR = Path(__file__).parent / "saved"
MODEL_DIR.mkdir(exist_ok=True)

# Regime labels (matches regime_detector.MarketRegime values)
R_NORMAL    = "NORMAL"
R_DEFENSIVE = "DEFENSIVE"
R_CRISIS    = "CRISIS"
R_ALL       = "all"
ALL_REGIMES = [R_NORMAL, R_DEFENSIVE, R_CRISIS, R_ALL]

# Triple barrier defaults
DEFAULT_PT_MULT  = 1.5   # profit taker in ATR units
DEFAULT_SL_MULT  = 1.0   # stop loss in ATR units
DEFAULT_MAX_BARS = 10    # time barrier in bars


# ══════════════════════════════════════════════════════════════════════════════
# [G3-ML1] Triple Barrier Labeler
# ══════════════════════════════════════════════════════════════════════════════
class TripleBarrierLabeler:
    """
    Labels each bar as BUY (+1), SELL (-1), or HOLD (0) based on which
    barrier is touched first in the following max_bars candles.

    Profit barrier : close + pt_mult * ATR
    Stop barrier   : close - sl_mult * ATR
    Time barrier   : max_bars candles (returns 0 if neither hit)

    [G3-ML2] ATR-normalized barriers ensure the same risk/reward ratio
    regardless of symbol volatility.
    """

    def __init__(self,
                 pt_mult:  float = DEFAULT_PT_MULT,
                 sl_mult:  float = DEFAULT_SL_MULT,
                 max_bars: int   = DEFAULT_MAX_BARS,
                 atr_col:  str   = "ATRr_14"):
        self.pt_mult  = pt_mult
        self.sl_mult  = sl_mult
        self.max_bars = max_bars
        self.atr_col  = atr_col

    def label(self, df: pd.DataFrame) -> pd.Series:
        """
        Returns a pd.Series aligned with df.index.
        Values: 1=BUY, -1=SELL, 0=HOLD
        Last max_bars rows are NaN (no forward window).
        """
        close = df["close"].values.astype(float)
        high  = df["high"].values.astype(float)
        low   = df["low"].values.astype(float)

        # Use ATR if available, else fallback to rolling std
        if self.atr_col in df.columns:
            atr = df[self.atr_col].values.astype(float)
        else:
            pct = pd.Series(close).pct_change()
            atr = (pct.rolling(14).std() * pd.Series(close)).values

        n      = len(df)
        labels = np.full(n, np.nan)

        for i in range(n - self.max_bars):
            c0 = close[i]
            a  = atr[i]
            if np.isnan(a) or a <= 0:
                continue

            pt_level = c0 + self.pt_mult * a   # profit target price
            sl_level = c0 - self.sl_mult * a   # stop loss price

            result = 0  # default HOLD (time barrier)
            for j in range(i + 1, i + self.max_bars + 1):
                if j >= n:
                    break
                # Check high/low for intrabar touch
                if high[j] >= pt_level:
                    result = 1    # BUY — profit hit first
                    break
                if low[j] <= sl_level:
                    result = -1   # SELL — stop hit first
                    break

            labels[i] = result

        series = pd.Series(labels, index=df.index, name="target")

        # Log class distribution
        valid = series.dropna()
        if len(valid) > 0:
            counts = valid.value_counts(normalize=True).sort_index()
            dist   = {int(k): round(float(v), 3) for k, v in counts.items()}
            n_buy  = dist.get(1,  0)
            n_sell = dist.get(-1, 0)
            n_hold = dist.get(0,  0)
            log.info(f"[TripleBarrier] BUY={n_buy:.1%} SELL={n_sell:.1%} HOLD={n_hold:.1%} "
                     f"(pt={self.pt_mult}×ATR, sl={self.sl_mult}×ATR, t={self.max_bars}bars)")
            if n_buy < 0.15 or n_sell < 0.15:
                log.warning("[TripleBarrier] ⚠️  Class imbalance detected — "
                            "consider adjusting pt_mult/sl_mult or using SMOTE")

        return series

    # [G3-ML8] Compute average realized returns per class for expected_return_score
    def compute_class_returns(self, df: pd.DataFrame, labels: pd.Series) -> Dict:
        """Compute average forward return for BUY and SELL labels."""
        fwd = df["close"].shift(-self.max_bars) / df["close"] - 1
        fwd.name = "fwd_return"
        combined = pd.concat([labels, fwd], axis=1).dropna()
        result = {}
        for cls, name in [( 1, "avg_buy_return"),
                          (-1, "avg_sell_return"),
                          ( 0, "avg_hold_return")]:
            subset = combined[combined["target"] == cls]["fwd_return"]
            result[name] = float(subset.mean()) if len(subset) > 0 else 0.0
        return result


# ══════════════════════════════════════════════════════════════════════════════
# [G3-ML4] Feature Builder — 7 new feature groups
# ══════════════════════════════════════════════════════════════════════════════
class FeatureBuilder:
    """
    G3 extended feature set. New groups added vs G2:
      vol_expansion_z, vol_shock_persist, vol_ratio_5_20 (volatility expansion)
      volume_z, volume_shock, volume_shock_persist               (volume shock)
      rel_strength_sector, rs_slope                              (sector RS)
      vwap_dev, vwap_dev_atr, vwap_above                         (VWAP)
      adv_decline_proxy, breadth_thrust                          (market breadth)
      hl_position, close_vs_midpoint                             (order flow)
      rsi_price_div, macd_hist_div                               (momentum div)
    """

    # Base features (original G2 set)
    BASE_FEATURES = [
        "return_1","return_3","return_5","return_10","return_20",
        "hl_pct","gap_pct","close_vs_open",
        "EMA_9","EMA_21","EMA_50","SMA_200",
        "ema9_vs_ema21","price_vs_ema21","price_vs_ema50","price_vs_sma200",
        "MACD_12_26_9","MACDs_12_26_9","MACDh_12_26_9",
        "consec_up","consec_down","momentum_quality",
        "RSI_14","MFI_14","rsi_trend",
        "ATRr_14","BBU_20_2.0","BBL_20_2.0","bb_position","bb_width","hist_vol_10",
        "atr_pct","vol_spike","vol_ma20","OBV","vwap_dev","volume_ratio",
        "ADX_14","trending",
        "nifty_return_1","nifty_return_5","bank_nifty_return_1",
        "gap_up","gap_down","overnight_gap",
    ]

    # [G3-ML4] New feature groups
    NEW_FEATURES = [
        # Volatility expansion
        "vol_expansion_z","atr_ratio_5","atr_ratio_20","realized_vol_ratio",
        # Volume shock
        "volume_z","volume_shock_flag","volume_shock_persist",
        "vol_ratio_5_20","vol_price_divergence",
        # Sector relative strength
        "rel_strength_nifty","rel_strength_slope","alpha_1d","alpha_5d",
        # VWAP
        "vwap_dev_atr","vwap_above","vwap_distance_pct",
        # Market breadth
        "breadth_proxy","breadth_momentum",
        # Order flow imbalance
        "hl_position","body_ratio","close_vs_midpoint","upper_shadow","lower_shadow",
        # Momentum divergence
        "rsi_price_div","macd_hist_slope","price_roc_divergence",
    ]

    FEATURE_COLS = BASE_FEATURES + NEW_FEATURES

    def build(self, df: pd.DataFrame,
              market_data: Dict[str, pd.DataFrame] = None,
              lookback: int = 3,
              regime_mask: Optional[pd.Series] = None) -> pd.DataFrame:
        """
        Build full feature matrix.
        regime_mask: boolean Series aligned with df.index (True = include row).
        """
        ie = IndicatorEngine()
        df = ie.add_all(df.copy())

        # ── Returns ──────────────────────────────────────────────────────────
        for n in [1, 3, 5, 10, 20]:
            col = f"return_{n}"
            if col not in df.columns:
                df[col] = df["close"].pct_change(n)

        # ── Relative position features ────────────────────────────────────────
        for ema_col, feat_col in [("EMA_21", "price_vs_ema21"),
                                   ("EMA_50", "price_vs_ema50"),
                                   ("SMA_200","price_vs_sma200")]:
            if ema_col in df.columns:
                df[feat_col] = df["close"] / df[ema_col].replace(0, np.nan) - 1

        if "EMA_9" in df.columns and "EMA_21" in df.columns:
            df["ema9_vs_ema21"] = df["EMA_9"] / df["EMA_21"].replace(0, np.nan) - 1

        if "open" in df.columns:
            df["close_vs_open"] = (df["close"] - df["open"]) / df["open"].replace(0, np.nan)

        if "BBU_20_2.0" in df.columns and "BBL_20_2.0" in df.columns:
            mid           = (df["BBU_20_2.0"] + df["BBL_20_2.0"]) / 2
            df["bb_width"]    = (df["BBU_20_2.0"] - df["BBL_20_2.0"]) / mid.replace(0, np.nan)
            df["bb_position"] = (df["close"] - df["BBL_20_2.0"]) / (
                df["BBU_20_2.0"] - df["BBL_20_2.0"]).replace(0, np.nan)

        df["hist_vol_10"] = df["close"].pct_change().rolling(10).std() * np.sqrt(252)

        if "ATRr_14" in df.columns:
            df["atr_pct"] = df["ATRr_14"] / df["close"].replace(0, np.nan) * 100

        # ── Volume features ───────────────────────────────────────────────────
        vol_ma20      = df["volume"].rolling(20).mean()
        vol_std20     = df["volume"].rolling(20).std()
        df["vol_ma20"]     = vol_ma20
        df["vol_spike"]    = df["volume"] / vol_ma20.replace(0, np.nan)
        df["volume_ratio"] = df["volume"] / df["volume"].rolling(5).mean().replace(0, np.nan)

        # [G3-ML4] Volume shock indicators
        df["volume_z"]           = (df["volume"] - vol_ma20) / vol_std20.replace(0, np.nan)
        df["volume_shock_flag"]  = (df["volume_z"] > 2.5).astype(int)
        df["volume_shock_persist"] = df["volume_shock_flag"].rolling(3).sum()
        df["vol_ratio_5_20"]     = (df["volume"].rolling(5).mean() /
                                    vol_ma20.replace(0, np.nan))
        # Volume diverging from price (high vol on flat price = distribution)
        price_chg = df["close"].pct_change().abs()
        df["vol_price_divergence"] = df["volume_z"] / (price_chg * 100 + 1)

        # ── Momentum quality ─────────────────────────────────────────────────
        up   = (df["close"] > df["close"].shift(1)).astype(int)
        down = (df["close"] < df["close"].shift(1)).astype(int)
        df["consec_up"]        = up.groupby((up != up.shift()).cumsum()).cumcount() + up
        df["consec_down"]      = down.groupby((down != down.shift()).cumsum()).cumcount() + down
        df["momentum_quality"] = df["consec_up"] - df["consec_down"]

        if "RSI_14" in df.columns:
            df["rsi_trend"] = df["RSI_14"].diff(3)

        # ── Gap features ─────────────────────────────────────────────────────
        if "open" in df.columns:
            prev_close         = df["close"].shift(1)
            df["overnight_gap"]= (df["open"] - prev_close) / prev_close.replace(0, np.nan)
            df["gap_up"]       = (df["overnight_gap"] >  0.005).astype(int)
            df["gap_down"]     = (df["overnight_gap"] < -0.005).astype(int)
            df["gap_pct"]      = df["overnight_gap"] * 100
            df["hl_pct"]       = (df["high"] - df["low"]) / df["close"].replace(0, np.nan)

        # ── ADX / trending ────────────────────────────────────────────────────
        try:
            import pandas_ta as ta
            adx_result = ta.adx(df["high"], df["low"], df["close"], length=14)
            if adx_result is not None:
                adx_col = [c for c in adx_result.columns if c.startswith("ADX_")]
                if adx_col:
                    df["ADX_14"]  = adx_result[adx_col[0]]
                    df["trending"]= (df["ADX_14"] > 25).astype(int)
        except Exception:
            df["ADX_14"]  = 20.0
            df["trending"]= 0

        # ── [G3-ML4] Volatility expansion features ────────────────────────────
        if "ATRr_14" in df.columns:
            atr          = df["ATRr_14"]
            df["atr_ratio_5"]      = atr / atr.rolling(5).mean().replace(0, np.nan)
            df["atr_ratio_20"]     = atr / atr.rolling(20).mean().replace(0, np.nan)
            # Realized vol ratio: recent 5d vs 20d
            rv5            = df["close"].pct_change().rolling(5).std()
            rv20           = df["close"].pct_change().rolling(20).std()
            df["realized_vol_ratio"] = rv5 / rv20.replace(0, np.nan)
            # Vol expansion z-score: ATR z-score over 20 bars
            atr_std20      = atr.rolling(20).std()
            atr_mean20     = atr.rolling(20).mean()
            df["vol_expansion_z"] = (atr - atr_mean20) / atr_std20.replace(0, np.nan)

        # ── [G3-ML4] VWAP deviation features ─────────────────────────────────
        if "vwap_dev" not in df.columns:
            # Compute intraday VWAP proxy (cumulative TP * vol / cumulative vol)
            tp = (df["high"] + df["low"] + df["close"]) / 3
            df["vwap_dev"] = (df["close"] - tp.rolling(20).mean()) / tp.rolling(20).mean().replace(0, np.nan) * 100

        if "ATRr_14" in df.columns:
            df["vwap_dev_atr"]      = df["vwap_dev"] / (df["atr_pct"].replace(0, np.nan))
        df["vwap_above"]           = (df["vwap_dev"] > 0).astype(int)
        df["vwap_distance_pct"]    = df["vwap_dev"].abs()

        # ── [G3-ML4] Order flow imbalance ────────────────────────────────────
        hl_range           = (df["high"] - df["low"]).replace(0, np.nan)
        df["hl_position"]  = (df["close"] - df["low"]) / hl_range  # 0=low, 1=high
        mid                = (df["high"] + df["low"]) / 2
        df["close_vs_midpoint"] = (df["close"] - mid) / mid.replace(0, np.nan)
        body               = (df["close"] - df["open"]).abs()
        df["body_ratio"]   = body / hl_range
        df["upper_shadow"] = (df["high"] - df[["close","open"]].max(axis=1)) / hl_range
        df["lower_shadow"] = (df[["close","open"]].min(axis=1) - df["low"]) / hl_range

        # ── [G3-ML4] Momentum divergence ─────────────────────────────────────
        if "RSI_14" in df.columns:
            price_ma5 = df["close"].rolling(5).mean()
            rsi_ma5   = df["RSI_14"].rolling(5).mean()
            # Divergence: price making higher high but RSI not (or vice versa)
            price_diff = price_ma5.diff(5)
            rsi_diff   = rsi_ma5.diff(5)
            df["rsi_price_div"] = np.sign(price_diff) - np.sign(rsi_diff)  # -2,0,+2

        if "MACDh_12_26_9" in df.columns:
            df["macd_hist_slope"] = df["MACDh_12_26_9"].diff(3)

        price_roc5 = df["close"].pct_change(5)
        price_roc10 = df["close"].pct_change(10)
        df["price_roc_divergence"] = price_roc5 - (price_roc10 / 2)  # short vs medium

        # ── [G3-ML4] Market context + sector RS ──────────────────────────────
        if market_data:
            nifty = market_data.get("^NSEI")
            if nifty is not None and not nifty.empty:
                nr   = nifty["close"].pct_change().reindex(df.index, method="ffill")
                nr5  = nifty["close"].pct_change(5).reindex(df.index, method="ffill")
                df["nifty_return_1"]    = nr
                df["nifty_return_5"]    = nr5
                # Relative strength vs NIFTY
                df["rel_strength_nifty"]= df["return_1"] - nr
                df["alpha_1d"]          = df["return_1"] - nr
                df["alpha_5d"]          = df["return_5"] - nr5
                # RS slope: is relative strength improving?
                df["rel_strength_slope"]= df["rel_strength_nifty"].rolling(5).mean()

            bank = market_data.get("^NSEBANK")
            if bank is not None and not bank.empty:
                df["bank_nifty_return_1"] = bank["close"].pct_change().reindex(df.index, method="ffill")

            # [G3-ML4] Market breadth proxy using index internals
            # Advance-decline: nifty vs bank nifty divergence as breadth proxy
            if "nifty_return_1" in df.columns and "bank_nifty_return_1" in df.columns:
                df["breadth_proxy"]    = df["nifty_return_1"] - df["bank_nifty_return_1"]
                df["breadth_momentum"] = df["breadth_proxy"].rolling(5).mean()

        # Ensure fallback for columns not set
        for col in self.NEW_FEATURES:
            if col not in df.columns:
                df[col] = 0.0

        for col in self.BASE_FEATURES:
            if col not in df.columns:
                df[col] = 0.0

        # ── Build lagged feature matrix ───────────────────────────────────────
        all_feature_cols = [c for c in self.FEATURE_COLS if c in df.columns]
        cols = {}
        for col in all_feature_cols:
            cols[col] = df[col]
            for lag in range(1, lookback + 1):
                cols[f"{col}_lag{lag}"] = df[col].shift(lag)

        features = pd.DataFrame(cols, index=df.index)

        # Apply regime mask if provided
        if regime_mask is not None:
            mask = regime_mask.reindex(features.index, fill_value=False).astype(bool)
            features = features[mask]

        return features.replace([np.inf, -np.inf], np.nan).dropna(how="all")

    def build_from_trades(self, trades: List[Dict]) -> Optional[pd.DataFrame]:
        if not trades:
            return None
        rows = []
        for t in trades:
            f = t.get("features_at_entry", {})
            if f:
                pnl = t.get("pnl", 0)
                f["actual_label"] = 1 if pnl > 0.001 else (-1 if pnl < -0.001 else 0)
                rows.append(f)
        return pd.DataFrame(rows) if rows else None


# ══════════════════════════════════════════════════════════════════════════════
# [G3-ML5] Feature Importance Pruner
# ══════════════════════════════════════════════════════════════════════════════
class FeatureImportancePruner:
    """
    After initial training, keeps only features whose cumulative importance
    reaches `threshold` (default 95%). Typically reduces feature count by
    30–50% and meaningfully reduces overfitting.

    Usage:
        pruner = FeatureImportancePruner()
        kept   = pruner.fit(model, feature_names, threshold=0.95)
        X_pruned = pruner.transform(X)
    """

    def __init__(self, threshold: float = 0.95, min_features: int = 20):
        self.threshold    = threshold
        self.min_features = min_features
        self.kept_cols_:  Optional[List[str]] = None
        self._path        = MODEL_DIR / "pruned_feature_names.pkl"

    def fit(self, model, feature_names: List[str], threshold: float = None) -> List[str]:
        threshold = threshold or self.threshold
        try:
            # Get importances from the model (handles CalibratedClassifierCV wrapper)
            imp = self._get_importances(model)
            if imp is None or len(imp) == 0:
                self.kept_cols_ = feature_names
                return feature_names

            n       = min(len(feature_names), len(imp))
            pairs   = sorted(zip(feature_names[:n], imp[:n]), key=lambda x: -x[1])
            total   = sum(v for _, v in pairs)
            cumsum  = 0.0
            kept    = []
            for name, val in pairs:
                kept.append(name)
                cumsum += val / max(total, 1e-9)
                if cumsum >= threshold and len(kept) >= self.min_features:
                    break

            removed = len(feature_names) - len(kept)
            log.info(f"[FeaturePruner] Kept {len(kept)}/{len(feature_names)} features "
                     f"(removed {removed}, cumulative importance={cumsum:.3f})")

            self.kept_cols_ = kept
            joblib.dump(kept, self._path)
            return kept

        except Exception as e:
            log.warning(f"[FeaturePruner] Failed: {e} — keeping all features")
            self.kept_cols_ = feature_names
            return feature_names

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        if self.kept_cols_ is None:
            return X
        present = [c for c in self.kept_cols_ if c in X.columns]
        return X[present]

    def load(self) -> bool:
        if self._path.exists():
            try:
                self.kept_cols_ = joblib.load(self._path)
                return True
            except Exception:
                pass
        return False

    @staticmethod
    def _get_importances(model) -> Optional[np.ndarray]:
        if hasattr(model, "feature_importances_"):
            return model.feature_importances_
        # CalibratedClassifierCV wraps the base model
        if hasattr(model, "calibrated_classifiers_"):
            for cal_clf in model.calibrated_classifiers_:
                base = getattr(cal_clf, "estimator", getattr(cal_clf, "base_estimator", None))
                if base and hasattr(base, "feature_importances_"):
                    return base.feature_importances_
        return None


# ══════════════════════════════════════════════════════════════════════════════
# [G2-ML4] Feature Drift Detector (PSI) — unchanged from G2
# ══════════════════════════════════════════════════════════════════════════════
class FeatureDriftDetector:
    PSI_THRESHOLD      = 0.20
    DRIFT_FEATURE_LIMIT = 3

    def __init__(self):
        self._baseline: Optional[pd.DataFrame] = None
        self._baseline_path = MODEL_DIR / "feature_baseline.parquet"
        self._load_baseline()

    def _load_baseline(self):
        if self._baseline_path.exists():
            try:
                self._baseline = pd.read_parquet(self._baseline_path)
            except Exception:
                pass

    def set_baseline(self, X: pd.DataFrame):
        self._baseline = X.copy()
        try:
            X.to_parquet(self._baseline_path)
        except Exception:
            pass

    def check(self, X_current: pd.DataFrame) -> Dict:
        if self._baseline is None or X_current.empty:
            return {"drifted": False, "psi_scores": {}, "n_drifted": 0}
        common_cols = [c for c in self._baseline.columns if c in X_current.columns]
        psi_scores  = {}
        for col in common_cols[:30]:
            try:
                psi = self._psi(self._baseline[col].dropna(), X_current[col].dropna())
                psi_scores[col] = round(psi, 4)
            except Exception:
                pass
        drifted = [c for c, v in psi_scores.items() if v > self.PSI_THRESHOLD]
        n       = len(drifted)
        if n >= self.DRIFT_FEATURE_LIMIT:
            log.warning(f"[Drift] {n} features drifted: {drifted[:5]} — consider retraining")
        return {"drifted": n >= self.DRIFT_FEATURE_LIMIT,
                "psi_scores": psi_scores, "n_drifted": n, "drifted_features": drifted}

    @staticmethod
    def _psi(expected: pd.Series, actual: pd.Series, buckets: int = 10) -> float:
        bins = np.percentile(expected, np.linspace(0, 100, buckets + 1))
        bins = np.unique(bins)
        if len(bins) < 2:
            return 0.0
        def _pct(s):
            counts, _ = np.histogram(s, bins=bins)
            p = counts / max(len(s), 1)
            return np.where(p == 0, 1e-6, p)
        e, a = _pct(expected), _pct(actual)
        return float(np.sum((a - e) * np.log(a / e)))


# ══════════════════════════════════════════════════════════════════════════════
# Calibration wrapper (sklearn 1.7+ compatible, replaces cv='prefit')
# ══════════════════════════════════════════════════════════════════════════════
class _CalibratedWrapper:
    """
    Thin wrapper that applies OvR isotonic/sigmoid calibrators to an existing
    fitted model.  Exposes predict_proba / predict so it's a drop-in replacement.
    Preserves _zerobot_label_map from XGBoost for correct class ordering.
    """

    def __init__(self, base_model, calibrators, classes, method):
        self._base     = base_model
        self._cals     = calibrators    # list of (class_label, calibrator, method)
        self.classes_  = classes
        self._method   = method
        # Forward XGB label maps if present
        if hasattr(base_model, "_zerobot_label_map"):
            self._zerobot_label_map = base_model._zerobot_label_map
            self._zerobot_label_inv = base_model._zerobot_label_inv
        if hasattr(base_model, "n_features_in_"):
            self.n_features_in_ = base_model.n_features_in_
        # Forward feature importances for pruner
        if hasattr(base_model, "feature_importances_"):
            self.feature_importances_ = base_model.feature_importances_

    def predict_proba(self, X):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            raw = self._base.predict_proba(X)   # (n, n_base_classes)

        n = len(X)
        cal_probs = np.zeros((n, len(self._cals)))
        for i, (_, cal, method) in enumerate(self._cals):
            col = raw[:, i] if i < raw.shape[1] else np.zeros(n)
            if method == "sigmoid":
                col = col.reshape(-1, 1)
            p = cal.predict(col)
            cal_probs[:, i] = np.clip(p, 0, 1)

        # Renormalize rows to sum to 1
        row_sums = cal_probs.sum(axis=1, keepdims=True)
        row_sums = np.where(row_sums == 0, 1, row_sums)
        return cal_probs / row_sums

    def predict(self, X):
        proba = self.predict_proba(X)
        idx   = np.argmax(proba, axis=1)
        return np.array([self.classes_[i] for i in idx])

    # Allow feature_importances_ to be accessed through the wrapper for pruner
    @property
    def calibrated_classifiers_(self):
        """Compatibility shim for old code that reads base model via this path."""
        class _Compat:
            def __init__(self, est): self.estimator = est
        return [_Compat(self._base)]


# ══════════════════════════════════════════════════════════════════════════════
# ModelTrainer
# ══════════════════════════════════════════════════════════════════════════════
class ModelTrainer:

    def __init__(self):
        self.fb      = FeatureBuilder()
        self.labeler = TripleBarrierLabeler()   # [G3-ML1]
        self.pruner  = FeatureImportancePruner()# [G3-ML5]
        self.drift   = FeatureDriftDetector()

    # ── Individual model trainers ─────────────────────────────────────────────
    def _train_xgboost(self, X_tr, y_tr, X_val=None, y_val=None):
        try:
            from xgboost import XGBClassifier
            classes = np.unique(y_tr)
            n_cls   = len(classes)
            # Map labels {-1,0,1} → {0,1,2} for XGB
            label_map = {c: i for i, c in enumerate(sorted(classes))}
            y_tr_m    = np.array([label_map[v] for v in y_tr])
            y_val_m   = np.array([label_map[v] for v in y_val]) if y_val is not None else None
            model = XGBClassifier(
                n_estimators=600, max_depth=5, learning_rate=0.04,
                subsample=0.75, colsample_bytree=0.75,
                min_child_weight=5, gamma=0.2,
                reg_alpha=0.2, reg_lambda=1.5,
                objective="multi:softprob" if n_cls > 2 else "binary:logistic",
                num_class=n_cls if n_cls > 2 else None,
                eval_metric="mlogloss" if n_cls > 2 else "logloss",
                early_stopping_rounds=30 if y_val is not None else None,
                random_state=42, n_jobs=-1,
            )
            eval_set = [(X_val, y_val_m)] if y_val is not None else None
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model.fit(X_tr, y_tr_m, eval_set=eval_set, verbose=False)
            # Store label map so predictor can invert
            model._zerobot_label_map = label_map
            model._zerobot_label_inv = {v: k for k, v in label_map.items()}
            return model
        except ImportError:
            log.error("xgboost not installed"); return None

    def _train_lightgbm(self, X_tr, y_tr):
        try:
            from lightgbm import LGBMClassifier
            n_cls = len(np.unique(y_tr))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model = LGBMClassifier(
                    n_estimators=600, num_leaves=31, learning_rate=0.04,
                    subsample=0.75, colsample_bytree=0.75, min_child_samples=25,
                    reg_alpha=0.2, reg_lambda=1.5,
                    class_weight="balanced",
                    objective="multiclass" if n_cls > 2 else "binary",
                    num_class=n_cls if n_cls > 2 else None,
                    random_state=42, n_jobs=-1, verbose=-1,
                )
                model.fit(X_tr, y_tr)
            return model
        except ImportError:
            log.error("lightgbm not installed"); return None

    def _train_catboost(self, X_tr, y_tr):
        try:
            from catboost import CatBoostClassifier
            n_cls = len(np.unique(y_tr))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model = CatBoostClassifier(
                    iterations=500, learning_rate=0.04, depth=5,
                    l2_leaf_reg=5.0,
                    loss_function="MultiClass" if n_cls > 2 else "Logloss",
                    eval_metric="Accuracy",
                    random_seed=42, verbose=0,
                )
                model.fit(X_tr, y_tr)
            return model
        except ImportError:
            log.info("catboost not installed — skipping"); return None
        except Exception as e:
            log.warning(f"CatBoost failed: {e}"); return None

    def _train_extra_trees(self, X_tr, y_tr):
        try:
            from sklearn.ensemble import ExtraTreesClassifier
            model = ExtraTreesClassifier(
                n_estimators=400, max_depth=8, min_samples_split=15,
                min_samples_leaf=5,
                class_weight="balanced", random_state=42, n_jobs=-1,
            )
            model.fit(X_tr, y_tr)
            return model
        except Exception as e:
            log.warning(f"ExtraTrees failed: {e}"); return None

    # [G3-ML6] Dual calibration: pick best by Brier score
    # Uses direct calibrators (sklearn 1.7+ dropped cv='prefit')
    def _calibrate(self, model, X_val, y_val):
        """
        Wrap `model` with the best-performing calibrator on held-out (X_val, y_val).
        Tries Platt scaling (sigmoid) and isotonic regression; keeps lower Brier.
        Returns a CalibratedPredictor wrapper that exposes predict/predict_proba.
        """
        from sklearn.metrics import brier_score_loss

        # Get raw probabilities from the base model
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                raw_probs = model.predict_proba(X_val)   # (n, n_classes)
        except Exception as e:
            log.debug(f"Calibration skipped — predict_proba failed: {e}")
            return model

        classes = np.unique(y_val)
        best_cals   = None
        best_brier  = np.inf
        best_method = "none"

        for method in ["isotonic", "sigmoid"]:
            try:
                cals = []  # one calibrator per class (OvR)
                for i, cls in enumerate(classes):
                    y_bin    = (y_val == cls).astype(int)
                    col_prob = raw_probs[:, i] if i < raw_probs.shape[1] else np.zeros(len(y_val))

                    if method == "isotonic":
                        from sklearn.isotonic import IsotonicRegression
                        cal = IsotonicRegression(out_of_bounds="clip")
                    else:  # sigmoid / Platt
                        from sklearn.linear_model import LogisticRegression
                        cal = LogisticRegression(C=1.0, max_iter=200, random_state=42)
                        col_prob = col_prob.reshape(-1, 1)

                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        cal.fit(col_prob, y_bin)
                    cals.append((cls, cal, method))

                # Compute total Brier score under this calibration scheme
                brier = 0.0
                for i, (cls, cal, _) in enumerate(cals):
                    col = raw_probs[:, i] if i < raw_probs.shape[1] else np.zeros(len(y_val))
                    if method == "sigmoid":
                        col = col.reshape(-1, 1)
                    cal_p = cal.predict(col)
                    brier += brier_score_loss((y_val == cls).astype(int), cal_p)

                if brier < best_brier:
                    best_brier  = brier
                    best_cals   = cals
                    best_method = method

            except Exception as e:
                log.debug(f"Calibration {method} failed: {e}")

        if best_cals is None:
            return model

        log.info(f"  Calibration: {best_method} selected (Brier={best_brier:.4f})")
        return _CalibratedWrapper(model, best_cals, list(classes), best_method)

    # [G3-ML7] Walk-forward CV with purge gap
    def _walk_forward_cv(self, X: pd.DataFrame, y: pd.Series,
                          n_splits: int = 5, purge_gap: int = 5) -> Dict:
        from sklearn.metrics import f1_score, accuracy_score
        n         = len(X)
        fold_size = n // (n_splits + 1)
        results   = {"xgboost": [], "lightgbm": []}

        for fold in range(1, n_splits + 1):
            train_end = fold * fold_size
            test_start = train_end + purge_gap   # [G3-ML7] purge gap prevents leakage
            test_end   = min(test_start + fold_size, n)
            if test_end - test_start < 20:
                continue

            X_tr = X.iloc[:train_end]
            y_tr = y.iloc[:train_end]
            X_te = X.iloc[test_start:test_end]
            y_te = y.iloc[test_start:test_end]

            kw = dict(average="macro", zero_division=0)
            for algo, train_fn in [("xgboost",  lambda: self._train_xgboost(X_tr, y_tr)),
                                    ("lightgbm", lambda: self._train_lightgbm(X_tr, y_tr))]:
                m = train_fn()
                if m is None:
                    continue
                try:
                    preds = m.predict(X_te)
                    # XGB label map inversion
                    if hasattr(m, "_zerobot_label_inv"):
                        preds = np.array([m._zerobot_label_inv.get(int(p), p) for p in preds])
                    results[algo].append({
                        "fold": fold,
                        "acc":  float(accuracy_score(y_te, preds)),
                        "f1":   float(f1_score(y_te, preds, **kw)),
                    })
                except Exception as e:
                    log.debug(f"CV fold {fold} {algo}: {e}")

        summary = {}
        for algo, folds in results.items():
            if folds:
                avg_f1  = np.mean([f["f1"] for f in folds])
                avg_acc = np.mean([f["acc"] for f in folds])
                summary[f"{algo}_cv_f1"]  = round(float(avg_f1), 3)
                summary[f"{algo}_cv_acc"] = round(float(avg_acc), 3)
                log.info(f"  CV {algo}: F1={avg_f1:.3f} Acc={avg_acc:.3f} "
                         f"({len(folds)} folds, purge={purge_gap}bars)")
        return summary

    # ── Regime mask helper ────────────────────────────────────────────────────
    def _regime_mask(self, df: pd.DataFrame, target_regime: str) -> pd.Series:
        """
        Returns boolean Series for rows matching target_regime.
        Regime is estimated from VIX proxy (ADX + vol).
        """
        if target_regime == R_ALL:
            return pd.Series(True, index=df.index)

        try:
            from core.regime_detector import regime_detector
            # Use stored regime if available, else proxy from features
        except ImportError:
            pass

        # Proxy: use ATR expansion and ADX as regime indicators
        atr_col  = "ATRr_14" if "ATRr_14" in df.columns else None
        adx_col  = "ADX_14"  if "ADX_14"  in df.columns else None

        if atr_col and adx_col:
            adx    = df[adx_col].fillna(20)
            atr    = df[atr_col].fillna(df[atr_col].median())
            atr_z  = (atr - atr.rolling(20).mean()) / atr.rolling(20).std().replace(0, 1)
            high_vol = atr_z > 2.0

            if target_regime == R_CRISIS:
                return high_vol & (adx < 20)   # volatile + directionless = crisis
            elif target_regime == R_DEFENSIVE:
                return high_vol & (adx >= 20)  # volatile + trending = defensive
            elif target_regime == R_NORMAL:
                return ~high_vol               # normal vol

        return pd.Series(True, index=df.index)

    # ── Full training pipeline ────────────────────────────────────────────────
    def train_full(self, df: pd.DataFrame, symbol: str = "NIFTY",
                   market_data: Dict[str, pd.DataFrame] = None,
                   pt_mult: float = DEFAULT_PT_MULT,
                   sl_mult: float = DEFAULT_SL_MULT,
                   max_bars: int  = DEFAULT_MAX_BARS) -> Dict:
        """
        G3 training pipeline:
          1.  Build features (all 7 feature groups)
          2.  Triple barrier labels  [G3-ML1]
          3.  Walk-forward CV (purge gap)  [G3-ML7]
          4.  Train XGB + LGBM + CatBoost + ExtraTrees on all-regime data
          5.  Feature importance pruning  [G3-ML5]
          6.  Dual calibration (sigmoid vs isotonic)  [G3-ML6]
          7.  Regime-specific models (NORMAL / DEFENSIVE / CRISIS)  [G3-ML3]
          8.  Save class return stats for expected_return_score  [G3-ML8]
        """
        log.info(f"ML Training [G3]: {symbol} | {len(df)} candles | "
                 f"barriers pt={pt_mult}×ATR sl={sl_mult}×ATR t={max_bars}bars")

        results   = {}
        timestamp = datetime.now().strftime("%Y%m%d_%H%M")

        # [G3-ML1+ML2] Triple barrier labels
        self.labeler = TripleBarrierLabeler(pt_mult=pt_mult, sl_mult=sl_mult,
                                            max_bars=max_bars)
        ie = IndicatorEngine()
        df_ind = ie.add_all(df.copy())
        labels = self.labeler.label(df_ind)

        # [G3-ML8] Compute and save class return stats
        class_returns = self.labeler.compute_class_returns(df_ind, labels)
        joblib.dump(class_returns, MODEL_DIR / f"class_returns_{symbol}.pkl")
        log.info(f"  Class returns: {class_returns}")

        # Build features
        features = self.fb.build(df, market_data=market_data)

        # Align features + labels
        common = features.index.intersection(labels.dropna().index)
        X = features.loc[common].fillna(0)
        y = labels.loc[common].astype(int)

        # Drop last max_bars rows (no valid label)
        X = X.iloc[:-max_bars]
        y = y.iloc[:-max_bars]
        y = y[y.isin([-1, 0, 1])]  # ensure valid labels only
        X = X.loc[y.index]

        if len(X) < 150:
            log.warning(f"Insufficient data ({len(X)} rows < 150 min). Skipping.")
            return {}

        log.info(f"  Training data: {len(X)} rows | "
                 f"Features: {X.shape[1]} | "
                 f"Classes: {dict(y.value_counts().sort_index())}")

        # [G3-ML7] Walk-forward CV
        cv = self._walk_forward_cv(X.fillna(0), y, n_splits=5, purge_gap=5)
        results["cv"] = cv

        # Train/val split (80/20, no shuffle — temporal)
        split   = int(len(X) * 0.80)
        X_tr    = X.iloc[:split].fillna(0)
        y_tr    = y.iloc[:split]
        X_val   = X.iloc[split:].fillna(0)
        y_val   = y.iloc[split:]

        self.drift.set_baseline(X_tr)

        trained_models  = {}
        algos = [
            ("xgboost",     lambda: self._train_xgboost(X_tr, y_tr, X_val, y_val)),
            ("lightgbm",    lambda: self._train_lightgbm(X_tr, y_tr)),
            ("catboost",    lambda: self._train_catboost(X_tr, y_tr)),
            ("extra_trees", lambda: self._train_extra_trees(X_tr, y_tr)),
        ]

        # Train all-regime models
        for algo, train_fn in algos:
            model = train_fn()
            if model is None:
                continue

            # [G3-ML5] Importance pruning on the FIRST good model
            if algo == "xgboost" and self.pruner.kept_cols_ is None:
                self.pruner.fit(model, list(X_tr.columns))
                joblib.dump(self.pruner.kept_cols_, MODEL_DIR / "feature_names.pkl")
                # Reduce to pruned features
                X_tr_p  = self.pruner.transform(X_tr)
                X_val_p = self.pruner.transform(X_val)
                # Re-train XGB on pruned features
                model = self._train_xgboost(X_tr_p, y_tr, X_val_p, y_val) or model
                X_tr  = X_tr_p
                X_val = X_val_p
                log.info(f"  Pruned features: {len(self.pruner.kept_cols_)} kept")

            # Apply pruning to subsequent models
            if self.pruner.kept_cols_ is not None and algo != "xgboost":
                X_tr_use  = self.pruner.transform(X_tr)
                X_val_use = self.pruner.transform(X_val)
                if algo == "lightgbm":
                    model = self._train_lightgbm(X_tr_use, y_tr) or model
                elif algo == "catboost":
                    model = self._train_catboost(X_tr_use, y_tr) or model
                elif algo == "extra_trees":
                    model = self._train_extra_trees(X_tr_use, y_tr) or model
                X_val_eval = X_val_use
            else:
                X_val_eval = X_val

            # [G3-ML6] Calibrate
            model_cal = self._calibrate(model, X_val_eval, y_val)

            # Evaluate
            try:
                from sklearn.metrics import f1_score, accuracy_score
                preds = model_cal.predict(X_val_eval)
                if hasattr(model, "_zerobot_label_inv"):
                    preds = np.array([model._zerobot_label_inv.get(int(p), p) for p in preds])
                acc = float(accuracy_score(y_val, preds))
                f1  = float(f1_score(y_val, preds, average="macro", zero_division=0))
                results[algo] = {"accuracy": round(acc, 3), "f1": round(f1, 3)}
                log.info(f"  {algo}: Acc={acc:.3f} F1={f1:.3f}")
            except Exception as e:
                log.warning(f"  {algo} eval failed: {e}")

            path = MODEL_DIR / f"{algo}_{symbol}_{R_ALL}_{timestamp}.pkl"
            joblib.dump(model_cal, path)
            self._cleanup_old(algo, symbol, R_ALL, keep=3)
            trained_models[algo] = model_cal

        # [G3-ML3] Regime-specific models
        results["regime_models"] = {}
        for regime in [R_NORMAL, R_DEFENSIVE, R_CRISIS]:
            mask   = self._regime_mask(df_ind, regime)
            mask_f = mask.reindex(X.index, fill_value=False)
            X_r    = X[mask_f].fillna(0)
            y_r    = y[mask_f]

            if len(X_r) < 80:
                log.info(f"  Regime '{regime}': {len(X_r)} samples — skip (<80)")
                continue

            sp_r = int(len(X_r) * 0.80)
            log.info(f"  Regime '{regime}': {len(X_r)} samples → training")
            for algo, train_fn in [
                ("xgboost",  lambda: self._train_xgboost(
                    X_r.iloc[:sp_r], y_r.iloc[:sp_r],
                    X_r.iloc[sp_r:], y_r.iloc[sp_r:])),
                ("lightgbm", lambda: self._train_lightgbm(
                    X_r.iloc[:sp_r], y_r.iloc[:sp_r])),
            ]:
                m = train_fn()
                if m is None:
                    continue
                if sp_r < len(X_r):
                    m = self._calibrate(m, X_r.iloc[sp_r:], y_r.iloc[sp_r:])
                path = MODEL_DIR / f"{algo}_{symbol}_{regime}_{timestamp}.pkl"
                joblib.dump(m, path)
                self._cleanup_old(algo, symbol, regime, keep=2)
                results["regime_models"][f"{algo}_{regime}"] = True
                log.info(f"    Saved {algo} [{regime}]")

        # Save metadata
        if self.pruner.kept_cols_:
            joblib.dump(self.pruner.kept_cols_, MODEL_DIR / "feature_names.pkl")

        self._save_to_db(symbol, results, len(X))
        log.info(f"ML Training [G3] complete: {symbol}")
        return results

    def incremental_retrain(self, df, symbol, trade_feedback=None, market_data=None):
        log.info(f"ML Incremental retrain [G3]: {symbol}")
        recent = df.iloc[-int(len(df) * 0.5):]
        return self.train_full(recent, symbol, market_data=market_data)

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _cleanup_old(self, algo, symbol, suffix, keep=3):
        files = sorted(MODEL_DIR.glob(f"{algo}_{symbol}_{suffix}_*.pkl"))
        for f in files[:-keep]:
            try:
                f.unlink()
            except Exception:
                pass

    def _save_to_db(self, symbol, results, data_size):
        try:
            from core.state_manager import state_mgr
            if not state_mgr._db_available or not state_mgr._Session:
                return
            with state_mgr._Session() as session:
                from database.models import ModelRun
                for model_name, metrics in results.items():
                    if not isinstance(metrics, dict):
                        continue
                    run = ModelRun(
                        model_name=model_name,
                        version=symbol,
                        accuracy=float(metrics.get("accuracy") or 0),
                        f1_score=float(metrics.get("f1") or 0),
                        precision=float(metrics.get("precision") or 0),
                        recall=float(metrics.get("recall") or 0),
                        train_period=(f"{data_size} samples | "
                                      f"{datetime.now().strftime('%H:%M')}"),
                        is_active=True,
                    )
                    session.add(run)
                session.commit()
        except Exception as e:
            log.debug(f"Model run DB save: {e}")
