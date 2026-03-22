"""
ZeroBot — Token Manager
Angel One JWT tokens expire every 24 hours.
This auto-refreshes at the 23-hour mark and alerts on failure.
"""
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from core.logger import log
from core.config import cfg


class TokenManager:
    """Manages Angel One JWT token lifecycle."""

    REFRESH_BEFORE_EXPIRY_HOURS = 1  # Refresh 1 hour before expiry

    def __init__(self, broker=None, alert_fn=None):
        self._broker = broker
        self._alert_fn = alert_fn   # Telegram alert function
        self._token_issued_at: Optional[datetime] = None
        self._token_expires_at: Optional[datetime] = None
        self._is_valid = False
        self._refresh_failures = 0
        self.MAX_FAILURES = 3

    def register_token(self, issued_at: datetime = None):
        """Call after successful login."""
        self._token_issued_at = issued_at or datetime.now()
        self._token_expires_at = self._token_issued_at + timedelta(hours=24)
        self._is_valid = True
        self._refresh_failures = 0
        log.info(f"✅ Token registered. Expires: {self._token_expires_at.strftime('%H:%M:%S')}")

    def hours_until_expiry(self) -> float:
        if not self._token_expires_at:
            return 0.0
        delta = self._token_expires_at - datetime.now()
        return max(0, delta.total_seconds() / 3600)

    def needs_refresh(self) -> bool:
        return self.hours_until_expiry() <= self.REFRESH_BEFORE_EXPIRY_HOURS

    async def ensure_valid(self):
        """Call before any API request to ensure token is fresh."""
        if not self._is_valid or self.needs_refresh():
            await self._do_refresh()

    async def _do_refresh(self):
        """Attempt token refresh with retry and backoff."""
        for attempt in range(1, self.MAX_FAILURES + 1):
            try:
                if self._broker:
                    self._broker.refresh_token()
                self.register_token()
                log.info("✅ Token refreshed successfully")
                return
            except Exception as e:
                self._refresh_failures += 1
                wait = 2 ** attempt * 60  # 2min, 4min, 8min backoff
                log.error(f"Token refresh attempt {attempt} failed: {e}. Retrying in {wait//60}min")
                if self._alert_fn:
                    await self._alert_fn(
                        f"⚠️ Token refresh failed (attempt {attempt}/{self.MAX_FAILURES}): {e}",
                        priority="CRITICAL"
                    )
                if attempt < self.MAX_FAILURES:
                    await asyncio.sleep(wait)

        # All retries failed
        self._is_valid = False
        log.critical("🚨 Token refresh FAILED after all retries. Trading PAUSED.")
        if self._alert_fn:
            await self._alert_fn("🚨 CRITICAL: Token refresh failed. Bot PAUSED. Manual login required.", priority="CRITICAL")

    def get_status(self) -> dict:
        return {
            "is_valid": self._is_valid,
            "issued_at": self._token_issued_at.isoformat() if self._token_issued_at else None,
            "expires_at": self._token_expires_at.isoformat() if self._token_expires_at else None,
            "hours_until_expiry": round(self.hours_until_expiry(), 2),
            "needs_refresh": self.needs_refresh(),
            "refresh_failures": self._refresh_failures,
        }
