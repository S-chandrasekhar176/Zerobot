# -*- coding: utf-8 -*-
"""
ZeroBot G2 — Groq Brain (LLM Central Intelligence Layer)  [G2 UPGRADE]
=======================================================================
G2 CHANGES vs G1:
  [G2-B1] Session-refresh retry — on 403 the client is re-instantiated with
           a fresh session and the call is retried once.  Fixes the
           "Access denied — check network settings" error seen in G1 logs
           which was caused by a stale underlying HTTP session.
  [G2-B2] OpenRouter fallback provider — if Groq fails after retry, the
           same prompt is forwarded to api.openrouter.ai using the
           OPENROUTER_API_KEY env var (set OPENROUTER_API_KEY in .env).
           Both providers use compatible OpenAI-style chat completions.
           Falls back gracefully to the local heuristic if both fail.
  [G2-B3] Last-good macro cache — the most recent successful SessionBrief
           is persisted to disk (session_brief_cache.json).  On startup,
           if both providers fail the cached brief from the previous session
           is returned so the engine has better context than a blank slate.
  [G2-B4] Connectivity probe on init — fast HEAD request to api.groq.com
           before the first real call.  Result is logged clearly so the user
           knows immediately whether the issue is DNS/firewall vs API key.

WHAT GROQ BRAIN DOES:
  1. pre_session_brief()     — 9:00 IST: regime + sector focus + strategy weights
  2. trade_narrative()       — Rich per-trade reasoning for every signal
  3. portfolio_health()      — Real-time correlation & concentration risk check
  4. news_impact()           — Deep news assessment (beyond keyword scoring)
  5. exit_advice()           — Position exit guidance on news/drawdown events
  6. post_session_debrief()  — EOD lessons + grade + tomorrow watchlist

DESIGN PRINCIPLES:
  • ALL calls run via ThreadPoolExecutor — NEVER blocks asyncio event loop
  • 3.5s hard timeout — graceful fallback always available
  • 5-min TTL cache — same context = cached result, saves API budget
  • max 50 calls/session — well within free tier (14,400/day)
  • temperature=0 — deterministic, reproducible
  • All prompts output minified JSON — no fragile parsing
"""

import json, os, time, asyncio, hashlib, logging, datetime, functools
import concurrent.futures
from typing import Optional, Dict, Any, List
from pathlib import Path
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_BUDGET_PER_SESSION = 50
_CACHE_TTL_SECS     = 300       # 5 minutes
_TIMEOUT_S          = 3.5
_MODEL              = "llama-3.3-70b-versatile"
_EXECUTOR           = concurrent.futures.ThreadPoolExecutor(
                        max_workers=2, thread_name_prefix="groq_brain")

# [G2-B2] OpenRouter fallback
_OPENROUTER_URL   = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
# [G2-B3] Disk cache for last-good session brief
_BRIEF_CACHE_PATH = Path(__file__).parent.parent / "data" / "cache" / "session_brief_cache.json"

# ── Sector map ────────────────────────────────────────────────────────────────
_SECTOR_MAP: Dict[str, str] = {
    "RELIANCE.NS":"Energy",    "ONGC.NS":"Energy",      "NTPC.NS":"Utilities",
    "POWERGRID.NS":"Utilities","HDFCBANK.NS":"Banking",  "ICICIBANK.NS":"Banking",
    "AXISBANK.NS":"Banking",   "KOTAKBANK.NS":"Banking", "SBIN.NS":"Banking",
    "INDUSINDBK.NS":"Banking", "TCS.NS":"IT",            "INFY.NS":"IT",
    "WIPRO.NS":"IT",           "HCLTECH.NS":"IT",        "TECHM.NS":"IT",
    "BAJFINANCE.NS":"Finance", "BAJAJFINSV.NS":"Finance","MARUTI.NS":"Auto",
    "LT.NS":"Infrastructure",  "HINDUNILVR.NS":"FMCG",  "NESTLEIND.NS":"FMCG",
    "ITC.NS":"FMCG",           "ASIANPAINT.NS":"Consumer","TITAN.NS":"Consumer",
    "ULTRACEMCO.NS":"Cement",  "TATASTEEL.NS":"Steel",
}

