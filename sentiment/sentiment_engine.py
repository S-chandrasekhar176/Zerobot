# -*- coding: utf-8 -*-
"""
ZeroBot v2 -- Sentiment Engine [patch11 FIXED]
==============================================
Two-layer sentiment scoring:

Layer 1 (always active): Financial keyword scorer
  - Hand-curated bullish/bearish word lists specific to Indian markets
  - Word-boundary regex matching: "ban" no longer hits "Bandhan Bank"
  - Score: -1.0 to +1.0 in <1ms per headline, zero dependencies

Layer 2 (optional): FinBERT
  - ProsusAI/finbert -- BERT model fine-tuned on financial text
  - Enable: set ZEROBOT_USE_FINBERT=1 in .env
  - Confidence scale configurable via ZEROBOT_FINBERT_SCALE (default 0.8)

FIX LOG:
  [FIX-2] Word-boundary regex: "ban" no longer matches "bandhan bank"
  [FIX-2] "reduce" removed -- "reduced npa" is POSITIVE, not bearish
  [FIX-6] Canonical single version -- old duplicate removed
  [FIX-7] FinBERT scale configurable via ZEROBOT_FINBERT_SCALE env var
  [FIX-8] classify() has 5 bands (added MILDLY BULLISH / MILDLY BEARISH)
  [NEW]   score_batch() added for efficient burst scoring
"""

import os
import re
from typing import Dict, List, Tuple

from core.logger import log


# ── Keyword lists ─────────────────────────────────────────────────────────────
BULLISH_KEYWORDS: Dict[str, float] = {
    "beats estimates":       2.0,
    "profit rises":          1.5,
    "profit jumps":          1.5,
    "revenue beats":         1.5,
    "strong results":        1.5,
    "record profit":         2.0,
    "record revenue":        2.0,
    "dividend declared":     1.0,
    "dividend increased":    1.2,
    "special dividend":      1.5,
    "earnings beat":         1.8,
    "net profit rises":      1.5,
    "net profit jumps":      1.5,
    "margin expansion":      1.2,
    "nim expansion":         1.2,
    "loan growth":           1.0,
    "buyback":               1.5,
    "share buyback":         1.8,
    "bonus shares":          1.2,
    "new order":             1.0,
    "order win":             1.5,
    "large order":           1.5,
    "deal wins":             1.5,
    "upgrade":               1.5,
    "target raised":         1.2,
    "overweight":            1.0,
    "buy rating":            1.0,
    "strong buy":            1.5,
    "outperform":            1.0,
    "accumulate":            0.8,
    "fii buying":            1.5,
    "fii inflow":            1.5,
    "fii net buyer":         1.5,
    "dii buying":            1.0,
    "rate cut":              1.5,
    "repo rate cut":         1.8,
    "liquidity boost":       1.2,
    "inflation cools":       1.0,
    "market rally":          1.0,
    "52-week high":          1.2,
    "all-time high":         1.5,
    "breakout":              1.0,
    "acquisition":           0.8,
    "merger approved":       1.2,
    "capacity expansion":    1.0,
    "gst collection record": 1.2,
    # ── Asset quality improvement (banking) ──────────────────────────────────
    # FIX: These were removed from BEARISH but never added to BULLISH.
    # "SBI reduced NPA ratio" is clearly positive news for a bank.
    "reduced npa":          1.2,
    "npa reduction":        1.2,
    "npa falls":            1.5,
    "npa declines":         1.5,
    "npa improves":         1.5,
    "npa down":             1.2,
    "gross npa falls":      1.5,
    "net npa falls":        1.5,
    "bad loans fall":       1.5,
    "bad loan recovery":    1.2,
    "slippages decline":    1.2,
    "asset quality improves": 1.5,
    "provision coverage":   1.0,
    "credit cost falls":    1.2,
}

BEARISH_KEYWORDS: Dict[str, float] = {
    "misses estimates":     -2.0,
    "profit falls":         -1.5,
    "profit declines":      -1.5,
    "revenue misses":       -1.5,
    "weak results":         -1.5,
    "profit slips":         -1.2,
    "net loss":             -2.0,
    "net profit falls":     -1.5,
    "margin compression":   -1.2,
    "npa rises":            -1.8,
    "bad loans rise":       -1.5,
    "slippages rise":       -1.5,
    "dividend cut":         -1.5,
    "dividend scrapped":    -2.0,
    "sebi ban":             -2.5,
    "sebi order":           -2.0,
    "sebi notice":          -1.5,
    "ed raid":              -2.5,
    "income tax raid":      -2.0,
    "fraud":                -2.5,
    "scam":                 -2.5,
    "investigated":         -2.0,
    "criminal charges":     -2.5,
    "arrested":             -2.5,
    "penalty":              -1.5,
    "fine imposed":         -1.5,
    "regulatory action":    -2.0,
    "show cause notice":    -1.5,
    "rbi action":           -1.5,
    "suspension":           -2.0,
    "suspended":            -2.0,
    # FIX-2: "ban" removed -- matched "bandhan bank" falsely
    # FIX-2: "reduce" removed -- "reduced npa" is positive news
    "default":              -2.5,
    "debt default":         -2.5,
    "bankruptcy":           -2.5,
    "insolvency":           -2.5,
    "nclt":                 -1.5,
    "credit downgrade":     -2.0,
    "rating downgrade":     -2.0,
    "downgrade":            -1.5,
    "target cut":           -1.2,
    "underperform":         -1.0,
    "sell rating":          -1.2,
    "underweight":          -1.0,
    "fii selling":          -1.5,
    "fii outflow":          -1.5,
    "fii net seller":       -1.5,
    "rate hike":            -1.5,
    "repo rate hike":       -1.8,
    "inflation surges":     -1.2,
    "recession":            -2.0,
    "market crash":         -2.0,
    "market falls":         -1.0,
    "52-week low":          -1.2,
    "lower circuit":        -2.0,
    "panic selling":        -1.5,
    "breakdown":            -1.2,
    "circuit breaker":      -1.5,
}

