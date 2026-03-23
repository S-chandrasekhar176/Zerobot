"""
ZeroBot Secure Configuration Module

This module loads all configuration from environment variables.
IMPORTANT: Never hardcode credentials. Always use os.getenv().

Security best practices:
  1. All credentials come from environment variables
  2. config/.env is git-ignored (never committed)
  3. Use config/.env.example as a template for setup
  4. Rotate credentials immediately if exposed
  5. Use pre-commit hooks to prevent future leaks
"""

import os
from pathlib import Path
from typing import Optional

# Ensure .env is loaded FIRST before any config access
from dotenv import load_dotenv

_ENV_PATH = Path(__file__).parent / ".env"
_ENV_EXAMPLE_PATH = Path(__file__).parent / ".env.example"

# Load environment variables from .env (git-ignored)
load_dotenv(_ENV_PATH, override=True)

# Fallback: create empty .env if missing
if not _ENV_PATH.exists():
    _ENV_PATH.touch()


class SecureConfig:
    """
    Central secure configuration loader using environment variables only.
    
    Never stores plain credentials in memory longer than needed.
    All credentials are read directly from os.environ on access.
    """
    
    @staticmethod
    def get_credential(key: str, required: bool = False) -> Optional[str]:
        """
        Retrieve credential from environment variable.
        
        Args:
            key (str): Environment variable name (e.g., 'GROQ_API_KEY')
            required (bool): Raise error if missing and required
        
        Returns:
            str or None: Credential value, or None if not set
        
        Raises:
            RuntimeError: If required credential is missing
        """
        value = os.getenv(key)
        
        if required and not value:
            raise RuntimeError(
                f"❌ SECURITY: Required credential '{key}' not found in environment. "
                f"Set it in config/.env or as system environment variable."
            )
        
        # Log access (without exposing value)
        if value:
            # Mask credential for logging (show first 3 and last 3 chars)
            masked = f"{value[:3]}...{value[-3:]}" if len(value) > 6 else "***"
            # print(f"  ✓ {key} loaded ({masked})")
        
        return value
    
    # ──────────────────────────────────────────────────────────────────────
    # GROQ API (LLM for Gates 6 & 11)
    # ──────────────────────────────────────────────────────────────────────
    
    @property
    def groq_api_key(self) -> Optional[str]:
        """Groq API key for LLM features."""
        return self.get_credential("GROQ_API_KEY", required=False)
    
    @property
    def groq_enabled(self) -> bool:
        """Whether Groq integration is enabled."""
        return bool(self.groq_api_key)
    
    # ──────────────────────────────────────────────────────────────────────
    # SHOONYA / FINVASIA (Real-time data + execution)
    # ──────────────────────────────────────────────────────────────────────
    
    @property
    def shoonya_login(self) -> Optional[str]:
        """Shoonya username."""
        return self.get_credential("SHOONYA_LOGIN", required=False)
    
    @property
    def shoonya_password(self) -> Optional[str]:
        """Shoonya password."""
        return self.get_credential("SHOONYA_PASSWORD", required=False)
    
    @property
    def shoonya_totp_secret(self) -> Optional[str]:
        """Shoonya 2FA TOTP secret (base32 encoded)."""
        return self.get_credential("SHOONYA_TOTP_SECRET", required=False)
    
    @property
    def shoonya_api_key(self) -> Optional[str]:
        """Shoonya API key."""
        return self.get_credential("SHOONYA_API_KEY", required=False)
    
    @property
    def shoonya_vendor_code(self) -> Optional[str]:
        """Shoonya vendor code."""
        return self.get_credential("SHOONYA_VENDOR_CODE", required=False)
    
    @property
    def shoonya_imei(self) -> Optional[str]:
        """Shoonya device IMEI."""
        return self.get_credential("SHOONYA_IMEI", required=False)
    
    # ──────────────────────────────────────────────────────────────────────
    # ANGEL ONE (Data + execution)
    # ──────────────────────────────────────────────────────────────────────
    
    @property
    def angel_api_key(self) -> Optional[str]:
        """Angel One Smart API key."""
        return self.get_credential("ANGEL_API_KEY", required=False)
    
    @property
    def angel_client_id(self) -> Optional[str]:
        """Angel One client ID."""
        return self.get_credential("ANGEL_CLIENT_ID", required=False)
    
    @property
    def angel_password(self) -> Optional[str]:
        """Angel One password."""
        return self.get_credential("ANGEL_PASSWORD", required=False)
    
    @property
    def angel_totp_secret(self) -> Optional[str]:
        """Angel One 2FA TOTP secret (base32 encoded)."""
        return self.get_credential("ANGEL_TOTP_SECRET", required=False)
    
    # ──────────────────────────────────────────────────────────────────────
    # TELEGRAM ALERTS
    # ──────────────────────────────────────────────────────────────────────
    
    @property
    def telegram_token(self) -> Optional[str]:
        """Telegram bot token."""
        return self.get_credential("TELEGRAM_BOT_TOKEN", required=False)
    
    @property
    def telegram_chat_id(self) -> Optional[str]:
        """Telegram chat ID where alerts are sent."""
        return self.get_credential("TELEGRAM_CHAT_ID", required=False)
    
    # ──────────────────────────────────────────────────────────────────────
    # DATABASE
    # ──────────────────────────────────────────────────────────────────────
    
    @property
    def db_password(self) -> Optional[str]:
        """Database password."""
        return self.get_credential("DB_PASSWORD", required=False)
    
    @property
    def database_url(self) -> Optional[str]:
        """Full database URL (PostgreSQL)."""
        return self.get_credential("DATABASE_URL", required=False)
    
    # ──────────────────────────────────────────────────────────────────────
    # OPTIONAL INTEGRATIONS
    # ──────────────────────────────────────────────────────────────────────
    
    @property
    def openrouter_api_key(self) -> Optional[str]:
        """OpenRouter API key (fallback LLM)."""
        return self.get_credential("OPENROUTER_API_KEY", required=False)
    
    @property
    def dashboard_password(self) -> Optional[str]:
        """Dashboard HTTP Basic Auth password."""
        return self.get_credential("DASHBOARD_PASS", required=False)


# Global instance
secure_config = SecureConfig()


# ══════════════════════════════════════════════════════════════════════════════
# USAGE EXAMPLES
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("✅ Secure configuration module loaded")
    print(f"  Groq enabled: {secure_config.groq_enabled}")
    print(f"  Telegram enabled: {bool(secure_config.telegram_token)}")
    print(f"  Shoonya configured: {bool(secure_config.shoonya_login)}")
