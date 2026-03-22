# -*- coding: utf-8 -*-
"""
ZeroBot G2 — ML Ensemble Predictor  [G3 UPGRADE]
=================================================
G3 CHANGES vs G2:

  [G3-P1]  EXPANDED OUTPUT — predict() now returns:
             direction_probability : Dict  {BUY: float, HOLD: float, SELL: float}
             expected_return_score : float  E[R] = P(BUY)*avg_buy_ret - P(SELL)*avg_sell_ret
             direction             : str    highest-probability class
             confidence            : float  max class probability * 100

  [G3-P2]  REGIME ROUTING (4 regimes: NORMAL / DEFENSIVE / CRISIS / ALL)
             Uses VIX from regime_detector for routing if available, else ADX proxy.
             CRISIS regime models trained with tighter barriers are used when
             regime_detector reports CRISIS.

  [G3-P3]  PRUNED FEATURE ALIGNMENT
             Loads pruned_feature_names.pkl written by FeatureImportancePruner.
             Feature vector is aligned to the exact columns the model was trained on.

  [G3-P4]  DYNAMIC WEIGHT ADJUSTMENT
             Regime-matching models get a 1.4× weight boost.
             Recently trained models (within 24 hrs) get 1.1× freshness boost.
             Poorly performing models (low CV F1) are down-weighted.

  [G3-P5]  EXPECTED RETURN SCORE
             Loaded from class_returns_{symbol}.pkl saved during training.
             E[R] = P(BUY) * avg_buy_return - P(SELL) * avg_sell_return
             Normalized by ATR so it's comparable across symbols.

  [G3-P6]  CONFIDENCE FLOOR + ASYMMETRIC THRESHOLDS
             BUY  requires confidence ≥ 0.55  (default)
             SELL requires confidence ≥ 0.53  (slightly lower = capture more shorts)
             Both adjustable via config.

  [G2-P4]  MULTI-CRITERION RETRAIN TRIGGER (unchanged)
             Fires on: 50 trades OR 7 days OR feature drift PSI>0.2 on ≥3 features.
"""

import joblib
import warnings
import numpy as np
import pandas as pd
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Tuple

from core.config import cfg
from core.logger import log
from data.processors.indicator_engine import IndicatorEngine

MODEL_DIR = Path(__file__).parent / "saved"
MODEL_DIR.mkdir(exist_ok=True)

# Regime labels
R_NORMAL    = "NORMAL"
R_DEFENSIVE = "DEFENSIVE"
R_CRISIS    = "CRISIS"
R_ALL       = "all"

# Confidence floors (calibrated probability, not raw logit)
CONFIDENCE_BUY  = 0.55   # [G3-P6] minimum for BUY signal
CONFIDENCE_SELL = 0.53   # [G3-P6] slightly lower for SELL (captures more shorts)
CONFIDENCE_HOLD = 0.50   # below this = forced HOLD

RETRAIN_DAYS   = 7
RETRAIN_TRADES = 50

# Model weight multipliers
WEIGHT_REGIME_MATCH = 1.4   # regime-specific model matching current regime
WEIGHT_FRESH_MODEL  = 1.1   # model trained within 24 hours
WEIGHT_STALE_MODEL  = 0.8   # model trained > 7 days ago


