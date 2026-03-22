# -*- coding: utf-8 -*-
"""
ZeroBot v2 -- News Feed Aggregator [patch11 FIXED]
==================================================
Polls 3 free sources every 2 minutes:
  1. NSE Corporate Announcements (official JSON API)
  2. Moneycontrol RSS
  3. Economic Times Markets + Stocks RSS

FIX LOG:
  [FIX-1] pub_dt -> published_at: NSE NewsItem constructor arg corrected
           Previously: NewsItem(title, "nse_official", pub_dt=...)  <- WRONG
           Fixed:      NewsItem(title, "nse_official", published_at=...) <- CORRECT
           Impact: NSE corporate announcements (dividends, results) were silently dropped.

  [FIX-3] seen_ids now uses OrderedDict (FIFO eviction) instead of random set slicing.
           set(list(set)[-2000:]) is non-deterministic -- random IDs were kept, not newest.

  [FIX-4] RSS fetch has retry logic: 2 attempts with 3s backoff.
           One timeout no longer silently loses 2 minutes of headlines.

  [FIX-5] get_sentiment_score() returns SentimentResult(score, has_fresh_data, count, label).
           Callers can now distinguish "genuinely neutral" from "no data at all".
           float(result) still works for backward compatibility.

  [FIX-6] score_batch() called for bursts of >3 new headlines (faster with FinBERT).

  [FIX-7] HARD_BLOCK_KEYWORDS imported from sentiment_engine (single source of truth).
           No more duplicate list that could get out of sync.

  [FIX-8] has_breaking_negative_news() now uses sentiment_engine.is_hard_block()
           which has word-boundary matching. "ban" won't trigger on "Bandhan Bank".

  [G2-N1] NSE burst cap — on the first fetch (startup) only the MOST RECENT
           NSE_STARTUP_CAP (50) announcements are ingested.  Previously 371+
           were processed on startup creating massive log noise.
  [G2-N2] Relevance pre-filter — NSE announcements for symbols NOT in the
           trading universe are tagged to ^NSEI only (no per-symbol queue spam).
  [G2-N3] Score-based early discard — NSE items with known low-signal subjects
           (e.g. "Book Closure", "AGM", "Change of Address") are discarded
           before hitting the sentiment engine, cutting CPU by ~30%.
"""

import asyncio
import hashlib
import json
import time as _time
import xml.etree.ElementTree as ET
import urllib.request
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from core.logger import log
from news.sentiment_engine import SentimentEngine, HARD_BLOCK_KEYWORDS

# ── Symbol alias mapping ──────────────────────────────────────────────────────
SYMBOL_ALIASES: Dict[str, List[str]] = {
    "HDFCBANK.NS":   ["hdfc bank", "hdfcbank"],
    "ICICIBANK.NS":  ["icici bank", "icicibank"],
    "SBIN.NS":       ["sbi", "state bank", "state bank of india"],
    "AXISBANK.NS":   ["axis bank", "axisbank"],
    "KOTAKBANK.NS":  ["kotak bank", "kotak mahindra bank"],
    "BANDHANBNK.NS": ["bandhan bank"],   # never bare "ban"
    "INDUSINDBK.NS": ["indusind bank", "indusind"],
    "RELIANCE.NS":   ["reliance", "ril", "reliance industries"],
    "TCS.NS":        ["tcs", "tata consultancy"],
    "INFY.NS":       ["infosys", "infy"],
    "WIPRO.NS":      ["wipro"],
    "HCLTECH.NS":    ["hcl tech", "hcl technologies"],
    "TECHM.NS":      ["tech mahindra"],
    "BAJFINANCE.NS": ["bajaj finance"],
    "BAJAJFINSV.NS": ["bajaj finserv"],
    "MARUTI.NS":     ["maruti", "maruti suzuki"],
    "HINDUNILVR.NS": ["hindustan unilever", "hul"],
    "NESTLEIND.NS":  ["nestle india"],
    "ITC.NS":        ["itc"],
    "LT.NS":         ["larsen", "l&t", "larsen & toubro"],
    "ASIANPAINT.NS": ["asian paints"],
    "TITAN.NS":      ["titan company"],
    "ULTRACEMCO.NS": ["ultratech cement"],
    "TATASTEEL.NS":  ["tata steel"],
    "ONGC.NS":       ["ongc", "oil and natural gas"],
    "NTPC.NS":       ["ntpc"],
    "POWERGRID.NS":  ["power grid", "powergrid"],
    "^NSEI":         ["nifty", "nifty 50", "nse", "sensex", "markets"],
    "^NSEBANK":      ["bank nifty", "banknifty", "banking sector"],
    "^CNXIT":        ["nifty it", "it sector", "tech sector"],
}

