# -*- coding: utf-8 -*-
"""
ZeroBot — KotegawaStrategy
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Inspired by Takashi Kotegawa (BNF) — Japanese retail legend who
turned ¥1.6M into ¥17B trading liquidity shocks and mean reversion.

Core philosophy:
  Panic creates price dislocations. Forced liquidations overshoot.
  The edge is in buying what institutions are forced to sell and
  selling what retail is chasing in a frenzy — then exiting fast.

Three signal types:
  1. LIQUIDITY SHOCK REVERSAL — price move > 3×ATR + vol spike > 4×avg
  2. SECTOR RELATIVE VALUE    — stock lagging its sector by > 1.5σ
  3. NEWS EVENT MOMENTUM      — Groq-validated high-impact headline

Scoring gate:
  score = 0.35×ML + 0.25×vol_shock + 0.20×atr_move + 0.10×sentiment + 0.10×sector
  Trade only if score > 0.70

Risk:
  Stop loss  = 1.2 × ATR
  Take profit = 2.0 × ATR
  Max hold   = 90 minutes
  Risk/trade = 1.5% capital
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

import asyncio
import json
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from strategies.base_strategy import BaseStrategy
from risk.risk_engine import TradeSignal

log = logging.getLogger(__name__)

# ── Sector map (mirrors risk_engine.py — kept local so strategy is self-contained) ──
_SECTOR_MAP: Dict[str, List[str]] = {
    "Banking":   ["HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "AXISBANK.NS",
                  "KOTAKBANK.NS", "BANDHANBNK.NS", "INDUSINDBK.NS"],
    "IT":        ["TCS.NS", "INFY.NS", "WIPRO.NS", "HCLTECH.NS", "TECHM.NS"],
    "FMCG":      ["HINDUNILVR.NS", "NESTLEIND.NS", "ITC.NS"],
    "Finance":   ["BAJFINANCE.NS", "BAJAJFINSV.NS"],
    "Infra":     ["LT.NS", "NTPC.NS", "POWERGRID.NS"],
    "Commodity": ["TATASTEEL.NS", "ONGC.NS"],
    "Consumer":  ["ASIANPAINT.NS", "ULTRACEMCO.NS", "MARUTI.NS", "TITAN.NS"],
}
# Fast reverse lookup: symbol → sector
_SYM_SECTOR: Dict[str, str] = {
    sym: sec for sec, syms in _SECTOR_MAP.items() for sym in syms
}

# Regime sizing multipliers (BNF always scaled with the market's mood)
_REGIME_MULT: Dict[str, float] = {
    "AGGRESSIVE": 1.2,
    "NORMAL":     1.0,
    "DEFENSIVE":  0.7,
    "CRISIS":     0.4,
}

# Groq prompt — structured JSON output
_GROQ_SYSTEM = (
    "You are an institutional trading desk AI specialising in NSE India liquidity events. "
    "Respond ONLY with a valid JSON object — no markdown, no preamble."
)
_GROQ_USER_TMPL = (
    "Symbol: {symbol}\n"
    "Signal direction: {direction}\n"
    "ATR multiple of move: {atr_multiple:.2f}x (threshold: 3.0x)\n"
    "Volume spike: {volume_ratio:.1f}x average (threshold: 4.0x)\n"
    "Market regime: {regime}\n"
    "Recent headline: {headline}\n\n"
    "Classify this event:\n"
    "1 = panic liquidation (high reversal probability)\n"
    "2 = trend continuation (do NOT fade)\n"
    "3 = false signal / noise\n\n"
    "Return JSON exactly:\n"
    '{{ "decision": "APPROVE" | "REDUCE" | "REJECT", '
    '"confidence": 0.0-1.0, '
    '"event_type": 1 | 2 | 3, '
    '"reason": "one sentence" }}'
)


@dataclass
class KotegawaSignalState:
    """Per-symbol state — cooldown, entry tracking, performance."""
    symbol:          str
    last_signal_ts:  float = 0.0      # unix timestamp of last signal
    entry_price:     float = 0.0
    entry_ts:        float = 0.0
    entry_side:      str   = ""
    groq_calls:      int   = 0        # budget tracking
    wins:            int   = 0
    losses:          int   = 0
    # Rolling sector return window (5 bars)
    _sector_returns: List[float] = field(default_factory=list)

    def record_outcome(self, win: bool):
        if win:
            self.wins += 1
        else:
            self.losses += 1

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.5


class KotegawaStrategy(BaseStrategy):
    """
    BNF-inspired liquidity shock reversal strategy for ZeroBot.

    Plugs into the existing strategy engine as a drop-in BaseStrategy.
    The Groq check is called via asyncio from a sync context using a
    cached event loop (same pattern as groq_gates.py) so generate_signal
    remains synchronous and compatible with the engine's for-loop.
    """

    # ── Configuration ───────────────────────────────────────────────────────
    ATR_SHOCK_MULT       = 3.0    # Price move must be > this × ATR in 15min window
    VOL_SPIKE_THRESHOLD  = 4.0    # Volume must be > this × 20-bar average
    NIFTY_STABLE_PCT     = 0.7    # Market index must be within ±0.7% for shock signal
    MIN_COMPOSITE_SCORE  = 0.70   # Composite score gate (0–1)
    SECTOR_ZSCORE_THRESH = 1.5    # Sector relative value trigger (standard deviations)
    NEWS_CONF_THRESHOLD  = 0.65   # Groq news confidence gate
    GROQ_CONF_THRESHOLD  = 0.60   # Groq shock evaluation gate
    SIGNAL_COOLDOWN_SEC  = 300    # 5 min cooldown per symbol after any signal
    MAX_HOLD_MINUTES     = 90     # Hard exit after 90 minutes
    SL_ATR_MULT          = 1.2    # Stop loss = 1.2 × ATR
    TP_ATR_MULT          = 2.0    # Take profit = 2.0 × ATR

    # Score weights
    W_ML         = 0.35
    W_VOL        = 0.25
    W_ATR        = 0.20
    W_SENTIMENT  = 0.10
    W_SECTOR     = 0.10

    def __init__(self):
        super().__init__("Kotegawa")
        self._state: Dict[str, KotegawaSignalState] = {}
        # Cache for sector returns: sector_name → list of recent bar returns
        self._sector_return_cache: Dict[str, List[float]] = {}
        # Groq budget: max 6 calls per session (conservative — shared budget)
        self._groq_calls_session = 0
        self._groq_budget         = 6
        self._groq_session_date   = ""
        log.info("[KOTEGAWA] Strategy initialized — BNF liquidity shock mode active")

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN ENTRY POINT (called by engine for every symbol every tick)
    # ─────────────────────────────────────────────────────────────────────────

    def generate_signal(
        self,
        df: pd.DataFrame,
        symbol: str,
        # Optional context injected by engine (engine passes these as kwargs
        # if the strategy exposes them — see engine integration note below)
        candle_data: Optional[Dict[str, pd.DataFrame]] = None,
        news_feed=None,
        ml_confidence: float = 50.0,
        ml_direction: str = "HOLD",
        regime: str = "NORMAL",
        nifty_change_pct: float = 0.0,
    ) -> Optional[TradeSignal]:
        """
        Evaluate symbol for a Kotegawa liquidity shock or relative value trade.

        The engine calls this as `strategy.generate_signal(df, sym)`.
        Extra kwargs (candle_data, news_feed, etc.) default gracefully so the
        strategy works even when the engine doesn't inject them yet.
        """
        if not self.enabled:
            return None
        if len(df) < 20:
            return None

        sym_state = self._get_state(symbol)

        # ── Cooldown gate ──────────────────────────────────────────────────
        now_ts = time.time()
        if now_ts - sym_state.last_signal_ts < self.SIGNAL_COOLDOWN_SEC:
            return None

        last = df.iloc[-1]
        cmp  = float(last.get("close", 0))
        atr  = float(last.get("ATRr_14", cmp * 0.01))
        if cmp <= 0 or atr <= 0:
            return None

        # ── Pull available features ────────────────────────────────────────
        vol_spike   = float(last.get("vol_spike", 1.0))
        vwap_dev    = float(last.get("vwap_dev", 0.0))
        rsi         = float(last.get("RSI_14", 50.0))
        news_score  = float(last.get("news_sentiment_score", 0.0))

        # Price change over recent bars (15-min window = last 3 bars on 5m data)
        price_change_15m = self._price_change_window(df, bars=3)
        price_change_5m  = self._price_change_window(df, bars=1)

        # ── Signal 1: Liquidity Shock Reversal ────────────────────────────
        shock_signal = self._liquidity_shock_signal(
            symbol, df, cmp, atr, vol_spike,
            price_change_15m, nifty_change_pct, vwap_dev
        )

        # ── Signal 2: Sector Relative Value ───────────────────────────────
        rv_signal = self._sector_relative_value_signal(
            symbol, cmp, df, candle_data
        )

        # ── Signal 3: News Event Momentum ─────────────────────────────────
        news_signal = self._news_momentum_signal(
            symbol, news_feed, news_score
        )

        # ── Pick the strongest signal ──────────────────────────────────────
        candidate = shock_signal or rv_signal or news_signal
        if candidate is None:
            return None

        direction, signal_type, raw_trigger = candidate

        # ── Composite score gate ───────────────────────────────────────────
        ml_prob        = ml_confidence / 100.0
        vol_shock_score = min(1.0, (vol_spike - self.VOL_SPIKE_THRESHOLD) / 6.0 + 0.5)
        atr_move_score  = min(1.0, abs(price_change_15m) / (self.ATR_SHOCK_MULT * atr / cmp * 100) * 0.5 + 0.3)
        sentiment_score = min(1.0, max(0.0, (abs(news_score) * 0.5 + 0.5)))
        sector_score    = self._sector_strength_score(symbol, direction, candle_data)

        # Adjust scores if ML disagrees
        if ml_direction not in (direction, "HOLD"):
            ml_prob = max(0.3, ml_prob - 0.2)

        composite = (
            self.W_ML        * ml_prob
            + self.W_VOL     * vol_shock_score
            + self.W_ATR     * atr_move_score
            + self.W_SENTIMENT * sentiment_score
            + self.W_SECTOR  * sector_score
        )

        log.debug(
            f"[KOTEGAWA] {symbol} | {direction} | type={signal_type} | "
            f"score={composite:.3f} (ml={ml_prob:.2f} vol={vol_shock_score:.2f} "
            f"atr={atr_move_score:.2f} sent={sentiment_score:.2f} sec={sector_score:.2f})"
        )

        if composite < self.MIN_COMPOSITE_SCORE:
            log.debug(f"[KOTEGAWA] {symbol} rejected — score {composite:.3f} < {self.MIN_COMPOSITE_SCORE}")
            return None

        # ── Groq validation (shock signal only — news already Groq-vetted) ─
        groq_approved = True
        groq_conf     = 0.75
        if signal_type == "shock":
            atr_multiple = abs(price_change_15m) / (atr / cmp * 100) if atr > 0 else 0
            headline     = self._get_latest_headline(symbol, news_feed)
            groq_result  = self._call_groq_sync(
                symbol=symbol,
                direction=direction,
                atr_multiple=atr_multiple,
                volume_ratio=vol_spike,
                regime=regime,
                headline=headline,
            )
            if groq_result:
                decision = groq_result.get("decision", "REJECT")
                groq_conf = float(groq_result.get("confidence", 0.5))
                event_type = groq_result.get("event_type", 3)
                reason = groq_result.get("reason", "")
                if decision == "REJECT" or groq_conf < self.GROQ_CONF_THRESHOLD:
                    log.info(
                        f"[KOTEGAWA] {symbol} GROQ REJECTED — "
                        f"decision={decision} conf={groq_conf:.2f} reason={reason}"
                    )
                    return None
                if decision == "REDUCE":
                    composite *= 0.80
                    groq_conf  *= 0.85
                if event_type == 2:
                    # Trend continuation — wrong direction for reversal strategy
                    log.info(f"[KOTEGAWA] {symbol} Groq says trend continuation — skip reversal")
                    return None
                log.info(
                    f"[KOTEGAWA] {symbol} Groq {decision} | conf={groq_conf:.2f} | {reason}"
                )
            # If Groq unavailable, proceed cautiously (reduce confidence)
            else:
                composite  *= 0.90
                groq_conf   = 0.60

        # ── Regime size multiplier on confidence ───────────────────────────
        regime_key  = regime.upper().replace(" ", "_")
        regime_mult = _REGIME_MULT.get(regime_key, 1.0)
        # CRISIS regime: this strategy is specifically useful (panic = opportunity)
        # but we still apply a safety haircut
        if regime_key == "CRISIS":
            regime_mult = 0.5

        # Final confidence (engine blends with ML on its side too)
        final_conf = min(92.0, composite * 100 * regime_mult * groq_conf)
        final_conf = max(50.0, final_conf)

        # ── Build trigger string ───────────────────────────────────────────
        atr_mult_str = f"{abs(price_change_15m) / (atr / cmp * 100):.1f}" if atr > 0 else "?"
        trigger = (
            f"[KOTEGAWA-{signal_type.upper()}] {direction} | "
            f"score={composite:.2f} | vol={vol_spike:.1f}x | "
            f"ATR_move={atr_mult_str}x | {raw_trigger}"
        )

        # ── Record state ───────────────────────────────────────────────────
        sym_state.last_signal_ts = now_ts
        sym_state.entry_price    = cmp
        sym_state.entry_ts       = now_ts
        sym_state.entry_side     = direction

        log.info(
            f"[KOTEGAWA] ✅ SIGNAL | {symbol} {direction} | "
            f"conf={final_conf:.1f}% | score={composite:.3f} | {trigger}"
        )

        return TradeSignal(
            symbol     = symbol,
            side       = direction,
            strategy   = self.name,
            confidence = final_conf,
            trigger    = trigger,
            atr        = atr,
            cmp        = cmp,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # SIGNAL 1 — LIQUIDITY SHOCK REVERSAL
    # ─────────────────────────────────────────────────────────────────────────

    def _liquidity_shock_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        cmp: float,
        atr: float,
        vol_spike: float,
        price_change_15m: float,
        nifty_change_pct: float,
        vwap_dev: float,
    ) -> Optional[Tuple[str, str, str]]:
        """
        Detect forced liquidation / panic buying.

        Returns (direction, signal_type, trigger_description) or None.
        """
        # Market index stability gate — don't fade a genuine market crash
        if abs(nifty_change_pct) > self.NIFTY_STABLE_PCT:
            log.debug(
                f"[KOTEGAWA] {symbol} shock skipped — NIFTY move {nifty_change_pct:+.2f}% "
                f"exceeds ±{self.NIFTY_STABLE_PCT}%"
            )
            return None

        # Volume gate — must be an exceptional spike
        if vol_spike < self.VOL_SPIKE_THRESHOLD:
            return None

        # Price move gate — ATR-normalised move in 15-min window
        atr_pct      = atr / cmp * 100  # ATR as % of price
        move_in_atrs = abs(price_change_15m) / atr_pct if atr_pct > 0 else 0

        if move_in_atrs < self.ATR_SHOCK_MULT:
            return None

        # VWAP context: reversal only makes sense if price is far from VWAP
        vwap_displaced = abs(vwap_dev) > 0.5  # price > 0.5% from VWAP

        # BUY on panic drop — price crashed down, expect mean reversion up
        if price_change_15m < 0 and vwap_dev < -0.5:
            trigger = (
                f"Panic DROP {price_change_15m:.2f}% | "
                f"{move_in_atrs:.1f}× ATR | vol {vol_spike:.1f}× | "
                f"VWAP_dev={vwap_dev:.2f}%"
            )
            return "BUY", "shock", trigger

        # SELL on panic spike — price blew up, expect mean reversion down
        if price_change_15m > 0 and vwap_dev > 0.5:
            trigger = (
                f"Panic SPIKE +{price_change_15m:.2f}% | "
                f"{move_in_atrs:.1f}× ATR | vol {vol_spike:.1f}× | "
                f"VWAP_dev={vwap_dev:.2f}%"
            )
            return "SELL", "shock", trigger

        return None

    # ─────────────────────────────────────────────────────────────────────────
    # SIGNAL 2 — SECTOR RELATIVE VALUE
    # ─────────────────────────────────────────────────────────────────────────

    def _sector_relative_value_signal(
        self,
        symbol: str,
        cmp: float,
        df: pd.DataFrame,
        candle_data: Optional[Dict[str, pd.DataFrame]],
    ) -> Optional[Tuple[str, str, str]]:
        """
        Detect when a stock is lagging its sector peers by > 1.5σ.
        Uses same candle_data dict available in the engine.
        """
        if candle_data is None:
            return None

        sector = _SYM_SECTOR.get(symbol)
        if sector is None:
            return None

        peers = [s for s in _SECTOR_MAP.get(sector, []) if s != symbol and s in candle_data]
        if len(peers) < 2:
            return None

        # Compute last-bar return for each peer
        peer_returns = []
        for peer in peers:
            peer_df = candle_data[peer]
            if len(peer_df) < 2:
                continue
            ret = float(peer_df["close"].iloc[-1] / peer_df["close"].iloc[-2] - 1) * 100
            peer_returns.append(ret)

        if len(peer_returns) < 2:
            return None

        sector_return = float(np.mean(peer_returns))
        sector_std    = float(np.std(peer_returns)) if len(peer_returns) > 1 else 0.5
        if sector_std < 0.01:
            sector_std = 0.01

        stock_return  = float(df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100 if len(df) >= 2 else 0.0
        relative_ret  = sector_return - stock_return
        zscore        = relative_ret / sector_std

        log.debug(
            f"[KOTEGAWA] {symbol} sector={sector} | "
            f"sector_ret={sector_return:.3f}% stock_ret={stock_return:.3f}% "
            f"rel={relative_ret:.3f}% z={zscore:.2f}"
        )

        if zscore > self.SECTOR_ZSCORE_THRESH:
            # Stock is meaningfully lagging sector → catch-up BUY
            trigger = (
                f"Sector RV BUY | sector={sector} | "
                f"lag={relative_ret:.2f}% | z={zscore:.2f}σ"
            )
            return "BUY", "sector_rv", trigger

        if zscore < -self.SECTOR_ZSCORE_THRESH:
            # Stock is meaningfully outrunning sector → fade SELL
            trigger = (
                f"Sector RV SELL | sector={sector} | "
                f"lead={-relative_ret:.2f}% | z={zscore:.2f}σ"
            )
            return "SELL", "sector_rv", trigger

        return None

    # ─────────────────────────────────────────────────────────────────────────
    # SIGNAL 3 — NEWS EVENT MOMENTUM (Groq-validated)
    # ─────────────────────────────────────────────────────────────────────────

    def _news_momentum_signal(
        self,
        symbol: str,
        news_feed,
        news_score: float,
    ) -> Optional[Tuple[str, str, str]]:
        """
        High-impact news → Groq evaluation → directional trade.
        Only fires when |news_score| >= 0.4 (matches engine's Gate 11 threshold).
        """
        # Gate: only act on genuinely impactful news
        if news_feed is None or abs(news_score) < 0.40:
            return None

        # Pull the latest headline for this symbol
        headline = self._get_latest_headline(symbol, news_feed)
        if not headline:
            return None

        # Groq confidence gate for news direction
        direction = "BUY" if news_score > 0 else "SELL"
        groq_result = self._call_groq_news(symbol, direction, headline)

        if groq_result is None:
            # Groq unavailable — fall back to raw sentiment score if strong enough
            if abs(news_score) >= 0.6:
                trigger = f"News momentum (no Groq) | score={news_score:+.2f} | {headline[:60]}"
                return direction, "news", trigger
            return None

        groq_conf = float(groq_result.get("confidence", 0.0))
        decision  = groq_result.get("decision", "REJECT")
        reason    = groq_result.get("reason", "")

        if decision == "REJECT" or groq_conf < self.NEWS_CONF_THRESHOLD:
            log.debug(f"[KOTEGAWA] {symbol} news rejected by Groq: {reason}")
            return None

        trigger = (
            f"News {direction} | Groq={decision} conf={groq_conf:.2f} | "
            f"score={news_score:+.2f} | {headline[:70]}"
        )
        log.info(f"[KOTEGAWA] News signal: {symbol} {direction} | {reason}")
        return direction, "news", trigger

    # ─────────────────────────────────────────────────────────────────────────
    # GROQ HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _call_groq_sync(
        self,
        symbol: str,
        direction: str,
        atr_multiple: float,
        volume_ratio: float,
        regime: str,
        headline: str,
    ) -> Optional[dict]:
        """
        Synchronous Groq call via the shared groq_brain singleton.
        Uses asyncio.run_coroutine_threadsafe when inside async context,
        falls back to a new event loop otherwise.
        """
        if not self._groq_budget_ok():
            log.debug("[KOTEGAWA] Groq budget exhausted — skipping shock check")
            return None

        try:
            from core.groq_brain import groq_brain
            if not groq_brain.is_available:
                return None
        except ImportError:
            return None

        user_msg = _GROQ_USER_TMPL.format(
            symbol       = symbol.replace(".NS", ""),
            direction    = direction,
            atr_multiple = atr_multiple,
            volume_ratio = volume_ratio,
            regime       = regime,
            headline     = headline[:150] if headline else "N/A",
        )

        try:
            result = self._run_async_safely(
                self._groq_raw_call(groq_brain, user_msg)
            )
            if result:
                self._groq_calls_session += 1
            return result
        except Exception as e:
            log.debug(f"[KOTEGAWA] Groq call error: {e}")
            return None

    def _call_groq_news(self, symbol: str, direction: str, headline: str) -> Optional[dict]:
        """Call Groq news_impact and map to our decision format."""
        if not self._groq_budget_ok():
            return None
        try:
            from core.groq_brain import groq_brain
            if not groq_brain.is_available:
                return None
            news_result = self._run_async_safely(
                groq_brain.news_impact(symbol, headline, direction)
            )
            if news_result is None:
                return None
            self._groq_calls_session += 1
            # Map NewsImpact → our dict format
            implication = getattr(news_result, "trade_implication", "HOLD")
            conf        = getattr(news_result, "confidence", 0.5)
            reason      = getattr(news_result, "reasoning", "")
            decision    = "APPROVE" if implication in ("BUY", "SELL") else "REJECT"
            return {"decision": decision, "confidence": conf, "reason": reason}
        except Exception as e:
            log.debug(f"[KOTEGAWA] Groq news call error: {e}")
            return None

    async def _groq_raw_call(self, groq_brain, user_msg: str) -> Optional[dict]:
        """Async wrapper for raw Groq API call with JSON parsing."""
        try:
            data, _ = await groq_brain._call(
                "kotegawa_shock",
                _GROQ_SYSTEM,
                {"user_message": user_msg},
            )
            if data is None:
                return None
            # groq_brain._call returns parsed dict; if it returns raw string, parse it
            if isinstance(data, str):
                clean = data.strip().lstrip("```json").rstrip("```").strip()
                return json.loads(clean)
            return data
        except Exception as e:
            log.debug(f"[KOTEGAWA] _groq_raw_call parse error: {e}")
            return None

    @staticmethod
    def _run_async_safely(coro):
        """
        Run an async coroutine from a synchronous context.
        Works whether called from inside or outside an existing event loop.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're inside an async context — use run_coroutine_threadsafe
                import concurrent.futures
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                return future.result(timeout=5.0)
            else:
                return loop.run_until_complete(coro)
        except Exception as e:
            log.debug(f"[KOTEGAWA] async runner error: {e}")
            return None

    def _groq_budget_ok(self) -> bool:
        """Reset daily budget and check remaining calls."""
        import datetime
        today = datetime.date.today().isoformat()
        if self._groq_session_date != today:
            self._groq_session_date   = today
            self._groq_calls_session  = 0
        return self._groq_calls_session < self._groq_budget

    # ─────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _price_change_window(self, df: pd.DataFrame, bars: int = 3) -> float:
        """Return % price change over the last N bars."""
        if len(df) < bars + 1:
            return 0.0
        start = float(df["close"].iloc[-(bars + 1)])
        end   = float(df["close"].iloc[-1])
        if start <= 0:
            return 0.0
        return (end - start) / start * 100.0

    def _sector_strength_score(
        self,
        symbol: str,
        direction: str,
        candle_data: Optional[Dict[str, pd.DataFrame]],
    ) -> float:
        """
        0–1 sector strength score.
        Returns 0.5 (neutral) if candle_data unavailable.
        """
        if candle_data is None:
            return 0.5
        sector = _SYM_SECTOR.get(symbol)
        if sector is None:
            return 0.5
        peers = [s for s in _SECTOR_MAP.get(sector, []) if s != symbol and s in candle_data]
        if len(peers) < 2:
            return 0.5

        peer_returns = []
        for peer in peers:
            pdf = candle_data[peer]
            if len(pdf) < 2:
                continue
            ret = float(pdf["close"].iloc[-1] / pdf["close"].iloc[-2] - 1)
            peer_returns.append(ret)

        if not peer_returns:
            return 0.5

        avg_sector_ret = float(np.mean(peer_returns))
        # For BUY: positive sector return boosts score
        # For SELL: negative sector return boosts score
        if direction == "BUY":
            score = 0.5 + min(0.5, avg_sector_ret * 50)   # +1%/day → 0.5 bonus
        else:
            score = 0.5 - max(-0.5, avg_sector_ret * 50)

        return float(np.clip(score, 0.0, 1.0))

    def _get_latest_headline(self, symbol: str, news_feed) -> str:
        """Safely pull the most recent headline for a symbol."""
        if news_feed is None:
            return ""
        try:
            headlines = news_feed.get_headlines_for_symbol(symbol, max_age_hours=2, limit=1)
            if headlines:
                return headlines[0].get("title", "")
        except Exception:
            pass
        return ""

    def _get_state(self, symbol: str) -> KotegawaSignalState:
        if symbol not in self._state:
            self._state[symbol] = KotegawaSignalState(symbol=symbol)
        return self._state[symbol]

    # ─────────────────────────────────────────────────────────────────────────
    # EXIT LOGIC (called by engine's position management loop)
    # ─────────────────────────────────────────────────────────────────────────

    def should_exit(
        self,
        symbol: str,
        current_price: float,
        entry_price: float,
        entry_side: str,
        entry_time: float,
        atr: float,
        vwap: float,
    ) -> Tuple[bool, str]:
        """
        Called externally (e.g. engine's position monitor) to check
        Kotegawa-specific exit conditions beyond the risk engine's SL/TP.

        Returns (should_exit: bool, reason: str)
        """
        now = time.time()
        mins_held = (now - entry_time) / 60.0

        # Exit 1: 90-minute hard stop
        if mins_held >= self.MAX_HOLD_MINUTES:
            return True, f"[KOTEGAWA] Max hold {self.MAX_HOLD_MINUTES}min reached"

        # Priority: SL/TP checked first (hard risk limits), then VWAP reversion
        if entry_side == "BUY":
            # Stop loss — absolute floor
            if current_price <= entry_price - (self.SL_ATR_MULT * atr):
                return True, f"[KOTEGAWA] SL hit | entry={entry_price:.2f} current={current_price:.2f}"
            # Take profit — ATR target
            if current_price >= entry_price + (self.TP_ATR_MULT * atr):
                return True, f"[KOTEGAWA] TP hit +{self.TP_ATR_MULT}×ATR"
            # VWAP reversion — mean-reversion goal reached
            if current_price >= vwap:
                return True, f"[KOTEGAWA] VWAP reversion BUY → ₹{current_price:.2f} ≥ VWAP ₹{vwap:.2f}"
        else:  # SELL
            if current_price >= entry_price + (self.SL_ATR_MULT * atr):
                return True, f"[KOTEGAWA] SL hit | entry={entry_price:.2f} current={current_price:.2f}"
            if current_price <= entry_price - (self.TP_ATR_MULT * atr):
                return True, f"[KOTEGAWA] TP hit +{self.TP_ATR_MULT}×ATR"
            if current_price <= vwap:
                return True, f"[KOTEGAWA] VWAP reversion SELL → ₹{current_price:.2f} ≤ VWAP ₹{vwap:.2f}"

        return False, ""

    # ─────────────────────────────────────────────────────────────────────────
    # PERFORMANCE REPORTING
    # ─────────────────────────────────────────────────────────────────────────

    def performance_summary(self) -> dict:
        """Return per-symbol performance metrics for dashboard / logging."""
        total_wins   = sum(s.wins   for s in self._state.values())
        total_losses = sum(s.losses for s in self._state.values())
        total        = total_wins + total_losses
        return {
            "strategy":      self.name,
            "total_signals": total,
            "wins":          total_wins,
            "losses":        total_losses,
            "win_rate":      round(total_wins / total, 3) if total > 0 else 0.0,
            "groq_calls":    self._groq_calls_session,
            "groq_budget":   self._groq_budget,
            "symbols_active": len(self._state),
        }

    def __repr__(self) -> str:
        perf = self.performance_summary()
        return (
            f"KotegawaStrategy(enabled={self.enabled} "
            f"signals={perf['total_signals']} "
            f"win_rate={perf['win_rate']:.1%} "
            f"groq={perf['groq_calls']}/{perf['groq_budget']})"
        )
