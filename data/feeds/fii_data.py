# -*- coding: utf-8 -*-
"""
ZeroBot G1 — FII/DII Alternative Data Feed
============================================
Fetches institutional flow data from NSE India public API.
FII/DII flows drive 60%+ of large-cap NSE moves.

USAGE IN TRADING:
  FII net > ₹2000cr  → BULLISH bias, increase position size
  FII net < -₹1000cr → BEARISH bias, reduce new longs
  DII buying on FII sell → support floor, less downside risk
"""
import json, time, logging, datetime, urllib.request, urllib.error
from typing import Optional, Dict, Any

log = logging.getLogger(__name__)

_NSE_URL   = "https://www.nseindia.com/api/fiidiiTradeReact"
_CACHE_TTL = 3600
_HEADERS   = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept":     "application/json",
    "Referer":    "https://www.nseindia.com/",
}


class FIIDIIFeed:

    def __init__(self):
        self._cache:    Optional[Dict] = None
        self._cache_ts: float = 0
        self._last:     Optional[Dict] = None

    def _valid(self) -> bool:
        return self._cache is not None and time.time()-self._cache_ts < _CACHE_TTL

    def _get_session_cookies(self) -> dict:
        """Fetch NSE session cookies — required for all NSE API calls."""
        try:
            import http.cookiejar as cookielib
            cj = cookielib.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            opener.addheaders = list(_HEADERS.items())
            opener.open("https://www.nseindia.com/", timeout=5)
            return {c.name: c.value for c in cj}
        except Exception:
            return {}

    def fetch(self, force: bool=False) -> Dict[str, Any]:
        if not force and self._valid() and self._cache:
            return self._cache
        try:
            # NSE requires a valid session cookie obtained from the homepage first
            cookies = self._get_session_cookies()
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            headers = {**_HEADERS, "Cookie": cookie_str} if cookie_str else _HEADERS
            req = urllib.request.Request(_NSE_URL, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as r:
                raw = json.loads(r.read().decode())
            if not raw or not isinstance(raw,list):
                return self._fallback()
            latest = raw[0]
            def _p(v):
                try: return float(str(v).replace(",","").replace(" ","") or "0")
                except: return 0.0
            fii_b=_p(latest.get("fiiBuyValue",0)); fii_s=_p(latest.get("fiiSellValue",0))
            dii_b=_p(latest.get("diiBuyValue",0)); dii_s=_p(latest.get("diiSellValue",0))
            fii_n=fii_b-fii_s; dii_n=dii_b-dii_s
            result = {
                "date":          latest.get("date",datetime.date.today().isoformat()),
                "fii_buy":       round(fii_b,2), "fii_sell":   round(fii_s,2),
                "fii_net":       round(fii_n,2), "dii_buy":    round(dii_b,2),
                "dii_sell":      round(dii_s,2), "dii_net":    round(dii_n,2),
                "combined_net":  round(fii_n+dii_n,2),
                "bias":          self._bias(fii_n,dii_n),
                "fii_label":     self._label(fii_n),
                "source":        "nse_live",
            }
            self._cache=result; self._cache_ts=time.time(); self._last=result
            log.info(f"[FII] FII={fii_n:+.0f}cr DII={dii_n:+.0f}cr bias={result['bias']}")
            return result
        except Exception as e:
            log.debug(f"[FII] Fetch error: {e}")
            return self._fallback()

    def _fallback(self) -> Dict[str,Any]:
        if self._last:
            return {**self._last,"source":"cache"}
        return {
            "date":datetime.date.today().isoformat(),
            "fii_net":0.0,"dii_net":0.0,"combined_net":0.0,
            "fii_buy":0.0,"fii_sell":0.0,"dii_buy":0.0,"dii_sell":0.0,
            "bias":"NEUTRAL","fii_label":"NEUTRAL","source":"fallback",
        }

    @staticmethod
    def _label(n: float) -> str:
        if n>3000: return "VERY_STRONG_BUY"
        if n>1000: return "STRONG_BUY"
        if n>200:  return "BUY"
        if n>-200: return "NEUTRAL"
        if n>-1000:return "SELL"
        if n>-3000:return "STRONG_SELL"
        return "VERY_STRONG_SELL"

    @staticmethod
    def _bias(fii: float, dii: float) -> str:
        c = fii*2+dii
        if c>4000:  return "STRONG_BULLISH"
        if c>1000:  return "BULLISH"
        if c>-1000: return "NEUTRAL"
        if c>-3000: return "BEARISH"
        return "STRONG_BEARISH"

    def signal_modifier(self, side: str) -> float:
        data = self._cache or self._fallback()
        fii  = data.get("fii_net",0.0)
        if side=="BUY":
            if fii>1000: return +5.0
            if fii>200:  return +2.0
            if fii<-1000:return -10.0
            if fii<-200: return -4.0
        else:
            if fii<-1000:return +5.0
            if fii<-200: return +2.0
            if fii>1000: return -8.0
            if fii>200:  return -3.0
        return 0.0

    def for_groq(self) -> str:
        d = self._cache or self._fallback()
        return f"FII={d.get('fii_net',0):+.0f}cr ({d.get('fii_label','?')}) DII={d.get('dii_net',0):+.0f}cr bias={d.get('bias','?')}"

    def for_dashboard(self) -> Dict[str,Any]:
        return self._cache or self._fallback()


# ── Singleton ─────────────────────────────────────────────────────────────────
fii_feed = FIIDIIFeed()
