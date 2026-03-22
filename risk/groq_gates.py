"""
ZeroBot v1.1 — Groq LLM-Powered Risk Gates
==========================================
Replaces local XGBoost Gate 6 (ML Confidence) and keyword Gate 11 (News Sentiment)
with real-time LLM evaluation via Groq's ultra-fast inference API.

WHY GROQ:
  • ~500 tokens/second on LPU hardware — sub-200ms latency per call
  • LLaMA 3.3 70B understands NSE sector dynamics, RBI policy, FII flows
  • 14,400 free requests/day on free tier (~24 trades/min, far more than needed)
  • Reads actual news headlines — far better than keyword scoring
  • Returns structured JSON directly, no post-processing overhead

INTEGRATION:
  When GROQ_API_KEY is set in config/.env, this module intercepts
  Gates 6 and 11 in the RiskEngine._run_gates() call and replaces
  local logic with a single Groq LLaMA call that evaluates BOTH gates together.

  Fallback: If Groq call fails (timeout/rate limit), local gates run as before.

SYSTEM PROMPT (ZEROBOT-ORACLE):
  Deterministic quantitative risk engine persona.
  Outputs only minified JSON — no markdown, no preamble.
  Zero temperature for reproducibility.
"""

import json
import time
import asyncio
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ── ZEROBOT-ORACLE System Prompt ─────────────────────────────────────────────
_SYSTEM_PROMPT = """You are ZEROBOT-ORACLE, a deterministic, zero-latency quantitative risk engine.
Your sole function is to evaluate two specific Risk Gates (Gate 6: ML Confidence and Gate 11: News Sentiment) for an algorithmic trading system operating on the NSE India market.

INPUT SCHEMA (JSON provided by user):
{
  "symbol": "string (e.g., RELIANCE.NS)",
  "sector": "string (e.g., Energy)",
  "signal": "BUY" | "SELL",
  "base_strategy_conf": float (0.0 to 1.0),
  "vix": float,
  "news_headline": "string"
}

EVALUATION LOGIC & RULES:

1. Calculate 'sentiment_score' (Float between -1.0 and 1.0):
   - Read 'news_headline' and determine its immediate impact ONLY on the specific 'sector' and 'symbol'.
   - +1.0 = Highly Bullish, -1.0 = Highly Bearish.
   - 0.0 = Neutral or macro-noise irrelevant to the specific sector.

2. Evaluate Gate 11 ('gate_11_pass' - Boolean):
   - MUST be false IF the sentiment strongly contradicts the signal. (e.g., signal="BUY" and sentiment <= -0.4, or signal="SELL" and sentiment >= 0.4).
   - MUST be true if sentiment aligns with the signal, or if sentiment is neutral (-0.3 to 0.3).

3. Calculate 'ml_conf' (Float between 0.0 and 1.0):
   - Start with 'base_strategy_conf'.
   - Confluence: If signal and sentiment align (e.g., BUY and sentiment > 0.3), add the sentiment_score to ml_conf (max 1.0).
   - Divergence: If they diverge slightly, subtract the absolute sentiment_score.
   - VIX Penalty: If 'vix' > 20.0, subtract 0.15 from ml_conf.
   - Clamp the final result strictly between 0.00 and 1.00.

4. Evaluate Gate 6 ('gate_6_pass' - Boolean):
   - MUST be true ONLY IF the final 'ml_conf' >= 0.62. Otherwise, false.

STRICT OUTPUT CONSTRAINTS:
- You MUST output ONLY a raw, minified JSON object.
- NO markdown code blocks (do not use ```json).
- NO conversational text, preambles, or explanations.
- The output keys must exactly match the schema below.

REQUIRED OUTPUT JSON SCHEMA:
{"sentiment_score": float, "ml_conf": float, "gate_11_pass": boolean, "gate_6_pass": boolean, "reasoning": "string (maximum 15 words explaining the exact reason for the gate decisions)"}"""

