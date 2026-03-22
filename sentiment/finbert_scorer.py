# -*- coding: utf-8 -*-
"""
ZeroBot Pro — FinBERT Sentiment Scorer (Patch 5 NEW)
═════════════════════════════════════════════════════
Financial BERT model fine-tuned on 10,000+ financial news headlines.
Classifies text as: POSITIVE | NEGATIVE | NEUTRAL
Returns probability score + direction signal.

Two modes:
  FAST (default): keyword scorer — zero dependencies, instant
  FINBERT:        transformer model — pip install transformers torch
                  Set use_finbert: true in settings.yaml

The scorer is used for:
  1. News headline filtering (NSE announcements, MoneyControl, ET)
  2. Adjusting ML signal confidence (+/- 10 based on news sentiment)
  3. Pre-market scan: which stocks have strong positive/negative news today

Auto-fallback: if FinBERT not installed → uses keyword scorer silently.
"""
from typing import List, Tuple, Dict, Optional
from dataclasses import dataclass
from core.logger import log

try:
    from core.config import cfg
    _USE_FINBERT = getattr(cfg, "use_finbert", False)
except Exception:
    _USE_FINBERT = False


@dataclass
class SentimentResult:
    text:       str
    label:      str         # "positive" | "negative" | "neutral"
    score:      float       # 0.0 to 1.0
    direction:  int         # +1, -1, 0
    confidence: float       # 0-100 confidence modifier for ML signal
    model:      str         # "finbert" | "keyword"


# ── Keyword fallback (fast, zero dependencies) ────────────────────────────────
_POS_WORDS = {
    "surge": 0.8, "rally": 0.8, "gain": 0.7, "rise": 0.7, "jump": 0.7,
    "record": 0.9, "profit": 0.8, "beat": 0.7, "outperform": 0.9,
    "upgrade": 0.8, "buy": 0.7, "target": 0.5, "growth": 0.6,
    "strong": 0.7, "positive": 0.6, "recovery": 0.7, "bullish": 0.9,
    "acquisition": 0.5, "deal": 0.5, "expansion": 0.6, "dividend": 0.7,
    "order": 0.5, "contract": 0.5, "wins": 0.7, "breakout": 0.8,
    "revenue": 0.5, "earnings beat": 0.9, "guidance raised": 0.9,
}
_NEG_WORDS = {
    "fall": 0.8, "drop": 0.8, "decline": 0.7, "loss": 0.8, "miss": 0.8,
    "downgrade": 0.9, "sell": 0.6, "crash": 0.9, "plunge": 0.9,
    "concern": 0.6, "risk": 0.5, "weak": 0.7, "negative": 0.6,
    "probe": 0.8, "fraud": 1.0, "sebi": 0.6, "default": 0.9,
    "fine": 0.7, "penalty": 0.7, "strike": 0.6, "warning": 0.7,
    "downside": 0.7, "bearish": 0.9, "below estimate": 0.9,
    "guidance cut": 0.9, "layoffs": 0.7, "debt": 0.5, "write-off": 0.8,
}


def _keyword_score(text: str) -> SentimentResult:
    text_lower = text.lower()
    pos_score = sum(v for k, v in _POS_WORDS.items() if k in text_lower)
    neg_score = sum(v for k, v in _NEG_WORDS.items() if k in text_lower)
    total = pos_score + neg_score + 0.01

    if pos_score > neg_score and pos_score > 0.5:
        return SentimentResult(
            text=text, label="positive", score=round(pos_score/total, 3),
            direction=1, confidence=min(15.0, pos_score * 5), model="keyword"
        )
    elif neg_score > pos_score and neg_score > 0.5:
        return SentimentResult(
            text=text, label="negative", score=round(neg_score/total, 3),
            direction=-1, confidence=min(15.0, neg_score * 5), model="keyword"
        )
    return SentimentResult(
        text=text, label="neutral", score=0.5,
        direction=0, confidence=0.0, model="keyword"
    )


# ── FinBERT (transformer-based) ───────────────────────────────────────────────
_finbert_pipeline = None

def _load_finbert():
    """Lazy-load FinBERT model on first use."""
    global _finbert_pipeline
    if _finbert_pipeline is not None:
        return True
    try:
        from transformers import pipeline
        log.info("Loading FinBERT model (first run — downloads ~500MB)...")
        _finbert_pipeline = pipeline(
            "text-classification",
            model="ProsusAI/finbert",
            return_all_scores=True,
            device=-1,  # CPU (set device=0 for GPU)
            truncation=True,
            max_length=512,
        )
        log.info("✅ FinBERT model loaded")
        return True
    except ImportError:
        log.warning(
            "FinBERT not available. Install: pip install transformers torch\n"
            "Falling back to keyword scorer."
        )
        return False
    except Exception as e:
        log.warning(f"FinBERT load failed ({e}) — keyword scorer active")
        return False


