# -*- coding: utf-8 -*-
"""
ZeroBot Pro — Paper Options Pricer (P4 NEW)
Provides synthetic LTP for NSE options in paper mode using simplified Black-Scholes.
Real Angel One/Zerodha WebSocket will replace this in live mode.
"""
import math
from datetime import datetime, date
from typing import Optional
from core.logger import log


def _norm_cdf(x: float) -> float:
    """Standard normal CDF approximation."""
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    d = 0.3989422820 * math.exp(-0.5 * x * x)
    p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.7814779 + t * (-1.8212560 + t * 1.3302745))))
    return 1 - p if x > 0 else p


def black_scholes_price(
    spot: float, strike: float, dte_days: int, iv: float = 0.20,
    rate: float = 0.065, option_type: str = "CE"
) -> float:
    """
    Compute Black-Scholes option premium.
    spot: current underlying price
    strike: option strike
    dte_days: days to expiry
    iv: implied volatility (0.20 = 20%)
    rate: risk-free rate (India RBI ~6.5%)
    option_type: CE or PE
    """
    if dte_days <= 0 or spot <= 0 or strike <= 0:
        return 0.01
    T = dte_days / 252.0
    sqrtT = math.sqrt(T)
    try:
        d1 = (math.log(spot / strike) + (rate + 0.5 * iv ** 2) * T) / (iv * sqrtT)
        d2 = d1 - iv * sqrtT
        if option_type.upper() == "CE":
            price = spot * _norm_cdf(d1) - strike * math.exp(-rate * T) * _norm_cdf(d2)
        else:
            price = strike * math.exp(-rate * T) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        return max(0.05, round(price, 2))
    except Exception:
        return max(0.05, abs(spot - strike) * 0.05)


def get_option_ltp(symbol: str, underlying_price: float) -> Optional[float]:
    """
    Parse option symbol like RELIANCE12MAR261450CE and return synthetic LTP.
    Works for NSE-style symbols: UNDERLYING + DDMMMYY + STRIKE + CE/PE
    """
    try:
        sym = symbol.upper().strip()
        opt_type = "CE" if sym.endswith("CE") else "PE" if sym.endswith("PE") else None
        if not opt_type:
            return None

        # Extract strike (last numeric block before CE/PE)
        body = sym[:-2]  # remove CE/PE
        # Find expiry date block (6 chars: DDMMMYY e.g. 12MAR26)
        import re
        m = re.search(r'(\d{1,2}[A-Z]{3}\d{2})(\d+)$', body)
        if not m:
            return None
        expiry_str = m.group(1)
        strike = float(m.group(2))

        # Parse expiry
        try:
            expiry = datetime.strptime(expiry_str, "%d%b%y").date()
        except Exception:
            return None

        dte = (expiry - date.today()).days
        if dte < 0:
            return 0.05  # expired

        # Estimate IV from distance to ATM
        moneyness = abs(underlying_price - strike) / underlying_price
        iv = 0.18 + moneyness * 0.5  # higher IV for far OTM
        iv = min(iv, 0.80)

        price = black_scholes_price(underlying_price, strike, dte, iv=iv, option_type=opt_type)
        return price
    except Exception as e:
        log.debug(f"Options pricer error for {symbol}: {e}")
        return None
