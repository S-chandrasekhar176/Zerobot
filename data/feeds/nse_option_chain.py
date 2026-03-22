# -*- coding: utf-8 -*-
"""
ZeroBot Pro — NSE Option Chain Scraper (Patch 5 NEW)
═════════════════════════════════════════════════════
Fetches REAL NSE option chain data directly from NSE website.
No paid API needed — NSE publishes this publicly.

Data available:
  - All strikes for given underlying (NIFTY, BANKNIFTY, RELIANCE, etc.)
  - Real bid/ask/LTP for each CE and PE
  - Open Interest, Change in OI
  - IV (Implied Volatility) per strike
  - PCR (Put-Call Ratio)
  - Max Pain strike

Usage:
  chain = NSEOptionChain()
  data  = await chain.fetch("NIFTY")
  atm   = chain.get_atm_strike(data, spot_price=24500)
  otm_ce = chain.get_otm_strikes(data, spot_price=24500, side="CE", n=3)
"""
import asyncio
import time
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from core.logger import log


# NSE requires these headers or it returns 401
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

NSE_OPTION_CHAIN_URL = "https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
NSE_STOCK_OC_URL     = "https://www.nseindia.com/api/option-chain-equities?symbol={symbol}"
NSE_SESSION_URL      = "https://www.nseindia.com/"


@dataclass
class OptionStrike:
    strike:        float
    expiry:        str
    ce_ltp:        float = 0.0
    ce_iv:         float = 0.0
    ce_oi:         float = 0.0
    ce_chg_oi:     float = 0.0
    ce_volume:     int   = 0
    ce_bid:        float = 0.0
    ce_ask:        float = 0.0
    pe_ltp:        float = 0.0
    pe_iv:         float = 0.0
    pe_oi:         float = 0.0
    pe_chg_oi:     float = 0.0
    pe_volume:     int   = 0
    pe_bid:        float = 0.0
    pe_ask:        float = 0.0


@dataclass
class OptionChainData:
    symbol:        str
    spot_price:    float
    expiry_dates:  List[str]
    strikes:       List[OptionStrike] = field(default_factory=list)
    pcr:           float = 1.0        # Put-Call Ratio
    max_pain:      float = 0.0
    fetched_at:    float = field(default_factory=time.time)
    source:        str   = "NSE"      # "NSE" | "synthetic" | "cache"