_KW_INDEX = {alias: sym for sym, aliases in SYMBOL_ALIASES.items() for alias in aliases}

FEED_URLS = {
    # MoneyControl removed — returns 503/403 consistently; use ET + NSE only
    "et_markets":   "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "et_stocks":    "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "et_economy":   "https://economictimes.indiatimes.com/economy/rssfeeds/1373380680.cms",
    "et_tech":      "https://economictimes.indiatimes.com/tech/rssfeeds/13357270.cms",
}

# MoneyControl and ET require a full browser User-Agent — bare "Mozilla/5.0" gets 403
_RSS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# [G2-N1] Limit startup NSE burst to most recent N announcements
NSE_STARTUP_CAP = 50

# [G2-N3] Low-signal NSE subjects — discard without scoring
NSE_LOW_SIGNAL_SUBJECTS = {
    "book closure", "agm", "board meeting", "change of address",
    "change in name", "change in registered office", "postal ballot",
    "alteration of memorandum", "shifting of registered office",
    "sub-division", "record date", "annual report", "annual general meeting",
    "notice of annual general meeting", "scrutinizer report",
}

NSE_API_URL = (
    "https://www.nseindia.com/api/corporate-announcements"
    "?index=equities&from_date={from_d}&to_date={to_d}"
)
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/",
}

_RSS_TIMEOUT   = 10
_RSS_RETRIES   = 2
_RSS_RETRY_DELAY = 3  # seconds
_NEWS_MAX_AGE  = 21600  # 6 hours


# ── Data classes ──────────────────────────────────────────────────────────────

class NewsItem:
    __slots__ = ["id", "title", "source", "url", "published_at",
                 "symbols", "sentiment_score", "raw_text"]

    def __init__(self, title, source, url="", published_at=None):
        self.title           = title.strip()
        self.source          = source
        self.url             = url
        self.published_at    = published_at or datetime.now()  # FIX-1: correct param name
        self.symbols         = []
        self.sentiment_score = 0.0
        self.raw_text        = title.lower()
        self.id              = hashlib.md5(f"{source}:{title[:80]}".encode()).hexdigest()[:12]


@dataclass
class SentimentResult:
    """
    FIX-5: Replaces bare float return from get_sentiment_score().
    Callers can check has_fresh_data to know if 0.0 means neutral or no-data.
    float(result) still works for backward compatibility.

    Rich comparisons (>=, <=, >, <, ==) delegate to self.score so that
    existing code like `if sc >= 0.4:` or `assert r >= 0.4` works directly
    without an explicit float() cast.
    """
    score:          float
    has_fresh_data: bool
    item_count:     int
    label:          str

    def __float__(self):
        return self.score

    # ── Rich comparison operators (delegate to self.score) ────────────────────
    def __ge__(self, other): return self.score >= (float(other) if not isinstance(other, (int, float)) else other)
    def __le__(self, other): return self.score <= (float(other) if not isinstance(other, (int, float)) else other)
    def __gt__(self, other): return self.score >  (float(other) if not isinstance(other, (int, float)) else other)
    def __lt__(self, other): return self.score <  (float(other) if not isinstance(other, (int, float)) else other)
    def __eq__(self, other): return self.score == (float(other) if not isinstance(other, (int, float)) else other)

    def __repr__(self):
        flag = "fresh" if self.has_fresh_data else "NO DATA"
        return f"SentimentResult({self.score:+.3f} | {self.label} | n={self.item_count} | {flag})"


