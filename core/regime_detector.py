# -*- coding: utf-8 -*-
"""
ZeroBot Pro — Volatility Regime Detector (Patch 5 NEW)
═══════════════════════════════════════════════════════
Detects market regime from India VIX + Nifty 50 trend.
Automatically adjusts strategy behavior:

  AGGRESSIVE  (VIX < 14)          → full position size, all strategies enabled
  NORMAL      (VIX 14–defensive)  → default behavior
  DEFENSIVE   (VIX defensive–halt)→ reduce position size 40%, skip options
  CRISIS      (VIX > halt)        → halt new trades, only exits allowed

Thresholds are read from config (settings.yaml):
  risk.vix_halt_threshold      (default 25.0)  → CRISIS threshold
  vix_defensive_threshold automatically = 80% of halt (e.g. 20.0 when halt=25.0)

Secondary regime factors:
  - Nifty 50 trend (above/below 50-day SMA)
  - 5-day rolling average to avoid VIX spike noise
  - Consecutive red days count
"""
from enum import Enum
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from core.logger import log


class MarketRegime(Enum):
    AGGRESSIVE = "AGGRESSIVE"   # VIX < 14, bull trend
    NORMAL     = "NORMAL"       # VIX 14–defensive, neutral
    DEFENSIVE  = "DEFENSIVE"    # VIX defensive–halt, cautious
    CRISIS     = "CRISIS"       # VIX > halt, halt trading


@dataclass
class RegimeState:
    regime:           MarketRegime = MarketRegime.NORMAL
    vix:              float = 15.0
    vix_5d_avg:       float = 15.0
    nifty_trend:      str   = "neutral"    # "bull", "bear", "neutral"
    size_multiplier:  float = 1.0          # position size multiplier
    options_allowed:  bool  = True
    new_trades_allowed: bool = True
    reason:           str   = ""
    updated_at:       Optional[datetime] = None


def _get_thresholds():
    """
    Read VIX thresholds from config.
    crisis_threshold   = cfg.vix_halt_threshold  (settings.yaml: risk.vix_halt_threshold)
    defensive_threshold = 80% of crisis_threshold

    Falls back to 25.0 / 20.0 if config is unavailable.
    """
    try:
        from core.config import cfg
        # vix_halt_threshold lives under cfg.risk (RiskConfig sub-model)
        risk = getattr(cfg, "risk", None)
        crisis = float(getattr(risk, "vix_halt_threshold", None) or 25.0)
        defensive = round(crisis * 0.80, 1)
        return crisis, defensive
    except Exception:
        return 25.0, 20.0


class RegimeDetector:
    """
    Singleton regime detector. Called each tick to update market regime.
    Engine reads regime before placing any order.

    VIX thresholds loaded from settings.yaml → risk.vix_halt_threshold.
    Default: CRISIS > 25.0 | DEFENSIVE > 20.0 (80% of 25)
    """

    def __init__(self):
        self.state = RegimeState()
        self._vix_history = []
        crisis, defensive = _get_thresholds()
        self._crisis_threshold    = crisis
        self._defensive_threshold = defensive
        log.info(
            f"RegimeDetector initialized — "
            f"CRISIS > {self._crisis_threshold} | "
            f"DEFENSIVE > {self._defensive_threshold} | "
            f"(from config: risk.vix_halt_threshold)"
        )

    def update(self, vix: float, nifty_price: float = 0, nifty_sma50: float = 0) -> RegimeState:
        """
        Update regime based on latest VIX and Nifty.
        Called from engine on each tick that includes VIX data.
        """
        # Reload thresholds so a live config reload takes effect immediately
        self._crisis_threshold, self._defensive_threshold = _get_thresholds()

        # Track 5-day rolling VIX average to smooth noise
        self._vix_history.append(vix)
        if len(self._vix_history) > 5 * 375:  # 5 trading days of minute ticks
            self._vix_history.pop(0)
        vix_avg = sum(self._vix_history[-50:]) / min(50, len(self._vix_history))

        # Nifty trend
        if nifty_price > 0 and nifty_sma50 > 0:
            nifty_trend = "bull" if nifty_price > nifty_sma50 * 1.01 else \
                          "bear" if nifty_price < nifty_sma50 * 0.99 else "neutral"
        else:
            nifty_trend = self.state.nifty_trend

        ct = self._crisis_threshold    # e.g. 25.0
        dt = self._defensive_threshold # e.g. 20.0

        # Determine regime
        if vix > ct:
            regime          = MarketRegime.CRISIS
            size_mult       = 0.0
            options_allowed = False
            new_trades      = False
            reason          = f"VIX {vix:.1f} > {ct} → CRISIS: halt new trades"
        elif vix > dt:
            regime          = MarketRegime.DEFENSIVE
            size_mult       = 0.5
            options_allowed = False
            new_trades      = True
            reason          = f"VIX {vix:.1f} {dt}-{ct} → DEFENSIVE: 50% size, no options"
        elif vix < 14 and nifty_trend == "bull":
            regime          = MarketRegime.AGGRESSIVE
            size_mult       = 1.25
            options_allowed = True
            new_trades      = True
            reason          = f"VIX {vix:.1f} < 14, Nifty BULL → AGGRESSIVE: 125% size"
        else:
            regime          = MarketRegime.NORMAL
            size_mult       = 1.0
            options_allowed = True
            new_trades      = True
            reason          = f"VIX {vix:.1f} → NORMAL"

        # Log regime change
        if regime != self.state.regime:
            log.warning(
                f"📊 REGIME CHANGE: {self.state.regime.value} → {regime.value} | {reason}"
            )

        self.state = RegimeState(
            regime=regime, vix=vix, vix_5d_avg=round(vix_avg, 2),
            nifty_trend=nifty_trend, size_multiplier=size_mult,
            options_allowed=options_allowed, new_trades_allowed=new_trades,
            reason=reason, updated_at=datetime.now()
        )
        return self.state

    def get_size_multiplier(self) -> float:
        """Get current position size multiplier (0.0 in CRISIS → 1.25 in AGGRESSIVE)."""
        return self.state.size_multiplier

    def is_trading_allowed(self, is_option: bool = False) -> tuple:
        """Returns (allowed: bool, reason: str)."""
        if not self.state.new_trades_allowed:
            return False, f"Regime {self.state.regime.value}: {self.state.reason}"
        if is_option and not self.state.options_allowed:
            return False, f"Options blocked in {self.state.regime.value} regime (VIX {self.state.vix:.1f})"
        return True, "OK"


# Module-level singleton
regime_detector = RegimeDetector()