# Hard block keywords -- always block BUY regardless of score
# Single definition here; feed_aggregator imports from here (FIX-7 deduplication)
HARD_BLOCK_KEYWORDS: List[str] = [
    "fraud", "sebi ban", "sebi order", "arrested", "ed raid",
    "income tax raid", "default", "bankruptcy", "insolvency",
    "delisted", "suspended", "criminal charges", "investigated",
    "penalty", "criminal", "show cause notice",
]

_MAX_SCORE = 5.0

# FIX-2: Pre-compile word-boundary patterns to prevent partial matches
# "ban" won't match "bandhan", "reduce" won't match "reduced"
_BULLISH_PATTERNS: List[Tuple[re.Pattern, float]] = [
    (re.compile(r'\b' + re.escape(kw) + r'\b'), w)
    for kw, w in BULLISH_KEYWORDS.items()
]
_BEARISH_PATTERNS: List[Tuple[re.Pattern, float]] = [
    (re.compile(r'\b' + re.escape(kw) + r'\b'), w)
    for kw, w in BEARISH_KEYWORDS.items()
]
_HARD_BLOCK_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r'\b' + re.escape(kw) + r'\b'), kw)
    for kw in HARD_BLOCK_KEYWORDS
]


class SentimentEngine:
    """
    Scores financial headlines on a -1.0 to +1.0 scale.
    Uses word-boundary keyword matching by default.
    Upgrades to FinBERT if ZEROBOT_USE_FINBERT=1 is set.
    """

    def __init__(self):
        self._finbert        = None
        self._finbert_ready  = False
        self._use_finbert    = os.getenv("ZEROBOT_USE_FINBERT", "0") == "1"
        # FIX-7: FinBERT confidence scale is now configurable via env var
        self._finbert_scale  = float(os.getenv("ZEROBOT_FINBERT_SCALE", "0.8"))
        if self._use_finbert:
            self._try_load_finbert()

    def _try_load_finbert(self):
        try:
            from transformers import pipeline
            log.info(f"Loading FinBERT (~400MB one-time download) scale={self._finbert_scale}...")
            self._finbert = pipeline(
                "sentiment-analysis",
                model="ProsusAI/finbert",
                tokenizer="ProsusAI/finbert",
                max_length=512,
                truncation=True,
            )
            self._finbert_ready = True
            log.info("FinBERT ready")
        except Exception as e:
            log.info(f"FinBERT unavailable ({e}) - using keyword scorer")

    def score(self, text: str) -> float:
        """Score a headline. Returns float in [-1.0, +1.0]."""
        if self._finbert_ready and self._finbert:
            return self._score_finbert(text)
        return self._score_keywords(text)

    def score_batch(self, texts: List[str]) -> List[float]:
        """Score multiple headlines efficiently (FinBERT batches internally)."""
        if self._finbert_ready and self._finbert and len(texts) > 1:
            try:
                results = self._finbert([t[:512] for t in texts], batch_size=8)
                scores = []
                for r in results:
                    label = r["label"].lower()
                    conf  = r["score"]
                    if label == "positive":
                        scores.append(round(conf * self._finbert_scale, 3))
                    elif label == "negative":
                        scores.append(round(-conf * self._finbert_scale, 3))
                    else:
                        scores.append(0.0)
                return scores
            except Exception as e:
                log.debug(f"FinBERT batch error: {e}")
        return [self._score_keywords(t) for t in texts]

    def _score_keywords(self, text: str) -> float:
        """FIX-2: Word-boundary regex prevents "ban"->bandhan, "reduce"->reduced npa."""
        t   = text.lower()
        raw = 0.0
        for pattern, weight in _BULLISH_PATTERNS:
            if pattern.search(t):
                raw += weight
        for pattern, weight in _BEARISH_PATTERNS:
            if pattern.search(t):
                raw += weight
        return round(max(-1.0, min(1.0, raw / _MAX_SCORE)), 3)

    def _score_finbert(self, text: str) -> float:
        try:
            r = self._finbert(text[:512])[0]
            l = r["label"].lower()
            c = r["score"]
            if l == "positive":  return round( c * self._finbert_scale, 3)
            if l == "negative":  return round(-c * self._finbert_scale, 3)
            return 0.0
        except Exception:
            return self._score_keywords(text)

    def classify(self, score: float) -> str:
        """FIX-8: 5-band classification (was 4-band, missing MILDLY BULLISH/BEARISH)."""
        if score >= 0.5:    return "STRONGLY BULLISH"
        if score >= 0.35:   return "BULLISH"
        if score >= 0.2:    return "MILDLY BULLISH"
        if score <= -0.5:   return "STRONGLY BEARISH"
        if score <= -0.35:  return "BEARISH"
        if score <= -0.2:   return "MILDLY BEARISH"
        return "NEUTRAL"

    def is_hard_block(self, text: str) -> Tuple[bool, str]:
        """FIX-2: Word-boundary match -- 'ban' won't trigger on 'Bandhan Bank'."""
        t = text.lower()
        for pattern, kw in _HARD_BLOCK_PATTERNS:
            if pattern.search(t):
                return True, kw
        return False, ""