# ── Symbol → Sector Mapping (NSE India) ──────────────────────────────────────
_SECTOR_MAP: Dict[str, str] = {
    "RELIANCE.NS": "Energy",      "ONGC.NS": "Energy",         "NTPC.NS": "Utilities",
    "POWERGRID.NS": "Utilities",  "HDFCBANK.NS": "Banking",    "ICICIBANK.NS": "Banking",
    "AXISBANK.NS": "Banking",     "KOTAKBANK.NS": "Banking",   "SBIN.NS": "Banking",
    "INDUSINDBK.NS": "Banking",   "BANDHANBNK.NS": "Banking",  "TCS.NS": "IT",
    "INFY.NS": "IT",              "WIPRO.NS": "IT",            "HCLTECH.NS": "IT",
    "TECHM.NS": "IT",             "BAJFINANCE.NS": "Finance",  "BAJAJFINSV.NS": "Finance",
    "MARUTI.NS": "Auto",          "LT.NS": "Infrastructure",   "HINDUNILVR.NS": "FMCG",
    "NESTLEIND.NS": "FMCG",       "ITC.NS": "FMCG",           "ASIANPAINT.NS": "Consumer",
    "TITAN.NS": "Consumer",       "ULTRACEMCO.NS": "Cement",   "TATASTEEL.NS": "Steel",
}

def _get_sector(symbol: str) -> str:
    return _SECTOR_MAP.get(symbol, "Diversified")


@dataclass
class GroqGateResult:
    sentiment_score: float
    ml_conf: float
    gate_6_pass: bool
    gate_11_pass: bool
    reasoning: str
    latency_ms: int
    source: str = "groq"  # "groq" | "fallback"