# ── System prompts ────────────────────────────────────────────────────────────
_BRIEF_PROMPT = """You are ZEROBOT-BRAIN, an elite NSE India quant trader with 20 years experience.
Given pre-market context, provide a structured session brief. Think like the top 1% of traders.
Output ONLY minified JSON, no markdown, no preamble:
{"regime":"BULLISH|BEARISH|SIDEWAYS|VOLATILE","bias":"LONG_BIAS|SHORT_BIAS|NEUTRAL|RISK_OFF",
"vix_comment":"one sentence","sector_focus":["up to 3 sectors"],"sectors_avoid":["up to 2"],
"key_risks":["up to 3"],"strategy_weights":{"Momentum":0.8,"MeanReversion":0.6,"VWAP":0.7,
"Breakout":0.5,"StatArb":0.4},"max_positions_today":8,"reasoning":"2-3 sentence overview"}"""

_NARRATIVE_PROMPT = """You are ZEROBOT-BRAIN, the world's best quant trader explaining a trade.
Be surgical, factual. State WHY this trade has edge. Output ONLY minified JSON:
{"headline":"≤15 words why this trade","detail":"2-3 sentences: signal+ML+news+risk",
"conviction":"HIGH|MEDIUM|LOW","risk_factors":["up to 3 specific risks"]}"""

_PORTFOLIO_PROMPT = """You are ZEROBOT-BRAIN assessing portfolio risk for NSE India algo trading.
Identify dangerous concentration/correlation. Output ONLY minified JSON:
{"health":"HEALTHY|ELEVATED_RISK|CRITICAL","concentration_score":0.3,
"warnings":["up to 4 specific warnings"],"suggested_actions":["up to 3 actions"],
"max_new_positions":5}"""

_NEWS_PROMPT = """You are ZEROBOT-BRAIN assessing real-time news impact on a specific NSE stock.
Consider company, sector, specific news. Output ONLY minified JSON:
{"impact_score":-0.5,"impact_label":"VERY_BULLISH|BULLISH|NEUTRAL|BEARISH|VERY_BEARISH",
"trade_implication":"BUY_SIGNAL|HOLD|AVOID|SELL_SIGNAL","confidence":0.8,"reasoning":"1-2 sentences"}"""

_EXIT_PROMPT = """You are ZEROBOT-BRAIN advising on exiting an NSE position.
Assess technical situation, news, and risk vs remaining upside. Output ONLY minified JSON:
{"should_exit":false,"urgency":"IMMEDIATE|SOON|HOLD|HOLD_STRONG",
"reasoning":"1-2 sentences","suggested_exit_pct":0.0}"""

_DEBRIEF_PROMPT = """You are ZEROBOT-BRAIN conducting EOD trading debrief.
Extract actionable lessons from session stats. Output ONLY minified JSON:
{"session_grade":"A|B|C|D","best_strategy":"name","worst_strategy":"name",
"key_lessons":["up to 3 lessons"],"tomorrow_focus":["up to 3 focus areas"],
"risk_assessment":"LOW|MEDIUM|HIGH"}"""


# ── Result dataclasses ────────────────────────────────────────────────────────
@dataclass
class SessionBrief:
    regime:               str
    bias:                 str
    vix_comment:          str
    sector_focus:         List[str]
    sectors_avoid:        List[str]
    key_risks:            List[str]
    strategy_weights:     Dict[str, float]
    max_positions_today:  int
    reasoning:            str
    source:               str = "groq"
    latency_ms:           int = 0

@dataclass
class TradeNarrative:
    headline:     str
    detail:       str
    conviction:   str
    risk_factors: List[str]
    source:       str = "groq"
    latency_ms:   int = 0

@dataclass
class PortfolioHealth:
    health:               str
    concentration_score:  float
    warnings:             List[str]
    suggested_actions:    List[str]
    max_new_positions:    int
    source:               str = "groq"
    latency_ms:           int = 0

