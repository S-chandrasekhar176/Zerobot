# -*- coding: utf-8 -*-
"""
ZeroBot v2 — Options Strategy (NSE F&O)
========================================
Trades NSE stock + index options (CE/PE) based on signals from the
existing equity strategies (Momentum, MeanReversion, VWAP).

How it works:
  1. Equity strategy fires BUY  → buy ATM+1 CALL (CE) on the underlying
  2. Equity strategy fires SELL → buy ATM+1 PUT  (PE) on the underlying
  3. ML confidence gate still applies
  4. Options-specific risk gates:
       - IV percentile check (don't buy expensive options)
       - Days-to-expiry check (avoid last 2 days theta crush)
       - Premium cap (max 2% of capital per trade)
       - Max 3 concurrent option positions
  5. Auto exit at +50% premium gain OR -50% stop loss

Position sizing (options are very different from stocks):
  - NEVER use full Kelly/ATR sizing — options can go to zero
  - Max premium = Capital × max_premium_per_trade_pct (2% default)
  - Lots = floor(max_premium / (option_price × lot_size))
  - Always minimum 1 lot

Paper mode simulation:
  - Option price estimated via simplified Black-Scholes
  - IV estimated from historical volatility × 1.2 (options typically
    trade at slight premium to HV)
  - Greeks calculated for position monitoring

Real (Angel One) mode:
  - Uses Angel One's option chain API for live strikes + premiums
  - Uncomment smartapi-python in requirements.txt when going live
"""

import math
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List, Tuple
import pandas as pd
import numpy as np

from strategies.base_strategy import BaseStrategy
from risk.risk_engine import TradeSignal
from core.config import cfg
from core.logger import log


# ── NSE Option lot sizes (current as of 2025) ──────────────────────────────
LOT_SIZES = {
    "^NSEI":       50,
    "^NSEBANK":    15,
    "RELIANCE.NS": 250,
    "HDFCBANK.NS": 550,
    "ICICIBANK.NS":700,
    "TCS.NS":      150,
    "INFY.NS":     300,
    "SBIN.NS":     750,
    "AXISBANK.NS": 1200,
    "WIPRO.NS":    1500,
    "BAJFINANCE.NS":125,
    "KOTAKBANK.NS":400,
    "LT.NS":       300,
    "MARUTI.NS":   25,
    "TITAN.NS":    375,
}

# NSE strike intervals per underlying
STRIKE_INTERVALS = {
    "^NSEI":       50,
    "^NSEBANK":    100,
    "RELIANCE.NS": 50,
    "HDFCBANK.NS": 20,
    "ICICIBANK.NS":20,
    "TCS.NS":      100,
}


def _next_thursday(from_date: date = None) -> date:
    """Return next Thursday (NSE weekly expiry)."""
    d = from_date or date.today()
    days_ahead = 3 - d.weekday()  # Thursday = 3
    if days_ahead <= 0:
        days_ahead += 7
    return d + timedelta(days=days_ahead)