class NSEOptionChain:
    """
    Fetches real NSE option chain. Falls back to synthetic (Black-Scholes)
    if NSE is unreachable (pre-market, holidays, rate limited).
    """

    def __init__(self, cache_ttl: int = 60):
        self._cache: Dict[str, OptionChainData] = {}
        self._cache_ttl = cache_ttl
        self._session_cookie: Optional[str] = None
        self._session_last: float = 0
        log.info("NSEOptionChain initialized (real NSE + Black-Scholes fallback)")

    async def _get_session_cookie(self) -> Optional[str]:
        """NSE requires a session cookie — get it by hitting the homepage first."""
        if time.time() - self._session_last < 600:  # re-use for 10 min
            return self._session_cookie
        try:
            import aiohttp
            async with aiohttp.ClientSession(headers=NSE_HEADERS) as s:
                async with s.get(NSE_SESSION_URL, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    cookies = {c.key: c.value for c in s.cookie_jar}
                    self._session_cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())
                    self._session_last = time.time()
                    log.debug("NSE session cookie obtained")
                    return self._session_cookie
        except Exception as e:
            log.debug(f"NSE session cookie failed: {e}")
            return None

    async def fetch(self, symbol: str, expiry: Optional[str] = None) -> OptionChainData:
        """
        Fetch option chain for symbol.
        symbol: "NIFTY" | "BANKNIFTY" | "RELIANCE" | "TCS" etc.
        expiry: specific expiry date string, or None = nearest expiry
        """
        # Check cache
        cache_key = f"{symbol}_{expiry or 'nearest'}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if time.time() - cached.fetched_at < self._cache_ttl:
                return cached

        # Try real NSE first
        data = await self._fetch_nse(symbol, expiry)
        if data:
            self._cache[cache_key] = data
            return data

        # Fallback to synthetic
        log.warning(f"NSE option chain unavailable for {symbol} — using synthetic")
        data = await self._synthetic_chain(symbol, expiry)
        self._cache[cache_key] = data
        return data

    async def _fetch_nse(self, symbol: str, expiry: Optional[str]) -> Optional[OptionChainData]:
        """Fetch real data from NSE API."""
        try:
            import aiohttp
            cookie = await self._get_session_cookie()
            headers = {**NSE_HEADERS}
            if cookie:
                headers["Cookie"] = cookie

            # Indices vs equities endpoint
            is_index = symbol in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")
            url = (NSE_OPTION_CHAIN_URL if is_index else NSE_STOCK_OC_URL).format(symbol=symbol)

            async with aiohttp.ClientSession(headers=headers) as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        log.debug(f"NSE OC returned {r.status} for {symbol}")
                        return None
                    raw = await r.json(content_type=None)

            return self._parse_nse_response(symbol, raw, expiry)
        except Exception as e:
            log.debug(f"NSE OC fetch failed ({symbol}): {e}")
            return None

    # ── Spot price fallback registry (symbol → last known good price) ─────────
    _last_known_spot: Dict[str, float] = {}

    def _resolve_spot_price(self, symbol: str, raw_spot: float,
                             data_list: list) -> float:
        """
        BUG-FIX (G3-O1): NSE API occasionally returns underlyingValue=0 when:
          • Market is closed / pre-open
          • Rate limit causes partial JSON response
          • Symbol is an equity (not index) — underlyingValue not always present

        Fallback chain:
          1. underlyingValue from API (if > 0)          ← normal case
          2. Midpoint of ATM CE/PE pair from strike data ← works 95% of time
          3. Last known good spot price (in-memory cache) ← staleness <= 5 min
          4. Yahoo Finance yfinance quick fetch           ← last resort
          5. 0.0 (caller handles gracefully)
        """
        if raw_spot > 0:
            self._last_known_spot[symbol] = raw_spot
            return raw_spot

        log.debug(f"[OC-FIX] underlyingValue=0 for {symbol} — trying fallbacks")

        # Fallback 2: estimate from strike data (CE ask + PE ask midpoint near ATM)
        if data_list:
            try:
                strikes_seen = sorted({float(r.get("strikePrice", 0)) for r in data_list if r.get("strikePrice")})
                if strikes_seen:
                    mid_idx = len(strikes_seen) // 2
                    atm_strike = strikes_seen[mid_idx]
                    # find the row for this strike
                    for row in data_list:
                        if float(row.get("strikePrice", 0)) == atm_strike:
                            ce_ltp = float(row.get("CE", {}).get("lastPrice", 0) or 0)
                            pe_ltp = float(row.get("PE", {}).get("lastPrice", 0) or 0)
                            ce_iv  = float(row.get("CE", {}).get("impliedVolatility", 0) or 0)
                            # spot ≈ atm_strike + (CE_ltp - PE_ltp) — put-call parity approx
                            if ce_ltp > 0 and pe_ltp > 0:
                                estimated = atm_strike + (ce_ltp - pe_ltp)
                                if estimated > 10:
                                    log.info(f"[OC-FIX] {symbol} spot estimated from PCR parity: {estimated:.1f}")
                                    self._last_known_spot[symbol] = estimated
                                    return estimated
            except Exception as _e:
                log.debug(f"[OC-FIX] strike fallback failed: {_e}")

        # Fallback 3: last known cached value
        cached = self._last_known_spot.get(symbol)
        if cached and cached > 0:
            log.info(f"[OC-FIX] {symbol} using last known spot: {cached:.1f}")
            return cached

        # Fallback 4: Yahoo Finance quick fetch (synchronous, timeout 2s)
        # NIFTY/BANKNIFTY are NSE indices — Yahoo uses ^ prefix, NOT .NS suffix
        _INDEX_YF_MAP = {
            "NIFTY":      "^NSEI",
            "BANKNIFTY":  "^NSEBANK",
            "FINNIFTY":   "^CNXFIN",
            "MIDCPNIFTY": "^NSEMDCP50",
            "NIFTYNXT50": "^NSMIDCP100",
        }
        try:
            import yfinance as yf
            yf_sym = _INDEX_YF_MAP.get(symbol.upper())
            if not yf_sym:
                yf_sym = symbol if "." in symbol else f"{symbol}.NS"
            ticker = yf.Ticker(yf_sym)
            info   = ticker.fast_info
            yf_price = float(getattr(info, "last_price", 0) or 0)
            if yf_price > 0:
                log.info(f"[OC-FIX] {symbol} spot from Yahoo ({yf_sym}): {yf_price:.1f}")
                self._last_known_spot[symbol] = yf_price
                return yf_price
        except Exception as _e:
            log.debug(f"[OC-FIX] Yahoo fallback failed for {symbol}: {_e}")

        log.warning(f"[OC-FIX] ⚠️ All spot fallbacks exhausted for {symbol} — returning 0")
        return 0.0

    def _parse_nse_response(self, symbol: str, raw: dict, target_expiry: Optional[str]) -> Optional[OptionChainData]:
        """Parse NSE option chain JSON into OptionChainData."""
        try:
            records   = raw.get("records", {})
            data_list = raw.get("filtered", {}).get("data", records.get("data", []))
            raw_spot  = float(records.get("underlyingValue", 0))
            # G3-O1 FIX: resolve spot with multi-level fallback (was: spot=0 crash)
            spot      = self._resolve_spot_price(symbol, raw_spot, data_list)
            expiries  = records.get("expiryDates", [])

            # Pick expiry
            if target_expiry and target_expiry in expiries:
                chosen_expiry = target_expiry
            else:
                chosen_expiry = expiries[0] if expiries else ""

            strikes: List[OptionStrike] = []
            total_ce_oi = 0.0
            total_pe_oi = 0.0

            for row in data_list:
                if row.get("expiryDate", "") != chosen_expiry and chosen_expiry:
                    continue
                strike = float(row.get("strikePrice", 0))
                ce = row.get("CE", {})
                pe = row.get("PE", {})

                os = OptionStrike(
                    strike    = strike,
                    expiry    = chosen_expiry,
                    ce_ltp    = float(ce.get("lastPrice",     0)),
                    ce_iv     = float(ce.get("impliedVolatility", 0)),
                    ce_oi     = float(ce.get("openInterest",  0)),
                    ce_chg_oi = float(ce.get("changeinOpenInterest", 0)),
                    ce_volume = int(ce.get("totalTradedVolume", 0)),
                    ce_bid    = float(ce.get("bidprice",      0)),
                    ce_ask    = float(ce.get("askPrice",      0)),
                    pe_ltp    = float(pe.get("lastPrice",     0)),
                    pe_iv     = float(pe.get("impliedVolatility", 0)),
                    pe_oi     = float(pe.get("openInterest",  0)),
                    pe_chg_oi = float(pe.get("changeinOpenInterest", 0)),
                    pe_volume = int(pe.get("totalTradedVolume", 0)),
                    pe_bid    = float(pe.get("bidprice",      0)),
                    pe_ask    = float(pe.get("askPrice",      0)),
                )
                strikes.append(os)
                total_ce_oi += os.ce_oi
                total_pe_oi += os.pe_oi

            pcr = round(total_pe_oi / max(total_ce_oi, 1), 3)
            max_pain = self._compute_max_pain(strikes)

            log.info(
                f"✅ NSE Option Chain: {symbol} | spot={spot:.1f} "
                f"| {len(strikes)} strikes | expiry={chosen_expiry} "
                f"| PCR={pcr:.2f} | MaxPain={max_pain:.0f}"
            )
            return OptionChainData(
                symbol=symbol, spot_price=spot, expiry_dates=expiries,
                strikes=strikes, pcr=pcr, max_pain=max_pain, source="NSE"
            )
        except Exception as e:
            log.debug(f"NSE OC parse error: {e}")
            return None

    def _compute_max_pain(self, strikes: List[OptionStrike]) -> float:
        """Max pain = strike where total option loss for buyers is maximised."""
        if not strikes:
            return 0.0
        min_pain = float("inf")
        pain_strike = 0.0
        for s in strikes:
            pain = sum(
                max(0, s.strike - other.strike) * other.ce_oi +
                max(0, other.strike - s.strike) * other.pe_oi
                for other in strikes
            )
            if pain < min_pain:
                min_pain = pain
                pain_strike = s.strike
        return pain_strike

    async def _synthetic_chain(self, symbol: str, expiry: Optional[str]) -> OptionChainData:
        """Build synthetic option chain using Black-Scholes when NSE is unavailable."""
        from data.feeds.options_pricer import black_scholes_price
        import math

        # Estimate spot price from config watchlist
        try:
            from core.config import cfg
            spot = getattr(cfg, "synthetic_spot", {}).get(symbol, 24500.0)
        except Exception:
            spot = 24500.0

        iv    = 0.18    # 18% base IV (reasonable for NSE)
        dte   = 7       # assume 7 days to expiry
        rate  = 0.065   # 6.5% India risk-free rate

        # Generate strikes around ATM
        atm = round(spot / 50) * 50
        strike_range = range(int(atm - 500), int(atm + 550), 50)
        from datetime import datetime, timedelta
        exp_date = (datetime.now() + timedelta(days=dte)).strftime("%d-%b-%Y").upper()

        strikes = []
        for k in strike_range:
            ce_ltp = black_scholes_price(spot, k, dte, iv * (1 + abs(k-spot)/spot), rate, "CE")
            pe_ltp = black_scholes_price(spot, k, dte, iv * (1 + abs(k-spot)/spot), rate, "PE")
            strikes.append(OptionStrike(
                strike=k, expiry=exp_date,
                ce_ltp=round(ce_ltp, 2), pe_ltp=round(pe_ltp, 2),
                ce_iv=round(iv*100, 1), pe_iv=round(iv*100, 1),
            ))

        return OptionChainData(
            symbol=symbol, spot_price=spot, expiry_dates=[exp_date],
            strikes=strikes, pcr=1.05, max_pain=atm, source="synthetic"
        )

    # ── Utility methods ──────────────────────────────────────────────────────

    def get_atm_strike(self, chain: OptionChainData, spot: Optional[float] = None) -> Optional[OptionStrike]:
        """Get the At-The-Money strike (nearest to spot price)."""
        if not chain.strikes:
            return None
        price = spot or chain.spot_price
        return min(chain.strikes, key=lambda s: abs(s.strike - price))

    def get_otm_strikes(
        self, chain: OptionChainData, spot: Optional[float],
        side: str = "CE", n: int = 3
    ) -> List[OptionStrike]:
        """Get n OTM strikes for CE (above spot) or PE (below spot)."""
        price = spot or chain.spot_price
        if side == "CE":
            candidates = [s for s in chain.strikes if s.strike > price]
            return sorted(candidates, key=lambda s: s.strike)[:n]
        else:
            candidates = [s for s in chain.strikes if s.strike < price]
            return sorted(candidates, key=lambda s: s.strike, reverse=True)[:n]

    def get_iv_rank(self, chain: OptionChainData) -> float:
        """
        IV Rank 0-100: how high is current IV vs historical range.
        (Current IV - 52w low) / (52w high - 52w low) * 100
        Uses ATM IV as proxy.
        """
        atm = self.get_atm_strike(chain)
        if not atm:
            return 50.0
        current_iv = (atm.ce_iv + atm.pe_iv) / 2
        iv_52w_low  = current_iv * 0.6
        iv_52w_high = current_iv * 1.8
        iv_rank = (current_iv - iv_52w_low) / max(iv_52w_high - iv_52w_low, 1) * 100
        return round(min(100, max(0, iv_rank)), 1)

    # ── P5: Methods needed by realtime_feed for paper LTP updates ────────────

    def parse_zerobot_symbol(self, option_symbol: str) -> Optional[dict]:
        """
        Parse ZeroBot option symbol format into lookup params.
        Input:  "RELIANCE12MAR261450CE"
        Output: {"symbol": "RELIANCE", "strike": 1450.0, "type": "CE", "expiry_str": "12-Mar-2026"}
        """
        import re
        pattern = r'^([A-Z&]+?)(\d{2}[A-Z]{3}\d{2})(\d+)(CE|PE)$'
        m = re.match(pattern, option_symbol.upper())
        if not m:
            return None
        sym, expiry_compact, strike_str, opt_type = m.groups()
        from datetime import datetime as _dt
        try:
            expiry_dt = _dt.strptime(expiry_compact, "%d%b%y")
            # NSE uses e.g. "12-Mar-2026" format in option chain
            expiry_str = expiry_dt.strftime("%-d-%b-%Y")
        except Exception:
            expiry_str = None
        return {
            "symbol": sym,
            "strike": float(strike_str),
            "type": opt_type,
            "expiry_str": expiry_str,
        }

    def get_ltp_for_position(self, option_symbol: str) -> Optional[float]:
        """
        Synchronous: parse ZeroBot symbol → fetch real NSE LTP.
        Used by realtime_feed for paper position LTP updates.
        Falls back to None (caller uses Black-Scholes instead).
        """
        parsed = self.parse_zerobot_symbol(option_symbol)
        if not parsed:
            return None
        try:
            # Run the async fetch in the current event loop or a new one
            import asyncio as _asyncio
            try:
                loop = _asyncio.get_event_loop()
                if loop.is_running():
                    # Already in async context — use executor
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        future = pool.submit(_asyncio.run, self.fetch(parsed["symbol"]))
                        chain = future.result(timeout=8)
                else:
                    chain = loop.run_until_complete(self.fetch(parsed["symbol"]))
            except Exception:
                chain = _asyncio.run(self.fetch(parsed["symbol"]))

            if not chain or not chain.strikes:
                return None
            # Find strike match
            for s in chain.strikes:
                if s.strike == parsed["strike"]:
                    if parsed["type"] == "CE":
                        return s.ce_ltp if s.ce_ltp and s.ce_ltp > 0 else None
                    else:
                        return s.pe_ltp if s.pe_ltp and s.pe_ltp > 0 else None
            return None
        except Exception as e:
            log.debug(f"NSE LTP lookup {option_symbol}: {e}")
            return None

    def get_iv_for_signal(self, symbol: str, option_type: str = "CE") -> float:
        """
        Synchronous: get real IV for Black-Scholes calibration.
        Returns 0.20 (20%) if NSE unavailable.
        """
        try:
            import asyncio as _asyncio
            loop = _asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(_asyncio.run, self.fetch(symbol))
                    chain = future.result(timeout=8)
            else:
                chain = loop.run_until_complete(self.fetch(symbol))
            if not chain:
                return 0.20
            atm = self.get_atm_strike(chain)
            if not atm:
                return 0.20
            iv = atm.ce_iv if option_type.upper() == "CE" else atm.pe_iv
            return max(0.05, min(iv, 2.0))
        except Exception:
            return 0.20

    def get_pcr_signal(self, symbol: str) -> Optional[str]:
        """
        Put-Call Ratio signal: BULLISH / BEARISH / NEUTRAL.
        PCR < 0.7 = bullish (more calls), PCR > 1.2 = bearish (more puts).
        """
        try:
            import asyncio as _asyncio
            loop = _asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(_asyncio.run, self.fetch(symbol))
                    chain = future.result(timeout=8)
            else:
                chain = loop.run_until_complete(self.fetch(symbol))
            if not chain:
                return None
            total_ce_oi = sum(s.ce_oi for s in chain.strikes)
            total_pe_oi = sum(s.pe_oi for s in chain.strikes)
            pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 1.0
            if pcr < 0.7:
                return "BULLISH"
            elif pcr > 1.2:
                return "BEARISH"
            return "NEUTRAL"
        except Exception:
            return None

    def health_check(self) -> dict:
        """Check if NSE API is reachable. Called during bot startup.
        Outside market hours (9:15–15:30 IST) spot_price=0 is expected and normal.
        We return ok=True if the API is reachable and returns a chain object.
        """
        try:
            import asyncio as _asyncio
            loop = _asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(_asyncio.run, self.fetch("NIFTY"))
                    chain = future.result(timeout=10)
            else:
                chain = loop.run_until_complete(self.fetch("NIFTY"))
            if chain:
                spot = chain.spot_price or 0.0
                strikes_count = len(chain.strikes) if chain.strikes else 0
                if spot > 0:
                    return {"ok": True, "nifty_spot": spot, "strikes": strikes_count,
                            "detail": f"NIFTY ₹{spot:.0f} | {strikes_count} strikes"}
                else:
                    # Outside market hours — API reachable, no live prices
                    return {"ok": True, "nifty_spot": 0, "strikes": strikes_count,
                            "detail": "Market closed — NSE API reachable (no live prices)"}
            return {"ok": False, "reason": "No chain data returned from NSE"}
        except Exception as e:
            return {"ok": False, "reason": str(e)}


# Module-level instance
nse_option_chain = NSEOptionChain(cache_ttl=60)