@dataclass
class NewsImpact:
    symbol:             str
    impact_score:       float
    impact_label:       str
    trade_implication:  str
    confidence:         float
    reasoning:          str
    source:             str = "groq"
    latency_ms:         int = 0

@dataclass
class ExitAdvice:
    should_exit:         bool
    urgency:             str
    reasoning:           str
    suggested_exit_pct:  float
    source:              str = "groq"
    latency_ms:          int = 0


# ══════════════════════════════════════════════════════════════════════════════
# GroqBrain — the central intelligence
# ══════════════════════════════════════════════════════════════════════════════
class GroqBrain:
    """
    Central LLM intelligence for ZeroBot G1.
    Singleton — access via module-level `groq_brain`.
    """

    def __init__(self):
        self._client        = None
        self._available     = False
        self._api_key       = ""
        self._cache: Dict[str, tuple] = {}
        self._session_calls = 0
        self._session_date  = ""
        self._total_calls   = 0
        self._total_latency = 0
        self._call_log: List[Dict] = []

    # ── Init ──────────────────────────────────────────────────────────────────
    def init(self, api_key: str) -> bool:
        self._api_key = api_key
        # [G2-B4] Connectivity probe
        self._probe_connectivity()
        try:
            from groq import Groq
            self._client    = Groq(api_key=api_key)
            self._available = True
            log.info(f"[BRAIN] ✅ Groq Brain online — {_MODEL}")
            return True
        except ImportError:
            log.warning("[BRAIN] groq package not installed — pip install groq")
        except Exception as e:
            log.warning(f"[BRAIN] Init failed: {e}")
        self._available = False
        return False

    def _probe_connectivity(self):
        """[G2-B4] Fast connectivity check to api.groq.com.
        NOTE: 403/401/404 HTTP errors mean the endpoint IS reachable —
        they are auth rejections, not network failures.
        Only warn on actual network errors: DNS failure, timeout, connection refused.
        """
        import urllib.request, urllib.error, socket
        try:
            urllib.request.urlopen("https://api.groq.com", timeout=3)
            log.info("[BRAIN] Groq endpoint reachable ✅")
        except urllib.error.HTTPError:
            # HTTP error = server responded = endpoint IS reachable
            log.info("[BRAIN] Groq endpoint reachable ✅ (auth required — expected)")
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            # These are genuine network failures
            log.warning(f"[BRAIN] ⚠️  Groq endpoint NOT reachable (network error): {e}")
            log.warning("[BRAIN] Check VPN/firewall — using local + OpenRouter fallback")

    def init_openrouter(self, api_key: str):
        """[G2-B2] Register OpenRouter as secondary LLM provider."""
        self._openrouter_key = api_key
        log.info("[BRAIN] OpenRouter fallback registered")

    def _call_openrouter_sync(self, system_prompt: str, payload: dict) -> tuple:
        """[G2-B2] OpenRouter fallback — identical prompt format."""
        import urllib.request, urllib.error
        t0 = time.time()
        key = getattr(self, "_openrouter_key", "") or os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            return None, 0
        body = json.dumps({
            "model": _OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": json.dumps(payload)},
            ],
            "max_tokens": 300, "temperature": 0,
        }).encode()
        req = urllib.request.Request(
            _OPENROUTER_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
                "HTTP-Referer":  "https://zerobot.local",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S + 2) as resp:
                data = json.loads(resp.read().decode())
            raw = data["choices"][0]["message"]["content"].strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            result = json.loads(raw)
            latency = int((time.time() - t0) * 1000)
            log.info(f"[BRAIN] OpenRouter fallback OK — {latency}ms")
            return result, latency
        except Exception as e:
            log.warning(f"[BRAIN] OpenRouter fallback failed: {e}")
            return None, int((time.time() - t0) * 1000)

    @property
    def is_available(self) -> bool:
        return self._available and bool(self._api_key)

    # ── Budget & cache ────────────────────────────────────────────────────────
    def _reset_daily(self):
        today = datetime.date.today().isoformat()
        if self._session_date != today:
            self._session_date  = today
            self._session_calls = 0

    def _over_budget(self) -> bool:
        self._reset_daily()
        return self._session_calls >= _BUDGET_PER_SESSION

    def _cache_key(self, fn: str, payload: dict) -> str:
        raw = fn + json.dumps(payload, sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()

    def _cache_get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            result, ts = self._cache[key]
            if time.time() - ts < _CACHE_TTL_SECS:
                return result
            del self._cache[key]
        return None

    def _cache_set(self, key: str, result: Any):
        self._cache[key] = (result, time.time())
        if len(self._cache) > 300:
            oldest = sorted(self._cache, key=lambda k: self._cache[k][1])[:100]
            for k in oldest: del self._cache[k]

    # ── Core call ─────────────────────────────────────────────────────────────
    def _call_sync(self, system_prompt: str, payload: dict) -> tuple:
        """[G2-B1] Blocking Groq call with session-refresh retry."""
        t0 = time.time()
        for attempt in range(1, 3):   # max 2 attempts
            try:
                resp = self._client.chat.completions.create(
                    model=_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": json.dumps(payload)},
                    ],
                    max_tokens=300, temperature=0, timeout=_TIMEOUT_S,
                )
                raw = resp.choices[0].message.content.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                data = json.loads(raw)
                latency = int((time.time() - t0) * 1000)
                self._session_calls += 1
                self._total_calls   += 1
                self._total_latency += latency
                return data, latency
            except json.JSONDecodeError as e:
                log.warning(f"[BRAIN] JSON parse error: {e}")
                return None, int((time.time() - t0) * 1000)
            except Exception as e:
                err_str = str(e)
                if "403" in err_str or "Access denied" in err_str:
                    if attempt == 1:
                        # [G2-B1] Session-refresh retry
                        log.warning("[BRAIN] 403 — refreshing Groq session and retrying")
                        try:
                            from groq import Groq
                            self._client = Groq(api_key=self._api_key)
                        except Exception:
                            pass
                        continue
                log.warning(f"[BRAIN] Call error ({int((time.time()-t0)*1000)}ms): {e}")
                break

        # [G2-B2] OpenRouter fallback
        data, latency = self._call_openrouter_sync(system_prompt, payload)
        if data:
            self._total_calls   += 1
            self._total_latency += latency
        return data, latency

    async def _call(self, fn_name: str, system_prompt: str, payload: dict) -> tuple:
        """Non-blocking async wrapper."""
        if not self.is_available or self._over_budget():
            return None, 0
        key = self._cache_key(fn_name, payload)
        cached = self._cache_get(key)
        if cached is not None:
            log.debug(f"[BRAIN] Cache hit: {fn_name}")
            return cached, 0
        loop = asyncio.get_event_loop()
        fn   = functools.partial(self._call_sync, system_prompt, payload)
        data, latency = await loop.run_in_executor(_EXECUTOR, fn)
        if data:
            self._cache_set(key, data)
            self._call_log.append({
                "time": datetime.datetime.now().strftime("%H:%M:%S"),
                "fn": fn_name, "latency": latency, "ok": True,
            })
            if len(self._call_log) > 100: self._call_log.pop(0)
            log.info(f"[BRAIN] {fn_name} → {latency}ms | session={self._session_calls}/{_BUDGET_PER_SESSION}")
        return data, latency

    # ══════════════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ══════════════════════════════════════════════════════════════════════════

    async def pre_session_brief(
        self,
        vix: float,
        nifty_change_pct: float = 0.0,
        nifty_level: float = 22000,
        fii_net_inr: float = 0.0,
        global_cues: str = "",
        rbi_event: bool = False,
    ) -> SessionBrief:
        """Pre-market brief: regime, sector focus, strategy weights."""
        payload = {
            "vix": round(vix, 2),
            "nifty_change_pct": round(nifty_change_pct, 2),
            "nifty_level": round(nifty_level),
            "fii_net_inr_cr": round(fii_net_inr, 1),
            "global_cues": global_cues or "No major overnight events.",
            "rbi_event_today": rbi_event,
            "date": datetime.date.today().isoformat(),
        }
        data, latency = await self._call("pre_session_brief", _BRIEF_PROMPT, payload)
        if data:
            brief = SessionBrief(
                regime=data.get("regime","SIDEWAYS"),
                bias=data.get("bias","NEUTRAL"),
                vix_comment=data.get("vix_comment",""),
                sector_focus=data.get("sector_focus",[]),
                sectors_avoid=data.get("sectors_avoid",[]),
                key_risks=data.get("key_risks",[]),
                strategy_weights=data.get("strategy_weights",{}),
                max_positions_today=int(data.get("max_positions_today",6)),
                reasoning=data.get("reasoning",""),
                source="groq", latency_ms=latency,
            )
            self._save_brief_cache(brief)   # [G2-B3]
            return brief
        # [G2-B3] Try last-good cached brief before bare heuristic
        cached_brief = self._load_brief_cache()
        if cached_brief:
            cached_brief.source     = "disk_cache"
            cached_brief.latency_ms = 0
            log.info("[BRAIN] Serving last-good session brief from disk cache")
            return cached_brief

        # Bare heuristic fallback
        regime = "VOLATILE" if vix>22 else ("BULLISH" if nifty_change_pct>0.5 else
                  "BEARISH" if nifty_change_pct<-0.5 else "SIDEWAYS")
        return SessionBrief(
            regime=regime, bias="RISK_OFF" if vix>22 else "NEUTRAL",
            vix_comment=f"VIX={vix:.1f}",
            sector_focus=[], sectors_avoid=[],
            key_risks=["High VIX — reduce size"] if vix>22 else [],
            strategy_weights={},
            max_positions_today=4 if vix>22 else 8,
            reasoning="Local fallback — Groq unavailable",
            source="fallback", latency_ms=0,
        )

    async def trade_narrative(
        self,
        symbol: str,
        side: str,
        confidence: float,
        strategy: str,
        cmp: float,
        stop_loss: float = 0,
        target: float = 0,
        vix: float = 16.0,
        sentiment_score: float = 0.0,
        news_headline: str = "",
        groq_gate_reasoning: str = "",
    ) -> TradeNarrative:
        """Rich per-trade narrative — called after risk gates pass."""
        rr = round(abs(target-cmp)/max(abs(cmp-stop_loss),0.01), 2) if stop_loss and cmp else 0
        payload = {
            "symbol": symbol.replace(".NS",""),
            "sector": _SECTOR_MAP.get(symbol,"Diversified"),
            "side": side, "strategy": strategy,
            "confidence_pct": round(confidence,1),
            "cmp": round(cmp,2), "stop_loss": round(stop_loss,2), "target": round(target,2),
            "rr_ratio": rr, "vix": round(vix,1),
            "sentiment_score": round(sentiment_score,2),
            "news_headline": news_headline[:100] if news_headline else "None",
            "gate_reasoning": groq_gate_reasoning[:60] if groq_gate_reasoning else "",
        }
        data, latency = await self._call("trade_narrative", _NARRATIVE_PROMPT, payload)
        if data:
            return TradeNarrative(
                headline=data.get("headline",f"{side} {symbol}"),
                detail=data.get("detail",""),
                conviction=data.get("conviction","MEDIUM"),
                risk_factors=data.get("risk_factors",[]),
                source="groq", latency_ms=latency,
            )
        sym = symbol.replace(".NS","")
        conv = "HIGH" if confidence>=75 else "MEDIUM" if confidence>=65 else "LOW"
        return TradeNarrative(
            headline=f"{'🟢' if side=='BUY' else '🔴'} {sym} | {strategy} | {confidence:.0f}% conf",
            detail=f"{strategy} on {sym}. ML={confidence:.1f}%. VIX={vix:.1f}. R:R={rr:.1f}:1.",
            conviction=conv, risk_factors=[],
            source="fallback", latency_ms=0,
        )

    async def portfolio_health(
        self,
        open_positions: Dict[str, dict],
        capital: float,
        daily_pnl: float,
        max_allowed: int = 10,
    ) -> PortfolioHealth:
        """Assess portfolio concentration and correlation risk."""
        if not open_positions:
            return PortfolioHealth(health="HEALTHY", concentration_score=0.0,
                warnings=[], suggested_actions=[], max_new_positions=max_allowed, source="fallback")

        from collections import Counter
        sector_counts = Counter(_SECTOR_MAP.get(s,"Other") for s in open_positions)
        pos_list = []
        for sym, pos in list(open_positions.items())[:15]:
            pos_list.append({
                "symbol": sym.replace(".NS",""),
                "sector": _SECTOR_MAP.get(sym,"Other"),
                "side": pos.get("side","LONG"),
                "pct_of_capital": round(pos.get("position_inr",0)/max(capital,1)*100,1),
            })
        payload = {
            "positions": pos_list,
            "total_positions": len(open_positions),
            "sector_counts": dict(sector_counts),
            "capital_inr": round(capital),
            "daily_pnl_pct": round(daily_pnl/max(capital,1)*100,2),
            "max_allowed": max_allowed,
        }
        data, latency = await self._call("portfolio_health", _PORTFOLIO_PROMPT, payload)
        if data:
            return PortfolioHealth(
                health=data.get("health","HEALTHY"),
                concentration_score=float(data.get("concentration_score",0.3)),
                warnings=data.get("warnings",[]),
                suggested_actions=data.get("suggested_actions",[]),
                max_new_positions=int(data.get("max_new_positions",max_allowed)),
                source="groq", latency_ms=latency,
            )
        # Fallback
        max_s = max(sector_counts.values()) if sector_counts else 0
        conc  = max_s / max(len(open_positions),1)
        health= "CRITICAL" if conc>0.6 else "ELEVATED_RISK" if conc>0.4 else "HEALTHY"
        return PortfolioHealth(
            health=health, concentration_score=round(conc,2),
            warnings=[f"High {k} concentration ({v} positions)" for k,v in sector_counts.items() if v>=3],
            suggested_actions=[], max_new_positions=max(0,max_allowed-len(open_positions)),
            source="fallback",
        )

    async def news_impact(self, symbol: str, headline: str, current_side: str="NONE") -> NewsImpact:
        """Deep news impact assessment — beyond keyword scoring."""
        if not headline:
            return NewsImpact(symbol=symbol, impact_score=0.0, impact_label="NEUTRAL",
                trade_implication="HOLD", confidence=0.3, reasoning="No headline", source="fallback")
        payload = {
            "symbol": symbol.replace(".NS",""),
            "sector": _SECTOR_MAP.get(symbol,"Diversified"),
            "headline": headline[:150], "current_side": current_side,
        }
        data, latency = await self._call("news_impact", _NEWS_PROMPT, payload)
        if data:
            return NewsImpact(
                symbol=symbol,
                impact_score=float(data.get("impact_score",0.0)),
                impact_label=data.get("impact_label","NEUTRAL"),
                trade_implication=data.get("trade_implication","HOLD"),
                confidence=float(data.get("confidence",0.5)),
                reasoning=data.get("reasoning",""),
                source="groq", latency_ms=latency,
            )
        return NewsImpact(symbol=symbol, impact_score=0.0, impact_label="NEUTRAL",
            trade_implication="HOLD", confidence=0.3, reasoning="Groq unavailable",
            source="fallback")

    async def exit_advice(
        self, symbol: str, side: str, entry: float, current: float,
        sl: float, target: float, pnl_inr: float, mins: int,
        vix: float=16.0, news: str="",
    ) -> ExitAdvice:
        """Should we exit an open position early?"""
        pnl_pct = (current-entry)/entry*100*(1 if side=="BUY" else -1)
        payload = {
            "symbol": symbol.replace(".NS",""),
            "sector": _SECTOR_MAP.get(symbol,"Diversified"),
            "side": side, "pnl_pct": round(pnl_pct,2),
            "sl_dist_pct": round(abs(current-sl)/entry*100,2),
            "tgt_dist_pct": round(abs(target-current)/entry*100,2),
            "pnl_inr": round(pnl_inr,2), "mins_held": mins,
            "vix": round(vix,1), "news": news[:100] if news else "None",
        }
        data, latency = await self._call("exit_advice", _EXIT_PROMPT, payload)
        if data:
            return ExitAdvice(
                should_exit=bool(data.get("should_exit",False)),
                urgency=data.get("urgency","HOLD"),
                reasoning=data.get("reasoning",""),
                suggested_exit_pct=float(data.get("suggested_exit_pct",0.0)),
                source="groq", latency_ms=latency,
            )
        return ExitAdvice(should_exit=False, urgency="HOLD",
            reasoning="Groq unavailable", suggested_exit_pct=0.0, source="fallback")

    async def post_session_debrief(
        self, trades: int, wins: int, losses: int, pnl: float,
        best: str, worst: str, strategies: List[str], open_pos: int,
    ) -> Dict[str, Any]:
        """EOD debrief — lessons, grade, tomorrow focus."""
        payload = {
            "trades": trades, "wins": wins, "losses": losses,
            "win_rate_pct": round(wins/max(trades,1)*100,1),
            "daily_pnl_inr": round(pnl,2),
            "best_trade": best, "worst_trade": worst,
            "strategies_used": strategies, "open_positions_eod": open_pos,
            "date": datetime.date.today().isoformat(),
        }
        data, latency = await self._call("post_session_debrief", _DEBRIEF_PROMPT, payload)
        if data:
            data["source"]="groq"; data["latency_ms"]=latency
            log.info(f"[BRAIN] Debrief: grade={data.get('session_grade','?')} {latency}ms")
            return data
        return {"session_grade":"B","best_strategy":"Unknown","worst_strategy":"Unknown",
                "key_lessons":["Groq unavailable — review manually"],
                "tomorrow_focus":[],"risk_assessment":"MEDIUM","source":"fallback"}

    # [G2-B3] Brief disk cache helpers
    def _save_brief_cache(self, brief):
        try:
            _BRIEF_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            import dataclasses
            with open(_BRIEF_CACHE_PATH, "w") as f:
                json.dump({**dataclasses.asdict(brief),
                           "saved_at": datetime.datetime.now().isoformat()}, f)
        except Exception:
            pass

    def _load_brief_cache(self):
        try:
            if not _BRIEF_CACHE_PATH.exists():
                return None
            with open(_BRIEF_CACHE_PATH) as f:
                d = json.load(f)
            # Only use if < 24h old
            saved_at = datetime.datetime.fromisoformat(d.get("saved_at","2000-01-01"))
            if (datetime.datetime.now() - saved_at).total_seconds() > 86400:
                return None
            return SessionBrief(
                regime=d.get("regime","SIDEWAYS"),
                bias=d.get("bias","NEUTRAL"),
                vix_comment=d.get("vix_comment",""),
                sector_focus=d.get("sector_focus",[]),
                sectors_avoid=d.get("sectors_avoid",[]),
                key_risks=d.get("key_risks",[]),
                strategy_weights=d.get("strategy_weights",{}),
                max_positions_today=int(d.get("max_positions_today",6)),
                reasoning=d.get("reasoning","") + " [from cache]",
            )
        except Exception:
            return None

    def get_stats(self) -> Dict[str, Any]:
        avg = int(self._total_latency/self._total_calls) if self._total_calls else 0
        return {
            "available": self.is_available,
            "model": _MODEL,
            "total_calls": self._total_calls,
            "session_calls": self._session_calls,
            "budget_remaining": max(0, _BUDGET_PER_SESSION - self._session_calls),
            "avg_latency_ms": avg,
            "cache_size": len(self._cache),
            "recent_calls": list(reversed(self._call_log[-20:])),
        }


# ── Singleton ─────────────────────────────────────────────────────────────────
groq_brain = GroqBrain()

def init_groq_brain(api_key: str) -> bool:
    """Initialize singleton. Called from main.py at startup."""
    return groq_brain.init(api_key)