def _finbert_score(text: str) -> SentimentResult:
    """Score using FinBERT transformer model."""
    if not _load_finbert() or _finbert_pipeline is None:
        return _keyword_score(text)
    try:
        results = _finbert_pipeline(text[:512])[0]
        scores  = {r["label"].lower(): r["score"] for r in results}
        label   = max(scores, key=scores.get)
        score   = scores[label]
        direction = 1 if label == "positive" else -1 if label == "negative" else 0
        # Confidence modifier: high-confidence positive/negative → ±10
        confidence = round((score - 0.5) * 20, 1) if label != "neutral" else 0.0
        return SentimentResult(
            text=text, label=label, score=round(score, 3),
            direction=direction, confidence=confidence, model="finbert"
        )
    except Exception as e:
        log.debug(f"FinBERT inference error: {e}")
        return _keyword_score(text)


# ── Public API ────────────────────────────────────────────────────────────────

def score(text: str) -> SentimentResult:
    """Score a single headline/text."""
    if _USE_FINBERT:
        return _finbert_score(text)
    return _keyword_score(text)


def score_batch(texts: List[str]) -> List[SentimentResult]:
    """Score multiple headlines. Batched for FinBERT efficiency."""
    if not texts:
        return []
    if _USE_FINBERT and _load_finbert() and _finbert_pipeline:
        try:
            results = []
            batch_size = 32
            for i in range(0, len(texts), batch_size):
                batch = texts[i:i+batch_size]
                raw = _finbert_pipeline([t[:512] for t in batch])
                for text, scores_list in zip(batch, raw):
                    scores = {r["label"].lower(): r["score"] for r in scores_list}
                    label  = max(scores, key=scores.get)
                    s      = scores[label]
                    dir_   = 1 if label == "positive" else -1 if label == "negative" else 0
                    conf   = round((s - 0.5) * 20, 1) if label != "neutral" else 0.0
                    results.append(SentimentResult(
                        text=text, label=label, score=round(s,3),
                        direction=dir_, confidence=conf, model="finbert"
                    ))
            return results
        except Exception as e:
            log.debug(f"FinBERT batch error: {e}")
    return [_keyword_score(t) for t in texts]


def _extract_text(item) -> str:
    """
    Safely extract a string from either a plain string or a dict headline.
    Handles the case where news_feed returns List[dict] with a 'title' key.
    """
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("title") or item.get("text") or item.get("headline") or "")
    return str(item)


def aggregate_symbol_sentiment(headlines, symbol: str) -> Dict:
    """
    Given a list of headlines (str OR dict with 'title'), return aggregate
    sentiment for a symbol. Used by engine to adjust ML signal confidence.
    """
    if not headlines:
        return {"direction": 0, "avg_score": 0.5, "confidence_modifier": 0.0, "count": 0}

    # Normalise: convert any dict items to plain strings before scoring
    texts = [_extract_text(h) for h in headlines if _extract_text(h).strip()]
    if not texts:
        return {"direction": 0, "avg_score": 0.5, "confidence_modifier": 0.0, "count": 0}

    results = score_batch(texts)
    if not results:  # P16: guard empty batch (zero-division fix)
        return {"direction": 0, "avg_score": 0.0, "confidence_modifier": 0.0, "count": 0}
    pos = sum(1 for r in results if r.direction == 1)
    neg = sum(1 for r in results if r.direction == -1)
    neu = sum(1 for r in results if r.direction == 0)
    avg_score = sum(r.score * r.direction for r in results) / len(results)
    conf_mod  = sum(r.confidence for r in results) / len(results)

    net_dir = 1 if pos > neg else -1 if neg > pos else 0
    log.debug(
        f"Sentiment [{symbol}]: {len(headlines)} headlines | "
        f"+{pos} -{neg} ~{neu} | modifier={conf_mod:+.1f}"
    )
    return {
        "direction":           net_dir,
        "avg_score":           round(avg_score, 3),
        "confidence_modifier": round(conf_mod, 2),
        "positive":            pos,
        "negative":            neg,
        "neutral":             neu,
        "count":               len(results),
        "model":               results[0].model if results else "none",
    }