class GroqGateEvaluator:
    """
    LLM-powered evaluator for Gates 6 (ML Confidence) and 11 (News Sentiment).

    Uses Groq's LLaMA 3.3 70B via their ultra-fast LPU inference API.
    Falls back to local logic if Groq is unavailable.

    Usage:
        evaluator = GroqGateEvaluator(api_key="gsk_...")
        result = await evaluator.evaluate(
            symbol="HDFCBANK.NS",
            signal="BUY",
            base_conf=0.62,
            vix=14.2,
            news_headline="RBI unexpectedly cuts repo rate by 25 basis points."
        )
        if result.gate_6_pass and result.gate_11_pass:
            # proceed with order
    """

    _MODEL = "llama-3.3-70b-versatile"  # Best accuracy + speed on Groq
    _TIMEOUT_S = 3.0                    # Max 3s — if slower, fall back to local
    _MAX_RETRIES = 1
    _CALL_COUNT = 0          # Total calls since process start
    _CALLS_TODAY = 0          # Calls today (resets at midnight)
    _CALLS_TODAY_DATE: str = ""  # Date string for daily reset
    _TOTAL_LATENCY_MS = 0
    _DECISION_LOG: list = []   # Last 50 Groq decisions for dashboard
    _MAX_LOG = 50

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._client = None
        self._available = False
        self._last_error: str = ""
        self._init_client()

    def _init_client(self):
        try:
            from groq import Groq
            self._client = Groq(api_key=self._api_key)
            self._available = True
            log.info(f"[GROQ] ✅ Groq client initialized — model: {self._MODEL}")
        except ImportError:
            log.warning("[GROQ] groq package not installed. Run: pip install groq")
            self._available = False
        except Exception as e:
            log.warning(f"[GROQ] Init failed: {e}")
            self._available = False

    @property
    def is_available(self) -> bool:
        return self._available and bool(self._api_key)

    def evaluate_sync(
        self,
        symbol: str,
        signal: str,
        base_conf: float,
        vix: float,
        news_headline: str = "",
    ) -> GroqGateResult:
        """Synchronous evaluation (used from _run_gates in RiskEngine)."""
        if not self.is_available:
            return self._local_fallback(symbol, signal, base_conf, vix, news_headline)

        sector = _get_sector(symbol)
        payload = {
            "symbol": symbol,
            "sector": sector,
            "signal": signal,
            "base_strategy_conf": round(base_conf, 4),
            "vix": round(vix, 2),
            "news_headline": news_headline or "No relevant news today.",
        }

        t0 = time.time()
        try:
            # BUG-FIX: Groq SDK >=0.11 requires httpx.Timeout object for the
            # timeout parameter. A bare float raises a TypeError at runtime.
            try:
                import httpx as _httpx
                _timeout = _httpx.Timeout(self._TIMEOUT_S)
            except ImportError:
                _timeout = self._TIMEOUT_S  # older SDK accepts float directly

            resp = self._client.chat.completions.create(
                model=self._MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": json.dumps(payload)},
                ],
                max_tokens=120,
                temperature=0,       # Deterministic — same input = same output
                timeout=_timeout,
            )
            latency_ms = int((time.time() - t0) * 1000)
            raw = resp.choices[0].message.content.strip()

            # Strip any accidental markdown fences
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            data = json.loads(raw)
            result = GroqGateResult(
                sentiment_score=float(data["sentiment_score"]),
                ml_conf=float(data["ml_conf"]),
                gate_6_pass=bool(data["gate_6_pass"]),
                gate_11_pass=bool(data["gate_11_pass"]),
                reasoning=str(data.get("reasoning", "")),
                latency_ms=latency_ms,
                source="groq",
            )
            GroqGateEvaluator._CALL_COUNT += 1
            GroqGateEvaluator._TOTAL_LATENCY_MS += latency_ms
            # Daily reset
            import datetime as _dt_cr
            _today = _dt_cr.date.today().isoformat()
            if GroqGateEvaluator._CALLS_TODAY_DATE != _today:
                GroqGateEvaluator._CALLS_TODAY = 0
                GroqGateEvaluator._CALLS_TODAY_DATE = _today
            GroqGateEvaluator._CALLS_TODAY += 1

            # Store in dashboard log (keep last 50)
            import datetime as _dt
            GroqGateEvaluator._DECISION_LOG.append({
                "time": _dt.datetime.now().strftime("%H:%M:%S"),
                "symbol": symbol,
                "side": signal,
                "gate6": result.gate_6_pass,
                "gate11": result.gate_11_pass,
                "approved": result.gate_6_pass and result.gate_11_pass,
                "ml_conf": round(result.ml_conf, 2),
                "sentiment": round(result.sentiment_score, 2),
                "latency_ms": latency_ms,
                "reasoning": result.reasoning[:80],
                "source": "groq",
            })
            if len(GroqGateEvaluator._DECISION_LOG) > GroqGateEvaluator._MAX_LOG:
                GroqGateEvaluator._DECISION_LOG.pop(0)

            log.info(
                f"[GROQ] {symbol} {signal} | conf={result.ml_conf:.2f} | "
                f"sent={result.sentiment_score:+.2f} | "
                f"G6={'✅' if result.gate_6_pass else '❌'} "
                f"G11={'✅' if result.gate_11_pass else '❌'} | "
                f"{latency_ms}ms | {result.reasoning}"
            )
            return result

        except json.JSONDecodeError as e:
            log.warning(f"[GROQ] JSON parse error: {e} | raw: {raw[:100]}")
            return self._local_fallback(symbol, signal, base_conf, vix, news_headline)
        except Exception as e:
            self._last_error = str(e)
            log.warning(f"[GROQ] API call failed ({int((time.time()-t0)*1000)}ms): {e} — using local fallback")
            return self._local_fallback(symbol, signal, base_conf, vix, news_headline)

    def _local_fallback(
        self,
        symbol: str,
        signal: str,
        base_conf: float,
        vix: float,
        news_headline: str,
    ) -> GroqGateResult:
        """Local gate computation — used when Groq is unavailable."""
        log.info(f"[GROQ] ⚡ Using LOCAL fallback gates for {symbol} {signal} (Groq unavailable)")
        # Gate 11: simple keyword sentiment
        headline_lower = (news_headline or "").lower()
        bullish_kw = ["acquisition", "profit", "growth", "buy", "upgrade", "beat", "rate cut", "dividend", "expansion"]
        bearish_kw = ["fraud", "crash", "loss", "sell", "downgrade", "miss", "rate hike", "default", "insolvency"]
        bull_score = sum(1 for k in bullish_kw if k in headline_lower)
        bear_score = sum(1 for k in bearish_kw if k in headline_lower)
        sentiment = 0.0
        if bull_score > bear_score:   sentiment = min(0.6, bull_score * 0.2)
        elif bear_score > bull_score: sentiment = max(-0.6, -bear_score * 0.2)

        gate_11 = not (
            (signal == "BUY"  and sentiment <= -0.4) or
            (signal == "SELL" and sentiment >=  0.4)
        )

        # Gate 6: adjust conf
        ml_conf = base_conf
        if signal == "BUY"  and sentiment > 0.3:  ml_conf = min(1.0, ml_conf + sentiment)
        elif signal == "SELL" and sentiment < -0.3: ml_conf = min(1.0, ml_conf + abs(sentiment))
        else: ml_conf = max(0.0, ml_conf - abs(sentiment))
        if vix > 20.0: ml_conf = max(0.0, ml_conf - 0.15)
        # BUG-FIX-2: was 0.65 — now 0.62 to match local risk_engine gate (line 430)
        gate_6 = ml_conf >= 0.62

        _fb_result = GroqGateResult(
            sentiment_score=round(sentiment, 3),
            ml_conf=round(ml_conf, 4),
            gate_6_pass=gate_6,
            gate_11_pass=gate_11,
            reasoning="Local fallback: Groq unavailable",
            latency_ms=0,
            source="fallback",
        )
        import datetime as _dt2
        GroqGateEvaluator._DECISION_LOG.append({
            "time": _dt2.datetime.now().strftime("%H:%M:%S"),
            "symbol": symbol,
            "side": signal,
            "gate6": gate_6,
            "gate11": gate_11,
            "approved": gate_6 and gate_11,
            "ml_conf": round(ml_conf, 2),
            "sentiment": round(sentiment, 2),
            "latency_ms": 0,
            "reasoning": "Local fallback — Groq unavailable",
            "source": "fallback",
        })
        if len(GroqGateEvaluator._DECISION_LOG) > GroqGateEvaluator._MAX_LOG:
            GroqGateEvaluator._DECISION_LOG.pop(0)
        return _fb_result

    async def evaluate_async(self,
        symbol: str, signal: str, base_conf: float,
        vix: float, news_headline: str = "") -> "GroqGateResult":
        """
        FIX-3: Async wrapper — runs evaluate_sync in a thread executor
        so the Groq HTTP call (200-2000ms) does NOT block the asyncio event loop.
        Use this from async code; evaluate_sync is for sync code only.
        """
        import asyncio as _aio
        import functools
        loop = _aio.get_event_loop()
        fn = functools.partial(
            self.evaluate_sync,
            symbol=symbol, signal=signal, base_conf=base_conf,
            vix=vix, news_headline=news_headline
        )
        return await loop.run_in_executor(None, fn)

    @classmethod
    def get_stats(cls) -> Dict[str, Any]:
        avg_lat = (cls._TOTAL_LATENCY_MS // cls._CALL_COUNT) if cls._CALL_COUNT else 0
        approved = sum(1 for d in cls._DECISION_LOG if d["approved"])
        blocked  = sum(1 for d in cls._DECISION_LOG if not d["approved"])
        fallback = sum(1 for d in cls._DECISION_LOG if d.get("source") == "fallback")
        groq_ok  = sum(1 for d in cls._DECISION_LOG if d.get("source") == "groq")
        return {
            "total_calls": cls._CALL_COUNT,
            "calls_today": cls._CALLS_TODAY,
            "avg_latency_ms": avg_lat,
            "approved": approved,
            "blocked": blocked,
            "groq_calls": groq_ok,
            "fallback_calls": fallback,
            "decisions": list(reversed(cls._DECISION_LOG)),  # newest first
        }


# ── Module-level singleton (lazy init) ───────────────────────────────────────
_evaluator: Optional[GroqGateEvaluator] = None

def get_groq_evaluator() -> Optional[GroqGateEvaluator]:
    """Get or create the singleton GroqGateEvaluator. Returns None if not configured."""
    global _evaluator
    if _evaluator is not None:
        return _evaluator
    try:
        from core.config import cfg
        if cfg.groq_api_key:
            _evaluator = GroqGateEvaluator(api_key=cfg.groq_api_key)
            return _evaluator
    except Exception as e:
        log.debug(f"[GROQ] Could not init evaluator: {e}")
    return None