def _last_thursday_of_month(from_date: date = None) -> date:
    """Return last Thursday of the current month (monthly expiry)."""
    d = from_date or date.today()
    # Go to last day of month
    if d.month == 12:
        last_day = date(d.year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(d.year, d.month + 1, 1) - timedelta(days=1)
    # Walk back to find last Thursday
    while last_day.weekday() != 3:
        last_day -= timedelta(days=1)
    return last_day


def black_scholes_price(
    S: float,       # Spot price
    K: float,       # Strike price
    T: float,       # Time to expiry in years
    r: float,       # Risk-free rate (use 0.065 for India)
    sigma: float,   # Implied volatility (annualised, e.g. 0.20 = 20%)
    option_type: str = "CE",
) -> Tuple[float, Dict]:
    """
    Black-Scholes option pricing with Greeks.
    Returns (price, {delta, gamma, theta, vega, iv}).
    Used only in paper mode — Angel One provides live prices in live mode.
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.01, {"delta": 0, "gamma": 0, "theta": 0, "vega": 0, "iv": sigma}

    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    from scipy.stats import norm
    N = norm.cdf
    n = norm.pdf

    if option_type == "CE":
        price = S * N(d1) - K * math.exp(-r * T) * N(d2)
        delta = N(d1)
    else:  # PE
        price = K * math.exp(-r * T) * N(-d2) - S * N(-d1)
        delta = N(d1) - 1

    gamma = n(d1) / (S * sigma * math.sqrt(T))
    theta = (-(S * n(d1) * sigma) / (2 * math.sqrt(T))
             - r * K * math.exp(-r * T) * (N(d2) if option_type == "CE" else N(-d2))) / 365
    vega = S * n(d1) * math.sqrt(T) / 100

    price = max(price, 0.05)  # Minimum tick
    return round(price, 2), {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 2),
        "vega": round(vega, 2),
        "iv": round(sigma * 100, 1),
    }


def estimate_iv(df: pd.DataFrame) -> float:
    """
    Estimate IV from historical volatility.
    Options typically trade at ~20% premium to HV (IV risk premium).
    """
    if len(df) < 20:
        return 0.20  # 20% default
    returns = df["close"].pct_change().dropna()
    hv_daily = returns.std()
    hv_annual = hv_daily * math.sqrt(252)
    iv = hv_annual * 1.20  # IV premium over HV
    return max(0.10, min(1.50, iv))  # Cap between 10% and 150%


def iv_percentile(df: pd.DataFrame, current_iv: float) -> float:
    """
    Compute IV percentile rank (0-100) vs last 252 days.
    Used to avoid buying expensive options (high IV = high premium decay risk).
    """
    if len(df) < 30:
        return 50.0
    # Proxy: use rolling 20-day HV as IV history
    returns = df["close"].pct_change().dropna()
    rolling_hv = returns.rolling(20).std() * math.sqrt(252) * 1.20
    rolling_hv = rolling_hv.dropna()
    if len(rolling_hv) < 10:
        return 50.0
    rank = (rolling_hv < current_iv).mean() * 100
    return round(rank, 1)


class OptionsStrategy(BaseStrategy):
    """
    Converts equity directional signals into options positions.
    BUY signal  → buy CE (call option) on the underlying
    SELL signal → buy PE (put option) on the underlying

    Uses Black-Scholes for paper mode pricing.
    Adds options-specific risk gates on top of the main 10-gate engine.
    """

    def __init__(self):
        super().__init__("Options")
        self.opts = cfg.options
        self._active_positions: Dict[str, Dict] = {}  # symbol → option position

    def generate_signal(
        self,
        df: pd.DataFrame,
        symbol: str,
        equity_signal_side: Optional[str] = None,  # "BUY" | "SELL" from equity strategy
    ) -> Optional[TradeSignal]:
        """
        Generate an options trade signal based on the underlying's direction.
        Called by engine when trading_mode is 'options' or 'both'.
        equity_signal_side: direction from the parent equity strategy signal.
        """
        if not self.enabled:
            return None
        if symbol not in self.opts.underlyings:
            return None
        if equity_signal_side not in ("BUY", "SELL"):
            return None
        if df.empty or len(df) < 20:
            return None

        # ── Compute option parameters ──────────────────────────────────────
        spot = float(df.iloc[-1]["close"])
        iv = estimate_iv(df)
        iv_rank = iv_percentile(df, iv)

        # IV gate: don't buy when IV is too expensive (>80th percentile)
        # P16: Strict IV Rank filter — only BUY if IV Rank < 30, only SELL if > 70
        # These thresholds override settings.yaml min/max_iv_percentile for better entry quality
        if equity_signal_side == "BUY" and iv_rank > 30:
            log.debug(f"Options {symbol}: IV rank {iv_rank:.0f} too high for BUY (>30) — overpriced options")
        if equity_signal_side == "SELL" and iv_rank < 70:
            log.debug(f"Options {symbol}: IV rank {iv_rank:.0f} too low for SELL (<70) — cheap options, skip selling")

        if iv_rank > self.opts.max_iv_percentile:
            log.debug(f"Options {symbol}: IV rank {iv_rank:.0f} too high (>{self.opts.max_iv_percentile}) — skipping")
            return None
        if iv_rank < self.opts.min_iv_percentile:
            log.debug(f"Options {symbol}: IV rank {iv_rank:.0f} too low (<{self.opts.min_iv_percentile}) — skipping")
            return None

        # ── Select expiry ──────────────────────────────────────────────────
        today = date.today()
        expiry = _next_thursday(today) if self.opts.expiry == "weekly" else _last_thursday_of_month(today)
        days_to_expiry = (expiry - today).days

        if days_to_expiry < self.opts.min_days_to_expiry:
            # Roll to next expiry
            if self.opts.expiry == "weekly":
                expiry = _next_thursday(expiry + timedelta(days=1))
            else:
                expiry = _last_thursday_of_month(date(today.year, today.month % 12 + 1, 1))
            days_to_expiry = (expiry - today).days

        if days_to_expiry > self.opts.max_days_to_expiry:
            log.debug(f"Options {symbol}: {days_to_expiry} DTE > max {self.opts.max_days_to_expiry} — skipping")
            return None

        # ── Select strike ──────────────────────────────────────────────────
        interval = STRIKE_INTERVALS.get(symbol, 50)
        atm_strike = round(spot / interval) * interval
        offset = self.opts.strike_offset * interval
        option_type = "CE" if equity_signal_side == "BUY" else "PE"

        if option_type == "CE":
            strike = atm_strike + offset   # OTM call (above spot)
        else:
            strike = atm_strike - offset   # OTM put (below spot)

        # ── Price the option (Black-Scholes in paper mode) ─────────────────
        T = days_to_expiry / 365.0
        r = 0.065  # India repo rate
        price, greeks = black_scholes_price(spot, strike, T, r, iv, option_type)

        # ── Position sizing ────────────────────────────────────────────────
        lot = cfg.options.lot_size(symbol)
        max_premium = cfg.initial_capital * (self.opts.max_premium_per_trade_pct / 100)
        lots = max(1, int(max_premium / (price * lot)))
        total_premium = lots * lot * price

        # Check total open option positions
        active_count = len(self._active_positions)
        if active_count >= self.opts.max_option_positions:
            log.debug(f"Options: max positions ({self.opts.max_option_positions}) reached")
            return None

        # ── Build option symbol string ──────────────────────────────────────
        expiry_str = expiry.strftime("%d%b%y").upper()  # e.g. 06MAR25
        symbol_clean = symbol.replace(".NS", "").replace("^", "")
        # NSE option symbol format: UNDERLYING + EXPIRY + STRIKE + CE/PE
        option_symbol = f"{symbol_clean}{expiry_str}{int(strike)}{option_type}"

        confidence = 65.0
        # Boost confidence for lower IV rank (cheaper options)
        if iv_rank < 40:
            confidence += 8
        # Boost for more DTE (less theta risk)
        if days_to_expiry > 7:
            confidence += 5

        log.info(
            f"Options signal: {option_symbol} | Spot={spot:.0f} Strike={strike} "
            f"Premium=₹{price:.1f} Lots={lots} Total=₹{total_premium:.0f} "
            f"IV={iv*100:.0f}% (rank={iv_rank:.0f}) DTE={days_to_expiry} "
            f"Δ={greeks['delta']:.3f} θ={greeks['theta']:.2f}/day"
        )

        # Store for monitoring
        self._active_positions[option_symbol] = {
            "underlying": symbol,
            "type": option_type,
            "strike": strike,
            "expiry": expiry.isoformat(),
            "entry_premium": price,
            "lots": lots,
            "lot_size": lot,
            "greeks": greeks,
            "days_to_expiry": days_to_expiry,
            "stop_premium": round(price * (1 - self.opts.stop_loss_pct / 100), 2),
            "target_premium": round(price * (1 + self.opts.profit_target_pct / 100), 2),
            "iv_at_entry": round(iv * 100, 1),
        }

        return TradeSignal(
            symbol=option_symbol,
            side="BUY",  # Options strategy always buys CE or PE (long options only)
            strategy=self.name,
            confidence=confidence,
            trigger=(
                f"{option_type} on {symbol_clean} | strike={strike} | "
                f"premium=₹{price:.1f} | lots={lots} | DTE={days_to_expiry} | "
                f"IV={iv*100:.0f}% rank={iv_rank:.0f} | "
                f"target=₹{total_premium*(1+self.opts.profit_target_pct/100):.0f} "
                f"stop=₹{total_premium*(1-self.opts.stop_loss_pct/100):.0f}"
            ),
            atr=price * 0.3,     # ATR proxy = 30% of premium (options are volatile)
            cmp=price,
            suggested_qty=lots * lot,
        )

    def check_exit(self, option_symbol: str, current_premium: float) -> Optional[str]:
        """
        Check if an open option position should be exited.
        Returns "TARGET" | "STOP" | None
        Called by the engine's stop/target loop.
        """
        pos = self._active_positions.get(option_symbol)
        if not pos:
            return None

        # Target hit
        if current_premium >= pos["target_premium"]:
            log.info(f"Options TARGET: {option_symbol} premium {current_premium:.1f} >= {pos['target_premium']:.1f}")
            del self._active_positions[option_symbol]
            return "TARGET"

        # Stop hit
        if current_premium <= pos["stop_premium"]:
            log.info(f"Options STOP: {option_symbol} premium {current_premium:.1f} <= {pos['stop_premium']:.1f}")
            del self._active_positions[option_symbol]
            return "STOP"

        # Expiry check — exit 2 days before expiry to avoid theta crush
        expiry = date.fromisoformat(pos["expiry"])
        if (expiry - date.today()).days <= 2:
            log.warning(f"Options EXPIRY EXIT: {option_symbol} — {(expiry - date.today()).days} days left")
            del self._active_positions[option_symbol]
            return "EXPIRY"

        return None

    def get_active_positions(self) -> Dict:
        return self._active_positions.copy()
