# -*- coding: utf-8 -*-
"""
ZeroBot Pro — Kelly Criterion Position Sizer (Patch 5 NEW)
═══════════════════════════════════════════════════════════
Optimal position sizing using Kelly Criterion + fractional Kelly.

Kelly formula: f* = (bp - q) / b
  b = net odds (R:R ratio)
  p = probability of win (ML confidence or historical win rate)
  q = probability of loss (1 - p)

We use FRACTIONAL Kelly (25% = quarter-Kelly) to:
  1. Protect against edge estimation errors
  2. Reduce drawdown volatility
  3. Stay within capital concentration limits

Example:
  Capital: ₹55,000  |  Win rate: 65%  |  R:R: 2:1
  Kelly f* = (2×0.65 - 0.35) / 2 = 0.475  (47.5% of capital!)
  Quarter-Kelly = 0.475 × 0.25 = 0.119 = 11.9% = ₹6,500

This is then capped by risk engine limits (max_single_stock_pct etc.)
"""
from dataclasses import dataclass
from typing import Optional
from core.logger import log


@dataclass
class SizeResult:
    qty:           int
    position_inr:  float
    kelly_f:       float     # raw Kelly fraction
    frac_kelly_f:  float     # fractional Kelly applied
    basis:         str       # explanation string


class KellySizer:
    """
    Compute optimal position size using fractional Kelly.
    """

    def __init__(self, fraction: float = 0.25, max_pct: float = 0.20):
        """
        fraction: Kelly fraction (0.25 = quarter-Kelly, conservative)
        max_pct:  hard cap as % of capital regardless of Kelly
        """
        self.fraction = fraction
        self.max_pct  = max_pct

    def compute(
        self,
        capital:               float,
        cmp:                   float,
        confidence:            float,          # ML confidence 0-100
        rr_ratio:              float = 2.0,    # reward:risk ratio
        win_rate:              Optional[float] = None,
        regime_mult:           float = 1.0,    # from RegimeDetector
        min_qty:               int   = 1,
        expected_return_score: float = 0.0,   # [G3-ML] ATR-norm E[R] from predictor
    ) -> SizeResult:
        """
        Compute Kelly-optimal position size.

        [G3-ML] expected_return_score integration:
        When the G3 predictor provides a non-zero expected_return_score,
        it is used to derive a more precise rr_ratio:
            implied_rr = max(0.5, |E[R]| / atr_risk_per_share)
        This lets Kelly sizing respond to actual model-implied edge rather
        than a fixed 2:1 assumption.
        """
        if cmp <= 0 or capital <= 0:
            return SizeResult(qty=1, position_inr=cmp, kelly_f=0, frac_kelly_f=0, basis="invalid inputs")

        # [G3-ML] Derive rr_ratio from expected_return_score if available
        # expected_return_score is %-expressed (e.g. 0.004 = 0.4% ATR-norm return)
        er_note = ""
        if expected_return_score != 0.0:
            # Map E[R] to an implied R:R relative to default sl (1.0 ATR ≈ 0.5×E[R])
            implied_rr = abs(expected_return_score) * 200   # 0.004 → 0.8; clamp below
            implied_rr = max(0.5, min(5.0, implied_rr))
            if implied_rr > 0.5:
                rr_ratio = 0.5 * rr_ratio + 0.5 * implied_rr  # blend with caller's rr
                er_note  = f" E[R]={expected_return_score:.4f}→rr={rr_ratio:.2f}"

        # Probability of win
        p = max(0.45, min(0.85, confidence / 100.0))
        if win_rate and 0.3 < win_rate < 0.9:
            p = 0.6 * p + 0.4 * win_rate

        q = 1.0 - p
        b = max(0.5, rr_ratio)

        kelly_f  = max(0.0, (b * p - q) / b)
        frac_f   = kelly_f * self.fraction * regime_mult
        frac_f   = min(frac_f, self.max_pct)

        position_inr = capital * frac_f
        qty = max(min_qty, int(position_inr / cmp))

        basis = (
            f"Kelly={kelly_f:.3f} × frac={self.fraction} × regime={regime_mult:.1f} "
            f"→ {frac_f:.3f} | p={p:.2f} q={q:.2f} b={b:.1f}{er_note} | "
            f"₹{position_inr:.0f} / ₹{cmp:.1f} = {qty}qty"
        )
        log.debug(f"KellySizer: {basis}")

        return SizeResult(
            qty=qty,
            position_inr=round(position_inr, 2),
            kelly_f=round(kelly_f, 4),
            frac_kelly_f=round(frac_f, 4),
            basis=basis,
        )

    def update_fraction(self, consecutive_losses: int):
        """
        G1-FIX-F5: Anti-martingale with 5% hard floor.
        BUG: Calling update_fraction(0) reset fraction to 0.25 immediately after
        a drawdown — blowing up position size when the streak resets to 0.
        FIX: Only allow fraction to DECREASE, and enforce 5% floor always.
        0 losses → 0.25  |  2 → 0.15  |  3 → 0.10  |  5+ → 0.05
        """
        if consecutive_losses >= 5:
            target = 0.05
        elif consecutive_losses >= 3:
            target = 0.10
        elif consecutive_losses >= 2:
            target = 0.15
        else:
            target = 0.25
        # G1-FIX-F5: When losses=0 (streak reset), don't jump back to full Kelly.
        # Allow gradual recovery by capping at current level. Always floor at 5%.
        if consecutive_losses == 0:
            self.fraction = min(self.fraction + 0.05, target)  # recover slowly
        else:
            self.fraction = target
        self.fraction = max(self.fraction, 0.05)   # hard floor — never below 5%
        if consecutive_losses > 0:
            log.info(f"[G1-F5] KellySizer: fraction={self.fraction:.0%} (streak={consecutive_losses} losses, floor=5%)")


# Module-level sizer
kelly_sizer = KellySizer(fraction=0.25, max_pct=0.20)