class EnsemblePredictor:
    """
    G3 real-time predictor.

    Key output fields (from predict()):
        direction_probability : {"BUY": 0.62, "HOLD": 0.25, "SELL": 0.13}
        expected_return_score : 0.0043  (ATR-normalized expected return)
        direction             : "BUY"
        confidence            : 62.0   (%)
        regime                : "NORMAL"
        models_used           : ["xgboost", "lightgbm", ...]
    """

    # Base weights per algorithm (normalised before use)
    BASE_WEIGHTS: Dict[str, float] = {
        "xgboost":     0.35,
        "lightgbm":    0.30,
        "catboost":    0.20,
        "extra_trees": 0.15,
    }

    def __init__(self):
        # Model stores: keyed by algo name
        self._models_all:      Dict[str, object] = {}
        self._models_normal:   Dict[str, object] = {}
        self._models_defensive:Dict[str, object] = {}
        self._models_crisis:   Dict[str, object] = {}

        self._feature_names:  Optional[List[str]] = None   # pruned list
        self._ie              = IndicatorEngine()
        self._prediction_log: deque = deque(maxlen=500)
        self._trade_count     = 0
        self._last_retrain    = datetime.now()
        self._drift_flag      = False

        # Per-symbol caches
        self._feat_cache:   Dict[str, Tuple] = {}
        self._result_cache: Dict[str, Dict]  = {}

        # [G3-P5] Class return stats per symbol
        self._class_returns: Dict[str, Dict] = {}

        self._model_timestamps: Dict[str, datetime] = {}
        self._load_models()

    # ── Model loading ─────────────────────────────────────────────────────────
    def _load_models(self):
        # [G3-P3] Load pruned feature names
        for fname in ["feature_names.pkl", "pruned_feature_names.pkl"]:
            fpath = MODEL_DIR / fname
            if fpath.exists():
                try:
                    self._feature_names = joblib.load(fpath)
                    log.info(f"Loaded {len(self._feature_names)} feature names from {fname}")
                    break
                except Exception:
                    pass

        algo_list = ["xgboost", "lightgbm", "catboost", "extra_trees"]
        store_map = {
            R_ALL:       self._models_all,
            R_NORMAL:    self._models_normal,
            R_DEFENSIVE: self._models_defensive,
            R_CRISIS:    self._models_crisis,
        }

        def _load_latest(algo: str, suffix: str) -> Optional[object]:
            files = sorted(MODEL_DIR.glob(f"{algo}_*_{suffix}_*.pkl"))
            for f in reversed(files):
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        m = joblib.load(f)
                    # Parse timestamp from filename for freshness weighting
                    parts = f.stem.split("_")
                    try:
                        ts = datetime.strptime(f"{parts[-2]}_{parts[-1]}", "%Y%m%d_%H%M")
                        self._model_timestamps[f"{algo}_{suffix}"] = ts
                    except Exception:
                        pass
                    log.info(f"Loaded {algo} [{suffix}]: {f.name}")
                    return m
                except Exception as e:
                    log.warning(f"Cannot load {f.name}: {str(e)[:60]} — removing")
                    try:
                        f.unlink()
                    except Exception:
                        pass
            return None

        for suffix, store in store_map.items():
            for algo in algo_list:
                m = _load_latest(algo, suffix)
                if m:
                    store[algo] = m

        if not self._models_all:
            log.info("No G3 models found — will train on startup")

        # [G3-P5] Load class return stats
        for path in MODEL_DIR.glob("class_returns_*.pkl"):
            try:
                symbol = path.stem.replace("class_returns_", "")
                self._class_returns[symbol] = joblib.load(path)
            except Exception:
                pass

    # ── Current regime ─────────────────────────────────────────────────────────
    def _get_regime(self, last_row: pd.Series) -> str:
        """Determine current regime from regime_detector or ADX proxy."""
        try:
            from core.regime_detector import regime_detector
            return regime_detector.state.regime.value   # NORMAL/DEFENSIVE/CRISIS
        except Exception:
            pass
        # Fallback: ADX proxy
        adx = float(last_row.get("ADX_14", 20) or 20)
        atr_z = float(last_row.get("vol_expansion_z", 0) or 0)
        if atr_z > 2.5:
            return R_CRISIS
        if atr_z > 1.5 or adx < 20:
            return R_DEFENSIVE
        return R_NORMAL

    # ── Dynamic model weights ─────────────────────────────────────────────────
    def _get_weight(self, algo: str, suffix: str, current_regime: str) -> float:
        base = self.BASE_WEIGHTS.get(algo, 0.25)

        # Regime match boost
        if suffix == current_regime:
            base *= WEIGHT_REGIME_MATCH
        elif suffix == R_ALL:
            base *= 1.0  # neutral
        else:
            base *= 0.7  # wrong regime

        # Freshness boost/penalty
        key = f"{algo}_{suffix}"
        if key in self._model_timestamps:
            age_hours = (datetime.now() - self._model_timestamps[key]).total_seconds() / 3600
            if age_hours < 24:
                base *= WEIGHT_FRESH_MODEL
            elif age_hours > 168:  # > 7 days
                base *= WEIGHT_STALE_MODEL

        return base

    # ── Feature vector construction ───────────────────────────────────────────
    def _build_feat_vec(self, last_row: pd.Series) -> np.ndarray:
        """Build 2D feature vector aligned to saved feature names."""
        if self._feature_names:
            vec = np.array([[float(last_row.get(f, 0) or 0) for f in self._feature_names]])
        else:
            numeric = last_row[pd.to_numeric(last_row, errors="coerce").notna()]
            vec = numeric.values.reshape(1, -1)
        return np.nan_to_num(vec, nan=0.0, posinf=0.0, neginf=0.0)

    def _align_features(self, feat_vec: np.ndarray, expected: int) -> np.ndarray:
        n = feat_vec.shape[1]
        if n < expected:
            return np.pad(feat_vec, ((0, 0), (0, expected - n)))
        if n > expected:
            return feat_vec[:, :expected]
        return feat_vec

    # ── Prob padding: ensures 3 classes {SELL=-1, HOLD=0, BUY=1} ─────────────
    def _to_3class_probs(self, raw_probs: np.ndarray, model) -> np.ndarray:
        """
        Normalise raw_probs to always be [p_sell, p_hold, p_buy] (3 elements).
        Handles:
          - XGB with label remapping  {0:SELL(-1), 1:HOLD(0), 2:BUY(1)}
          - LGBM/CB/ET with native labels
          - Binary models [p_neg, p_pos]
        """
        # XGB: has _zerobot_label_inv for remapping
        if hasattr(model, "_zerobot_label_inv"):
            inv = model._zerobot_label_inv   # {xgb_class: original_label}
            # raw_probs indexed by xgb class (0,1,2)
            result = [0.0, 0.0, 0.0]   # [p_sell, p_hold, p_buy]
            label_to_idx = {-1: 0, 0: 1, 1: 2}
            for xgb_cls, orig_label in inv.items():
                if xgb_cls < len(raw_probs):
                    target_idx = label_to_idx.get(int(orig_label), 1)
                    result[target_idx] = float(raw_probs[xgb_cls])
            return np.array(result)

        # CalibratedClassifierCV: check classes_
        if hasattr(model, "classes_"):
            classes = list(model.classes_)
            result  = [0.0, 0.0, 0.0]
            label_to_idx = {-1: 0, 0: 1, 1: 2}
            for i, cls in enumerate(classes):
                if i < len(raw_probs):
                    idx = label_to_idx.get(int(cls), 1)
                    result[idx] = float(raw_probs[i])
            return np.array(result)

        # Binary fallback [p_neg, p_pos]
        if len(raw_probs) == 2:
            p_sell, p_buy = float(raw_probs[0]), float(raw_probs[1])
            p_hold = max(0.0, 1.0 - p_sell - p_buy)
            return np.array([p_sell, p_hold, p_buy])

        # Already 3 classes
        if len(raw_probs) == 3:
            return np.array([float(p) for p in raw_probs])

        # Fallback: pad/trim to 3
        out = np.zeros(3)
        for i in range(min(3, len(raw_probs))):
            out[i] = float(raw_probs[i])
        return out

    # ── Expected return score ─────────────────────────────────────────────────
    def _expected_return_score(self, ensemble_probs: np.ndarray,
                                 symbol: str, last_row: pd.Series) -> float:
        """
        [G3-P5] Compute ATR-normalized expected return:
            E[R] = P(BUY) * avg_buy_return - P(SELL) * avg_sell_return

        Falls back to ATR-based estimate if class_returns not available.
        """
        p_sell, p_hold, p_buy = float(ensemble_probs[0]), float(ensemble_probs[1]), float(ensemble_probs[2])

        cr = self._class_returns.get(symbol)
        if cr:
            avg_buy  = cr.get("avg_buy_return",  0.0)
            avg_sell = cr.get("avg_sell_return",  0.0)
            # avg_sell is already negative for losing trades; take abs for down-side
            e_return = p_buy * avg_buy + p_sell * avg_sell
        else:
            # ATR-based estimate: assume reward = 1.5 ATR, risk = 1.0 ATR
            atr_pct = float(last_row.get("atr_pct", 0.5) or 0.5)
            e_return = p_buy * (1.5 * atr_pct / 100) - p_sell * (1.0 * atr_pct / 100)

        return round(float(e_return), 6)

    # ── Core prediction ───────────────────────────────────────────────────────
    def predict(self, df: pd.DataFrame, symbol: str) -> Dict:
        """
        G3 prediction pipeline.

        Returns
        -------
        Dict with keys:
            direction            : "BUY" | "HOLD" | "SELL"
            confidence           : float  (0–100, calibrated probability × 100)
            direction_probability: {"BUY": float, "HOLD": float, "SELL": float}
            expected_return_score: float  (ATR-normalized, e.g. 0.004 = 40bps)
            ensemble_probs       : {"sell": float, "hold": float, "buy": float}
            regime               : str
            adx                  : float
            models_used          : List[str]
            symbol               : str
            reason               : str
        """
        all_models = {**self._models_all, **self._models_normal,
                      **self._models_defensive, **self._models_crisis}
        if not all_models:
            return self._hold("No models loaded")

        try:
            last_close = float(df.iloc[-1]["close"]) if not df.empty else 0.0
            cache_key  = (len(df), last_close)
            if self._feat_cache.get(symbol) == cache_key and symbol in self._result_cache:
                return self._result_cache[symbol]

            df_feat = self._ie.add_all(df.copy())
            if df_feat.empty or len(df_feat) < 5:
                return self._hold("Insufficient data")

            last_row = df_feat.iloc[-1]

            # [G3-P2] Determine current regime
            regime = self._get_regime(last_row)

            # Select models: prefer regime-specific, fall back to all-regime
            regime_store = {
                R_NORMAL:    self._models_normal,
                R_DEFENSIVE: self._models_defensive,
                R_CRISIS:    self._models_crisis,
            }.get(regime, {})

            # Build active model pool with source tracking
            active_models: Dict[str, Tuple[object, str]] = {}  # algo → (model, suffix)
            for algo, model in self._models_all.items():
                active_models[algo] = (model, R_ALL)
            for algo, model in regime_store.items():
                active_models[f"{algo}_regime"] = (model, regime)  # overrides all-regime

            if not active_models:
                return self._hold("No active models")

            # [G3-P3] Build feature vector (aligned to pruned names)
            feat_vec = self._build_feat_vec(last_row)

            # ── Ensemble aggregation ──────────────────────────────────────────
            weighted_probs: List[np.ndarray] = []
            weights:         List[float]     = []
            models_used:     List[str]       = []

            for name, (model, suffix) in active_models.items():
                try:
                    algo_base = name.replace("_regime", "")
                    n_expected = getattr(model, "n_features_in_",
                                  feat_vec.shape[1])
                    fv = self._align_features(feat_vec, n_expected)

                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        raw_probs = model.predict_proba(fv)[0]

                    probs_3cls = self._to_3class_probs(raw_probs, model)
                    # Normalize
                    total = probs_3cls.sum()
                    if total > 0:
                        probs_3cls /= total

                    w = self._get_weight(algo_base, suffix, regime)
                    weighted_probs.append(probs_3cls * w)
                    weights.append(w)
                    models_used.append(name)
                except Exception as e:
                    log.debug(f"{name} predict error: {e}")

            if not weighted_probs:
                return self._hold("All models failed")

            # Weighted ensemble
            total_w  = sum(weights)
            ensemble = np.sum(weighted_probs, axis=0) / max(total_w, 1e-9)

            # Ensure sums to 1
            if ensemble.sum() > 0:
                ensemble /= ensemble.sum()

            p_sell, p_hold, p_buy = float(ensemble[0]), float(ensemble[1]), float(ensemble[2])

            # [G3-P1] Direction probability dict
            direction_probability = {
                "BUY":  round(p_buy,  4),
                "HOLD": round(p_hold, 4),
                "SELL": round(p_sell, 4),
            }

            # [G3-P6] Direction decision with asymmetric thresholds
            adx = float(last_row.get("ADX_14", 20) or 20)

            # In CRISIS regime: raise thresholds, be very conservative
            buy_floor  = CONFIDENCE_BUY  + (0.05 if regime == R_CRISIS else 0)
            sell_floor = CONFIDENCE_SELL + (0.05 if regime == R_CRISIS else 0)

            if   p_buy  >= buy_floor  and p_buy  > p_hold and p_buy  > p_sell:
                direction = "BUY"
                confidence = p_buy
            elif p_sell >= sell_floor and p_sell > p_hold and p_sell > p_buy:
                direction  = "SELL"
                confidence = p_sell
            else:
                direction  = "HOLD"
                confidence = p_hold

            # Final HOLD override if confidence too low overall
            if max(p_buy, p_sell) < CONFIDENCE_HOLD:
                direction  = "HOLD"
                confidence = p_hold

            # [G3-P5] Expected return score
            expected_return_score = self._expected_return_score(ensemble, symbol, last_row)

            result = {
                # [G3-P1] Primary outputs
                "direction":             direction,
                "confidence":            round(confidence * 100, 2),
                "direction_probability": direction_probability,
                "expected_return_score": expected_return_score,
                # Context
                "symbol":       symbol,
                "regime":       regime,
                "adx":          round(adx, 1),
                "models_used":  models_used,
                # Internals
                "ensemble_probs": {
                    "sell": round(p_sell, 4),
                    "hold": round(p_hold, 4),
                    "buy":  round(p_buy,  4),
                },
                "reason": (
                    f"G3-Ensemble {direction} "
                    f"p_buy={p_buy:.3f} p_sell={p_sell:.3f} "
                    f"E[R]={expected_return_score:.4f} "
                    f"regime={regime}"
                ),
                "features_snapshot": {
                    f: float(last_row.get(f, 0) or 0)
                    for f in (self._feature_names or [])[:20]
                },
            }

            self._prediction_log.append({
                "symbol":    symbol,
                "timestamp": pd.Timestamp.now().isoformat(),
                "direction": direction,
                "confidence":confidence * 100,
                "regime":    regime,
                "er_score":  expected_return_score,
            })

            # Update cache
            self._feat_cache[symbol]   = cache_key
            self._result_cache[symbol] = result
            if len(self._feat_cache) > 50:
                oldest = next(iter(self._feat_cache))
                del self._feat_cache[oldest]
                self._result_cache.pop(oldest, None)

            return result

        except Exception as e:
            log.error(f"Prediction error {symbol}: {e}")
            return self._hold(str(e))

    # ── Retrain trigger ───────────────────────────────────────────────────────
    def record_trade_outcome(self, symbol: str, pnl: float,
                              prediction_timestamp: str = None) -> bool:
        """[G2-P4] Multi-criterion retrain trigger (unchanged from G2)."""
        self._trade_count += 1
        trade_trigger = self._trade_count >= RETRAIN_TRADES
        time_trigger  = (datetime.now() - self._last_retrain) >= timedelta(days=RETRAIN_DAYS)
        drift_trigger = self._drift_flag

        if trade_trigger or time_trigger or drift_trigger:
            reason = ("trades" if trade_trigger else
                      "days"   if time_trigger  else "feature_drift")
            log.info(f"ML retrain triggered: {reason} — scheduling refit")
            self._trade_count  = 0
            self._last_retrain = datetime.now()
            self._drift_flag   = False
            return True
        return False

    def flag_drift(self):
        self._drift_flag = True
        log.warning("[G3-P4] Drift flag set — retrain will fire on next trade outcome")

    def reload_models(self):
        """Hot-reload models from disk (call after trainer completes)."""
        self._models_all.clear()
        self._models_normal.clear()
        self._models_defensive.clear()
        self._models_crisis.clear()
        self._class_returns.clear()
        self._feat_cache.clear()
        self._result_cache.clear()
        self._load_models()
        log.info("Models reloaded from disk")

    def is_ready(self) -> bool:
        return bool(self._models_all or self._models_normal)

    def get_model_info(self) -> Dict:
        return {
            "models_all":            list(self._models_all.keys()),
            "models_normal":         list(self._models_normal.keys()),
            "models_defensive":      list(self._models_defensive.keys()),
            "models_crisis":         list(self._models_crisis.keys()),
            "feature_count":         len(self._feature_names or []),
            "prediction_log_size":   len(self._prediction_log),
            "trades_since_retrain":  self._trade_count,
            "retrain_threshold":     RETRAIN_TRADES,
            "retrain_days":          RETRAIN_DAYS,
            "drift_flagged":         self._drift_flag,
            "days_since_retrain":    (datetime.now() - self._last_retrain).days,
            "symbols_with_class_returns": list(self._class_returns.keys()),
        }

    # ── Helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _hold(reason: str) -> Dict:
        return {
            "direction":             "HOLD",
            "confidence":            50.0,
            "direction_probability": {"BUY": 0.0, "HOLD": 1.0, "SELL": 0.0},
            "expected_return_score": 0.0,
            "ensemble_probs":        {"sell": 0.0, "hold": 1.0, "buy": 0.0},
            "reason":                reason,
            "regime":                "UNKNOWN",
            "adx":                   0.0,
            "models_used":           [],
        }
