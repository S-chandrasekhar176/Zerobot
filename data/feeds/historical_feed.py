"""
ZeroBot G2 — Historical Data Feed  [G2 UPGRADE]
================================================
G2 CHANGES vs G1:
  [G2-D1] Exponential-backoff retry (3 attempts)
  [G2-D2] Disk snapshot cache (parquet)
  [G2-D3] Data completeness gate
  [G2-D4] Index symbols never marked dead
  [G2-D5] Intraday frequency guard

S-MODE FIX: In S-Mode (cfg.is_smode), intraday 5m/1m candles are fetched from
  Shoonya time_price_series API instead of Yahoo Finance.
  Falls back to Yahoo automatically if Shoonya is unavailable.
"""
import time
import hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any
import pandas as pd
import yfinance as yf

from core.config import cfg
from core.logger import log

# S-Mode Shoonya broker reference (set by engine after connect)
_shoonya_broker: Any = None

def register_shoonya_broker(broker) -> None:
    """Called by the engine after Shoonya broker connects in S-Mode."""
    global _shoonya_broker
    _shoonya_broker = broker
    log.info("[HistoricalFeed] Shoonya broker registered — S-Mode intraday data enabled")

# ── Constants ─────────────────────────────────────────────────────────────────
CACHE_DIR              = Path(__file__).parent.parent.parent / "data" / "cache" / "ohlcv"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
COMPLETENESS_THRESHOLD = 0.95      # warn if > 5% NaN in features
MAX_RETRIES            = 3
RETRY_BASE_S           = 2.0       # back-off: 2s, 4s, 8s
INTRADAY_MIN_AGE_S     = 240       # skip re-download if < 4 min old

_DEAD_SYMBOLS: set         = set()
_LAST_DOWNLOAD: Dict[str, float] = {}


