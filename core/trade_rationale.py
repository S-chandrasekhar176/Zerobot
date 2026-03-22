# -*- coding: utf-8 -*-
"""
ZeroBot Pro — Trade Rationale Generator (Aladdin Gap #6)
=========================================================
Generates plain-English explanations for every trade signal.
Makes ZeroBot explainable — every position has a "why we bought this" text.

Aladdin analogy:
  BlackRock's Aladdin shows portfolio managers exactly why each trade was
  recommended, decomposed into factor contributions. This module does the
  same for ZeroBot — converting numerical signals into readable sentences.

Usage:
  from core.trade_rationale import generate_rationale
  text = generate_rationale(symbol="HDFCBANK.NS", side="BUY",
                             confidence=72.5, strategy="Momentum",
                             cmp=1250.50, stop_loss=1210.0, target=1340.0,
                             adx=31, sentiment=0.15, vix=14.2,
                             atr=18.5, session="trending")
  # → "Buying HDFCBANK — Momentum breakout (ADX=31, strong trend).
  #    ML ensemble: 72.5% confident. News: Neutral (+0.15).
  #    VIX=14.2 (calm market — good entry). R:R = 2.3:1.
  #    Stop ₹1,210 | Target ₹1,340"
"""

from typing import Optional


def generate_rationale(
    symbol: str,
    side: str,
    confidence: float,
    strategy: str,
    cmp: float,
    stop_loss: float = 0,
    target: float = 0,
    adx: float = 0,
    sentiment: float = 0,
    vix: float = 0,
    atr: float = 0,
    session: str = "",
    news_headline: str = "",
    win_rate: float = 0,
) -> str:
    """
    Generate a concise trade rationale string.
    Designed to be shown in position cards and Telegram alerts.
    """
    parts = []

    # Core action
    action = "Buying" if side == "BUY" else "Shorting"
    sym_clean = symbol.replace(".NS", "").replace("^", "")
    parts.append(f"{action} *{sym_clean}*")

    # Strategy reason
    strategy_text = {
        "Momentum":      f"Momentum breakout{' (ADX={:.0f}, strong trend)'.format(adx) if adx >= 25 else ''}",
        "MeanReversion": "Mean reversion — price oversold vs VWAP",
        "VWAP":          "VWAP reclaim — price crossed above VWAP",
        "MarketMaking":  "Market structure — bid/ask spread capture",
        "StatArb":       f"Stat-arb pair trade — cointegrated spread",
        "Options":       f"Options momentum play",
    }.get(strategy, f"{strategy} signal")
    parts.append(strategy_text)

    # ML confidence
    if confidence > 0:
        conf_label = "High" if confidence >= 75 else "Moderate" if confidence >= 65 else "Low"
        parts.append(f"ML: {confidence:.1f}% ({conf_label} confidence)")

    # Win rate
    if win_rate > 0:
        parts.append(f"Strategy WR: {win_rate*100:.0f}%")

    # News sentiment
    if abs(sentiment) > 0.05:
        if sentiment > 0.4:
            sent_txt = f"News: 🟢 Bullish ({sentiment:+.2f})"
        elif sentiment < -0.4:
            sent_txt = f"News: 🔴 Bearish ({sentiment:+.2f})"
        elif sentiment > 0:
            sent_txt = f"News: Slightly positive ({sentiment:+.2f})"
        else:
            sent_txt = f"News: Slightly negative ({sentiment:+.2f})"
        parts.append(sent_txt)
    else:
        parts.append("News: Neutral")

    # VIX context
    if vix > 0:
        if vix < 15:
            vix_txt = f"VIX={vix:.1f} (calm market — good entry conditions)"
        elif vix < 20:
            vix_txt = f"VIX={vix:.1f} (moderate volatility)"
        elif vix < 25:
            vix_txt = f"VIX={vix:.1f} ⚠ elevated volatility"
        else:
            vix_txt = f"VIX={vix:.1f} 🚨 high volatility — reduced size"
        parts.append(vix_txt)

    # Session context
    if session:
        sess_text = {
            "trending":    "Trending session",
            "range-bound": "Range-bound session",
            "morning":     "Morning session (higher volume)",
            "afternoon":   "Afternoon session",
        }.get(session, "")
        if sess_text:
            parts.append(sess_text)

    # R:R
    if stop_loss > 0 and target > 0 and cmp > 0:
        risk = abs(cmp - stop_loss)
        reward = abs(target - cmp)
        rr = reward / risk if risk > 0 else 0
        parts.append(f"R:R = {rr:.1f}:1 | Stop ₹{stop_loss:.0f} | Target ₹{target:.0f}")

    # Breaking news (if any)
    if news_headline:
        parts.append(f"📰 {news_headline[:80]}")

    return " · ".join(parts)


def short_rationale(symbol: str, side: str, confidence: float,
                    strategy: str, cmp: float) -> str:
    """One-liner version for position card badge."""
    sym = symbol.replace(".NS", "").replace("^", "")
    arrow = "▲" if side == "BUY" else "▼"
    conf_txt = f"{confidence:.0f}%" if confidence > 0 else "—"
    return f"{arrow} {strategy} | {conf_txt} ML confidence | ₹{cmp:.0f}"