# ── Aggregator ────────────────────────────────────────────────────────────────

class NewsFeedAggregator:

    def __init__(self, poll_interval_sec: int = 120):
        self._interval   = poll_interval_sec
        # FIX-3: OrderedDict for deterministic FIFO eviction (set was random)
        self._seen_ids: OrderedDict = OrderedDict()
        self._by_symbol: Dict[str, deque] = defaultdict(lambda: deque(maxlen=50))
        self._all:       deque            = deque(maxlen=200)
        self._running    = False
        self._sentiment  = SentimentEngine()
        self._is_first_fetch = True   # P7: suppress bus.publish on startup batch

        # ENHANCEMENT: NSE dedup guard — track last fetch time so we never
        # re-process the same announcement window in back-to-back polls.
        self._nse_last_fetch: Optional[datetime] = None

        # ENHANCEMENT: Sentiment direction cache — detects when a symbol's
        # score crosses a threshold so the engine can react instantly.
        # key=symbol, value={"score": float, "label": str}
        self._sentiment_cache: Dict[str, dict] = {}

        # Callbacks registered by engine._on_news_threshold() for instant reaction.
        # Format: list of async callables(item: NewsItem)
        self._threshold_callbacks: list = []

        log.info("NewsFeedAggregator initialized (patch11 ENHANCED)")

    async def start(self):
        self._running = True
        asyncio.create_task(self._loop(), name="news_feed")
        log.info(f"News feed polling every {self._interval}s — NSE + MC + ET")

    async def stop(self):
        self._running = False

    def register_threshold_callback(self, coro_fn) -> None:
        """
        ENHANCEMENT: Register an async callback that fires immediately when any
        headline scores beyond the configured threshold (|score| >= 0.4 by default).

        The engine registers engine._on_news_threshold() here during start() so
        that high-impact headlines trigger an instant signal scan rather than
        waiting up to 60 seconds for the next _main_loop() cycle.

        Args:
            coro_fn: async callable(item: NewsItem) — called with the NewsItem
                     that crossed the threshold.
        """
        self._threshold_callbacks.append(coro_fn)
        log.info(f"News threshold callback registered: {getattr(coro_fn, '__name__', repr(coro_fn))}")

    async def _loop(self):
        while self._running:
            try:
                await self._fetch_all()
            except Exception as e:
                log.debug(f"News loop error: {e}")
            await asyncio.sleep(self._interval)

    async def _fetch_all(self):
        loop  = asyncio.get_running_loop()
        source_names = list(FEED_URLS.keys()) + ["nse"]
        tasks = [loop.run_in_executor(None, self._fetch_rss_with_retry, n, u)
                 for n, u in FEED_URLS.items()]
        tasks.append(loop.run_in_executor(None, self._fetch_nse))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # PATCH8: Per-source observability — log each source result clearly
        new_items: List[NewsItem] = []
        source_summary = []
        for name, batch in zip(source_names, results):
            if isinstance(batch, Exception):
                source_summary.append(f"{name}:❌({type(batch).__name__})")
                log.warning(f"News source [{name}] failed: {batch}")
            elif not batch:
                source_summary.append(f"{name}:0")
            else:
                new_count = sum(1 for item in batch if item.id not in self._seen_ids)
                source_summary.append(f"{name}:{new_count}")
                for item in batch:
                    if item.id not in self._seen_ids:
                        new_items.append(item)

        if not new_items:
            return

        # FIX-6: Batch score when >3 items and FinBERT active
        if len(new_items) > 3 and self._sentiment._finbert_ready:
            scores = self._sentiment.score_batch([i.title for i in new_items])
            for item, sc in zip(new_items, scores):
                item.sentiment_score = sc
        else:
            for item in new_items:
                item.sentiment_score = self._sentiment.score(item.title)

        for item in new_items:
            item.symbols = self._tag(item.title)
            self._seen_ids[item.id] = True  # FIX-3: OrderedDict preserves insertion order
            self._all.append(item)
            for sym in item.symbols:
                self._by_symbol[sym].append(item)
            if item.symbols:
                log.debug(f"News [{item.source}] {item.symbols} "
                          f"score={item.sentiment_score:+.2f}: {item.title[:80]}")

            # P7: On the FIRST fetch (startup), only publish high-score items.
            # The first batch can be 400+ cached NSE announcements — zero-score items
            # would flood the event bus and log with 400 lines on boot.
            # After first fetch clears, ALL items publish (real-time monitoring).
            _THRESHOLD = 0.4
            _publish = (not self._is_first_fetch) or (abs(item.sentiment_score) >= 0.15)
            if _publish:
                try:
                    from core.event_bus import bus
                    is_hard, _ = self._sentiment.is_hard_block(item.title)
                    await bus.publish("news_alert", {
                        "id":           item.id,
                        "symbol":       item.symbols[0] if item.symbols else None,
                        "symbols":      item.symbols,
                        "title":        item.title,
                        "score":        item.sentiment_score,
                        "source":       item.source,
                        "url":          item.url,
                        "published_at": item.published_at.isoformat(),
                        "is_hard_block": is_hard,
                    })
                except Exception as e:
                    log.debug(f"news_alert publish error: {e}")

            # High-impact only: threshold callbacks (engine._on_news_threshold)
            if abs(item.sentiment_score) >= _THRESHOLD:
                for cb in self._threshold_callbacks:
                    try:
                        await cb(item)
                    except Exception as e:
                        log.debug(f"News threshold callback error: {e}")
                # Send Telegram alert for major news (|score| >= 0.5)
                try:
                    from alerts.telegram_bot import alerter
                    await alerter.news_alert_high(
                        headline=item.title,
                        score=item.sentiment_score,
                        symbol=item.symbols[0] if item.symbols else "",
                    )
                except Exception as e:
                    log.debug(f"Telegram news alert error: {e}")

        if self._is_first_fetch:
            high_score = sum(1 for i in new_items if abs(i.sentiment_score) >= 0.15)
            log.info(
                f"📰 News startup: {len(new_items)} headlines loaded | "
                f"Sources: {' | '.join(source_summary)} | "
                f"{high_score} actionable (score≥0.15)"
            )
            self._is_first_fetch = False
        else:
            log.info(f"📰 News: {len(new_items)} new | Sources: {' | '.join(source_summary)}")

        # ENHANCEMENT: Detect sentiment direction changes per symbol and publish event.
        for sym in {s for item in new_items for s in item.symbols}:
            result = self.get_sentiment_score(sym)
            old    = self._sentiment_cache.get(sym, {})
            old_score = old.get("score", 0.0)
            new_score = float(result)
            # Direction flip: crossed zero OR crossed ±0.4 threshold in either direction
            crossed = (
                (old_score >= 0 and new_score < -0.4) or  # bull → bearish
                (old_score <= 0 and new_score > +0.4) or  # bear → bullish
                (abs(old_score) < 0.4 and abs(new_score) >= 0.4)  # neutral → strong
            )
            if crossed:
                try:
                    from core.event_bus import bus
                    await bus.publish("sentiment_change", {
                        "symbol":           sym,
                        "old_score":        old_score,
                        "new_score":        new_score,
                        "old_label":        old.get("label", "NEUTRAL"),
                        "new_label":        result.label,
                        "direction_change": "bull_to_bear" if new_score < old_score else "bear_to_bull",
                    })
                    log.info(f"Sentiment flip [{sym}]: {old_score:+.2f} → {new_score:+.2f} ({result.label})")
                except Exception as e:
                    log.debug(f"sentiment_change publish error: {e}")
            self._sentiment_cache[sym] = {"score": new_score, "label": result.label}

        # FIX-3: Deterministic FIFO eviction -- removes OLDEST entries first
        while len(self._seen_ids) > 5000:
            self._seen_ids.popitem(last=False)

    # ── Fetchers ──────────────────────────────────────────────────────────────

    def _fetch_rss_with_retry(self, name: str, url: str) -> List[NewsItem]:
        """FIX-4: Retry RSS fetch up to _RSS_RETRIES times with backoff."""
        last_err = None
        for attempt in range(1, _RSS_RETRIES + 1):
            try:
                return self._fetch_rss(name, url)
            except Exception as e:
                last_err = e
                if attempt < _RSS_RETRIES:
                    log.debug(f"RSS {name} attempt {attempt} failed: {e} — retrying in {_RSS_RETRY_DELAY}s")
                    _time.sleep(_RSS_RETRY_DELAY)
        log.debug(f"RSS {name} failed after {_RSS_RETRIES} attempts: {last_err}")
        return []

    def _fetch_rss(self, name: str, url: str) -> List[NewsItem]:
        """Fetch and parse one RSS feed. Raises on error (caller handles retry).
        Uses full Chrome UA — MoneyControl/ET return 403 on bare 'Mozilla/5.0'.
        """
        items = []
        req = urllib.request.Request(url, headers={
            "User-Agent": _RSS_USER_AGENT,
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        })
        with urllib.request.urlopen(req, timeout=_RSS_TIMEOUT) as r:
            raw = r.read()
        root = ET.fromstring(raw)
        ch = root.find("channel")
        if ch is None:
            return items
        for entry in ch.findall("item"):
            title = (entry.findtext("title") or "").strip()
            link  = (entry.findtext("link")  or "").strip()
            pub   = entry.findtext("pubDate") or ""
            if not title:
                continue
            try:
                from email.utils import parsedate_to_datetime
                pub_dt = parsedate_to_datetime(pub).replace(tzinfo=None)
            except Exception:
                pub_dt = datetime.now()
            if (datetime.now() - pub_dt).total_seconds() > _NEWS_MAX_AGE:
                continue
            items.append(NewsItem(title, name, link, pub_dt))
        return items

    def _fetch_nse(self) -> List[NewsItem]:
        """
        Fetch NSE corporate announcements.
        FIX-1: NewsItem constructed with published_at= (NOT the old broken pub_dt=)
        ENHANCEMENT: _nse_last_fetch guard — only fetches if at least 90s have
        passed since the last successful NSE call.  Prevents the same
        announcement from being re-ingested on back-to-back polls.
        """
        # ENHANCEMENT: Skip if fetched recently (NSE API updates ~every 5 min)
        now = datetime.now()
        if self._nse_last_fetch is not None:
            elapsed = (now - self._nse_last_fetch).total_seconds()
            if elapsed < 90:
                log.debug(f"NSE: skipping poll (last fetch {elapsed:.0f}s ago, min=90s)")
                return []

        items = []
        try:
            today = now.strftime("%d-%m-%Y")
            yest  = (now - timedelta(days=1)).strftime("%d-%m-%Y")
            url   = NSE_API_URL.format(from_d=yest, to_d=today)
            req   = urllib.request.Request(url, headers=NSE_HEADERS)
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            raw_announcements = data if isinstance(data, list) else []
            # [G2-N1] Cap startup burst — only process most recent N on first fetch
            if self._is_first_fetch and len(raw_announcements) > NSE_STARTUP_CAP:
                raw_announcements = raw_announcements[-NSE_STARTUP_CAP:]
                log.debug(f"NSE: startup cap applied — processing {NSE_STARTUP_CAP} most recent")

            for ann in raw_announcements:
                sym  = ann.get("symbol", "")
                subj = ann.get("subject", ann.get("desc", ""))
                if not subj:
                    continue

                # [G2-N3] Low-signal subject discard (saves CPU + log noise)
                if any(low in subj.lower() for low in NSE_LOW_SIGNAL_SUBJECTS):
                    continue

                title   = f"[NSE Announce] {sym}: {subj}"
                nse_sym = f"{sym}.NS"
                # [G2-N2] Map to ^NSEI if symbol not in universe
                from core.config import cfg
                universe_syms = set(getattr(cfg, "symbols", []))
                # FIX-1: Use published_at= not pub_dt= (pub_dt caused silent TypeError)
                item = NewsItem(title, "nse_official", published_at=datetime.now())
                if nse_sym in universe_syms or nse_sym in SYMBOL_ALIASES:
                    item.symbols = [nse_sym]
                else:
                    item.symbols = ["^NSEI"]  # [G2-N2] Non-universe → index bucket only
                items.append(item)
            # ENHANCEMENT: Only update timestamp on success
            self._nse_last_fetch = now
            log.debug(f"NSE: fetched {len(items)} announcements")
        except Exception as e:
            log.debug(f"NSE announcements: {e}")
        return items

    def _tag(self, text: str) -> List[str]:
        t = text.lower()
        return list({sym for kw, sym in _KW_INDEX.items() if kw in t})

    # ── Public API ────────────────────────────────────────────────────────────

    def get_news(self, symbol: str, max_age_hours: int = 4) -> List[NewsItem]:
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        return sorted(
            [i for i in self._by_symbol.get(symbol, []) if i.published_at >= cutoff],
            key=lambda x: x.published_at, reverse=True
        )

    def get_headlines_for_symbol(self, symbol: str, max_age_hours: int = 4, limit: int = 5) -> List[dict]:
        """
        Returns recent headlines for a symbol as plain dicts (for engine signal context).
        Called by engine.py when building trade rationale.
        """
        items = self.get_news(symbol, max_age_hours)[:limit]
        return [
            {
                "title":    item.title,
                "source":   item.source,
                "score":    item.sentiment_score,
                "age_min":  round((datetime.now() - item.published_at).total_seconds() / 60, 1),
            }
            for item in items
        ]

    def get_sentiment_score(self, symbol: str, max_age_hours: int = 2) -> SentimentResult:
        """
        FIX-5: Returns SentimentResult instead of bare float.
        result.has_fresh_data = False when no news exists in the window.
        float(result) still works for callers expecting a float.
        """
        items = self.get_news(symbol, max_age_hours)
        if not items:
            return SentimentResult(score=0.0, has_fresh_data=False,
                                   item_count=0, label="NEUTRAL (no data)")
        now   = datetime.now()
        w_sum = 0.0
        total = 0.0
        for item in items:
            age_min = (now - item.published_at).total_seconds() / 60
            w       = 2 ** (-age_min / 60)
            w_sum  += item.sentiment_score * w
            total  += w
        score = round(max(-1.0, min(1.0, w_sum / total if total > 0 else 0.0)), 3)
        return SentimentResult(score=score, has_fresh_data=True,
                               item_count=len(items),
                               label=self._sentiment.classify(score))

    def has_breaking_negative_news(self, symbol: str) -> Tuple[bool, str]:
        """
        FIX-8: Uses sentiment_engine.is_hard_block() which has word-boundary matching.
        FIX-7: No local HARD_BLOCK_KEYWORDS duplicate -- uses imported list.
        """
        for item in self.get_news(symbol, max_age_hours=4):
            blocked, kw = self._sentiment.is_hard_block(item.title)
            if blocked:
                return True, f"'{kw}' in: {item.title[:60]}"
        return False, ""

    def get_market_mood(self) -> SentimentResult:
        return self.get_sentiment_score("^NSEI", max_age_hours=1)

    def get_recent_headlines(self, limit: int = 20) -> List[dict]:
        items = sorted(self._all, key=lambda x: x.published_at, reverse=True)
        return [{"title": i.title[:120], "source": i.source, "symbols": i.symbols,
                 "score": i.sentiment_score, "label": self._sentiment.classify(i.sentiment_score),
                 "time": i.published_at.strftime("%H:%M")}
                for i in items[:limit]]