class HistoricalFeed:
    """
    Downloads OHLCV data from Yahoo Finance.
    G2: retry + disk cache + completeness checking + frequency guard.
    """

    INTERVAL_MAP = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "1d": "1d"}

    def __init__(self):
        self.symbols = cfg.symbols
        log.info(f"HistoricalFeed initialized — {len(self.symbols)} symbols")

    # ──────────────────────────────────────────────────────────────────────────
    def download(
        self,
        symbol: str,
        interval: str = "1d",
        period: str = "2y",
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        Download OHLCV with retry + disk-cache fallback.

        S-MODE: For intraday intervals (1m/5m/15m/30m) and non-index symbols,
        tries Shoonya time_price_series first, falls back to Yahoo Finance.
        Daily candles and index symbols (^…) always use Yahoo Finance.
        """
        if symbol in _DEAD_SYMBOLS:
            return pd.DataFrame()

        # [G2-D5] Frequency guard for intraday
        freq_key = f"{symbol}:{interval}"
        if interval in ("1m", "5m", "15m"):
            if time.time() - _LAST_DOWNLOAD.get(freq_key, 0) < INTRADAY_MIN_AGE_S:
                cached = self._load_cache(symbol, interval, period)
                if cached is not None:
                    return cached

        # ── S-MODE: try Shoonya for intraday non-index candles ──
        _is_intraday = interval in ("1m", "3m", "5m", "10m", "15m", "30m", "1h")
        _is_index    = symbol.startswith("^")
        if cfg.is_smode and _is_intraday and not _is_index and _shoonya_broker is not None:
            try:
                df_sh = _shoonya_broker.get_historical_data(
                    symbol=symbol, interval=interval, period=period
                )
                if df_sh is not None and not df_sh.empty:
                    df_sh = self._validate(df_sh, symbol)
                    log.info(f"✅ {symbol}: {len(df_sh)} rows [Shoonya] | {df_sh.index[0]} → {df_sh.index[-1]}")
                    self._save_cache(df_sh, symbol, interval, period)
                    _LAST_DOWNLOAD[freq_key] = time.time()
                    return df_sh
                else:
                    log.debug(f"[S-MODE] Shoonya returned no data for {symbol} — falling back to Yahoo")
            except Exception as _se:
                log.debug(f"[S-MODE] Shoonya hist failed for {symbol}: {_se} — falling back to Yahoo")

        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                log.info(f"Downloading {symbol} [{interval}] period={period}"
                         + (f" (attempt {attempt})" if attempt > 1 else ""))
                df = self._fetch(symbol, interval, period, start, end)
                if df is not None and not df.empty:
                    df = self._validate(df, symbol)
                    log.info(f"✅ {symbol}: {len(df)} rows | {df.index[0]} → {df.index[-1]}")
                    self._save_cache(df, symbol, interval, period)
                    _LAST_DOWNLOAD[freq_key] = time.time()
                    return df
                if not symbol.startswith("^"):
                    log.warning(f"No data for {symbol} (attempt {attempt})")
            except Exception as e:
                last_exc = e
                err_str = str(e)
                if any(kw in err_str for kw in ("404", "Not Found", "delisted")):
                    log.warning(f"⚠️  {symbol} appears delisted — removing from session")
                    if not symbol.startswith("^"):    # [G2-D4]
                        _DEAD_SYMBOLS.add(symbol)
                    return pd.DataFrame()
                log.warning(f"Download failed for {symbol} (attempt {attempt}): {e}")

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE_S ** attempt)   # 2s, 4s

        # [G2-D2] All attempts failed — serve disk snapshot
        log.error(f"Download failed for {symbol} after {MAX_RETRIES} attempts: {last_exc}")
        cached = self._load_cache(symbol, interval, period)
        if cached is not None:
            age_h = (time.time() - self._cache_mtime(symbol, interval, period)) / 3600
            log.warning(f"⚠️  {symbol}: serving cached snapshot ({age_h:.1f}h old)")
            return cached
        return pd.DataFrame()

    def download_all(self, interval: str = "1d", period: str = "3y") -> dict:
        results = {}
        for symbol in self.symbols:
            df = self.download(symbol, interval=interval, period=period)
            if not df.empty:
                results[symbol] = df
            time.sleep(0.5)
        log.info(f"✅ Downloaded {len(results)}/{len(self.symbols)} symbols")
        return results

    def get_nifty_vix(self, period: str = "1y") -> pd.DataFrame:
        return self.download("^INDIAVIX", interval="1d", period=period)

    def get_latest_price(self, symbol: str) -> Optional[float]:
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.fast_info
            return float(info.last_price) if info.last_price else None
        except Exception as e:
            log.error(f"Price fetch failed for {symbol}: {e}")
            return None

    def get_multiple_prices(self, symbols: List[str]) -> dict:
        prices = {}
        for sym in symbols:
            price = self.get_latest_price(sym)
            if price:
                prices[sym] = price
        return prices

    # [G2-D3] Completeness gate
    def check_completeness(self, df: pd.DataFrame, symbol: str) -> float:
        if df.empty:
            return 0.0
        completeness = 1.0 - df.isnull().mean().mean()
        if completeness < COMPLETENESS_THRESHOLD:
            log.warning(
                f"⚠️  {symbol}: completeness {completeness:.1%} "
                f"< {COMPLETENESS_THRESHOLD:.0%} — ML signals degraded"
            )
        return round(completeness, 4)

    # ── Internal ──────────────────────────────────────────────────────────────
    def _fetch(self, symbol, interval, period, start, end):
        import warnings
        ticker = yf.Ticker(symbol)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = (ticker.history(start=start, end=end, interval=interval)
                  if (start and end)
                  else ticker.history(period=period, interval=interval))

        if df is None or not hasattr(df, "empty") or df.empty:
            return None

        if hasattr(df.columns, "levels"):
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

        available = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
        if len(available) < 4:
            log.warning(f"{symbol}: missing OHLC columns {df.columns.tolist()}")
            return None

        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.columns = ["open", "high", "low", "close", "volume"]
        df.index.name = "timestamp"
        return df

    def _validate(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        before = len(df)
        bad = (df["high"] < df[["open", "close"]].max(axis=1)) | \
              (df["low"]  > df[["open", "close"]].min(axis=1))
        if bad.sum():
            df = df[~bad]
        if not symbol.startswith("^"):
            df = df[df["volume"] > 0]
        after = len(df)
        if before != after:
            log.info(f"{symbol}: {before - after} rows removed in validation")
        return df.dropna()

    # ── Disk cache ────────────────────────────────────────────────────────────
    def _cache_path(self, symbol, interval, period) -> Path:
        safe = symbol.replace("^", "IDX_").replace(".", "_")
        key  = hashlib.md5(f"{safe}_{interval}_{period}".encode()).hexdigest()[:8]
        return CACHE_DIR / f"{safe}_{interval}_{key}.parquet"

    def _save_cache(self, df, symbol, interval, period):
        try:
            df.to_parquet(self._cache_path(symbol, interval, period))
        except Exception as e:
            log.debug(f"Cache save failed for {symbol}: {e}")

    def _load_cache(self, symbol, interval, period) -> Optional[pd.DataFrame]:
        path = self._cache_path(symbol, interval, period)
        if path.exists():
            try:
                return pd.read_parquet(path)
            except Exception:
                path.unlink(missing_ok=True)
        return None

    def _cache_mtime(self, symbol, interval, period) -> float:
        p = self._cache_path(symbol, interval, period)
        return p.stat().st_mtime if p.exists() else 0.0
