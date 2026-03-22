"""
ZeroBot — Config Loader  [FIXED VERSION]
Loads settings.yaml + .env into a single typed config object.
Access anywhere: from core.config import cfg

FIX LOG:
  [BUG-4 FIX] AngelOneConfig.is_configured now requires totp_secret to be set
  [CFG-FIX-1] cfg.set() helper added — replaces unsafe cfg.__dict__[] mutation
              used in dashboard/api/main.py set_mode / update_config endpoints
"""
import os
from pathlib import Path
from typing import Dict, List, Optional
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / "config" / ".env", encoding="utf-8-sig", override=True)
_root_env = BASE_DIR / ".env"
if _root_env.exists():
    load_dotenv(_root_env, encoding="utf-8-sig", override=False)


class DatabaseConfig(BaseModel):
    use_sqlite: bool = os.getenv("USE_SQLITE", "true").lower() != "false"
    sqlite_path: str = str(BASE_DIR / "data" / "zerobot.db")
    host: str = os.getenv("DB_HOST", "localhost")
    port: int = int(os.getenv("DB_PORT", 5432))
    name: str = os.getenv("DB_NAME", "zerobot")
    user: str = os.getenv("DB_USER", "zerobot_user")
    password: str = os.getenv("DB_PASSWORD", "")
    pool_size: int = 5
    max_overflow: int = 10
    echo: bool = False

    model_config = {"arbitrary_types_allowed": True}

    @property
    def url(self) -> str:
        if self.use_sqlite:
            from pathlib import Path as _P
            _P(self.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
            return f"sqlite:///{self.sqlite_path}"
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def is_postgres(self) -> bool:
        return not self.use_sqlite


class TelegramConfig(BaseModel):
    enabled: bool = True
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    throttle_seconds: int = 30


class AngelOneConfig(BaseModel):
    api_key: str = os.getenv("ANGEL_API_KEY", "")
    client_id: str = os.getenv("ANGEL_CLIENT_ID", "")
    mpin: str = os.getenv("ANGEL_MPIN", "")
    totp_secret: str = os.getenv("ANGEL_TOTP_SECRET", "")

    @property
    def is_configured(self) -> bool:
        # [BUG-4 FIX] TOTP secret is mandatory — without it connect() will generate
        # an invalid TOTP from an empty seed, causing silent auth failures.
        return bool(self.api_key and self.client_id and self.mpin and self.totp_secret)

    @property
    def missing_fields(self) -> list:
        missing = []
        if not self.api_key:     missing.append("ANGEL_API_KEY")
        if not self.client_id:   missing.append("ANGEL_CLIENT_ID")
        if not self.mpin:        missing.append("ANGEL_MPIN")
        if not self.totp_secret: missing.append("ANGEL_TOTP_SECRET")
        return missing


class ShounyaConfig(BaseModel):
    user_id:      str = os.getenv("SHOONYA_USER", "")
    password:     str = os.getenv("SHOONYA_PASSWORD", "")
    totp_secret:  str = os.getenv("SHOONYA_TOTP_SECRET", "")
    vendor_code:  str = os.getenv("SHOONYA_VENDOR_CODE", "")
    api_key:      str = os.getenv("SHOONYA_API_KEY", "")
    imei:         str = os.getenv("SHOONYA_IMEI") or "abc1234"

    @property
    def totp_key(self) -> str:
        return self.totp_secret

    @property
    def is_configured(self) -> bool:
        return bool(self.user_id and self.totp_secret and self.api_key)

    @property
    def missing_fields(self) -> list:
        missing = []
        if not self.user_id:      missing.append("SHOONYA_USER")
        if not self.totp_secret:  missing.append("SHOONYA_TOTP_SECRET")
        if not self.api_key:      missing.append("SHOONYA_API_KEY")
        if not self.vendor_code:  missing.append("SHOONYA_VENDOR_CODE")
        return missing


class RiskConfig(BaseModel):
    max_position_pct: float = 0.10
    max_daily_loss_pct: float = 0.02
    max_open_trades: int = 5
    max_per_trade_risk_pct: float = 2.0
    max_open_positions: int = 5
    max_sector_exposure_pct: float = 30.0
    max_single_stock_pct: float = 20.0
    margin_buffer_pct: float = 20.0
    consecutive_loss_limit: int = 5
    vix_halt_threshold: float = 25.0
    vix_defensive_threshold: float = 20.0
    min_confidence: float = 0.60
    kelly_fraction: float = 0.25
    max_drawdown_pct: float = 0.15
    trailing_stop_pct: float = 1.5
    tiered_exit_enabled: bool = True
    tiered_exit_at_pct: float = 0.5


class PaperBrokerConfig(BaseModel):
    slippage_pct: float = 0.05
    order_timeout_seconds: int = 30
    brokerage_per_order: float = 20.0
    stt_intraday_pct: float = 0.025
    stamp_duty_pct: float = 0.015
    exchange_charges_pct: float = 0.00335
    gst_pct: float = 18.0


class OptionsConfig(BaseModel):
    underlyings: List[str] = ["^NSEI", "^NSEBANK", "RELIANCE.NS", "HDFCBANK.NS"]
    buy_calls_puts: bool = True
    sell_covered_calls: bool = False
    iron_condor: bool = False
    strike_selection: str = "atm_plus_1"
    expiry: str = "weekly"
    max_premium_per_trade_pct: float = 2.0
    max_option_positions: int = 3
    min_days_to_expiry: int = 2
    max_days_to_expiry: int = 15
    min_iv_percentile: float = 20.0
    max_iv_percentile: float = 80.0
    profit_target_pct: float = 50.0
    stop_loss_pct: float = 50.0
    lot_size_override: Optional[int] = None

    LOT_SIZES: Dict[str, int] = {
        "^NSEI":     50,
        "^NSEBANK":  15,
        "RELIANCE.NS": 250,
        "HDFCBANK.NS": 550,
        "ICICIBANK.NS": 700,
        "TCS.NS":    150,
        "INFY.NS":   300,
        "SBIN.NS":   750,
    }

    def lot_size(self, symbol: str) -> int:
        if self.lot_size_override:
            return self.lot_size_override
        return self.LOT_SIZES.get(symbol, 100)

    @property
    def strike_offset(self) -> int:
        return {"atm": 0, "atm_plus_1": 1, "atm_plus_2": 2}.get(self.strike_selection, 1)


class ZeroBotConfig(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    name: str = "ZeroBot"
    version: str = "1.1"
    mode: str = "paper"
    trading_mode: str = "stocks"
    log_level: str = "INFO"
    state_save_interval: int = 30
    initial_capital: float = 10000.0
    broker_name: str = "paper"

    risk: RiskConfig = RiskConfig()
    paper_broker: PaperBrokerConfig = PaperBrokerConfig()
    database: DatabaseConfig = DatabaseConfig()
    telegram: TelegramConfig = TelegramConfig()
    angel_one: AngelOneConfig = AngelOneConfig()
    shoonya: ShounyaConfig = ShounyaConfig()
    groq_api_key: str = ""
    options: OptionsConfig = OptionsConfig()

    exchange: str = "NSE"
    session_start: str = "09:15"
    session_end: str = "15:25"
    warmup_end: str = "09:30"
    closing_avoid: str = "15:20"
    timezone: str = "Asia/Kolkata"

    ml_min_confidence: float = 65.0
    ml_retrain_interval_days: int = 7
    use_finbert: bool = False
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8000

    symbols: List[str] = [
        "^NSEI", "^NSEBANK", "RELIANCE.NS", "TCS.NS",
        "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS", "SBIN.NS",
        "AXISBANK.NS", "WIPRO.NS", "ITC.NS", "KOTAKBANK.NS",
    ]

    # Runtime overrides (session-only, not persisted to yaml)
    _overrides: dict = {}

    @property
    def is_paper(self) -> bool:
        return self.mode == "paper"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def is_hybrid(self) -> bool:
        """Legacy compat — True if using Angel One data + paper execution."""
        return self.broker_name in ("a_paper", "a-paper", "hybrid", "angel_paper")

    @property
    def is_smode(self) -> bool:
        """True if using Shoonya data feed."""
        return self.broker_name in (
            "s_paper", "s-paper", "shoonya_paper", "s_mode", "smode",
            "s_live", "s-live", "shoonya_live", "shoonya"
        )

    @property
    def uses_angel_data(self) -> bool:
        """True if Angel One WebSocket provides tick data."""
        return self.broker_name in (
            "a_paper", "a-paper", "hybrid", "angel_paper",
            "a_live", "a-live", "angel_live", "angel",
            "dual", "dual_mode", "dual-mode"
        )

    @property
    def uses_shoonya_data(self) -> bool:
        """True if Shoonya WebSocket provides tick data."""
        return self.is_smode

    @property
    def is_paper_execution(self) -> bool:
        """True if no real orders will be sent."""
        return self.mode == "paper"

    @property
    def uses_real_data(self) -> bool:
        return self.uses_angel_data or self.uses_shoonya_data

    def set(self, key: str, value) -> bool:
        """
        [CFG-FIX-1] Safe session-only config override.
        Use this instead of cfg.__dict__[key] = value.
        Example:  cfg.set("mode", "live")
                  cfg.set("broker_name", "angel")
        Allowed keys: mode, trading_mode, broker_name, groq_api_key
        Returns True if key was accepted, False if key is not whitelisted.
        """
        ALLOWED = {"mode", "trading_mode", "broker_name", "groq_api_key", "initial_capital"}
        if key not in ALLOWED:
            return False
        # Pydantic v2: use object.__setattr__ to bypass frozen model
        try:
            object.__setattr__(self, key, value)
        except Exception:
            self.__dict__[key] = value  # fallback for Pydantic v1
        return True


def load_config() -> ZeroBotConfig:
    """Load config from settings.yaml, override with env vars."""
    yaml_path = BASE_DIR / "config" / "settings.yaml"
    raw = {}
    if yaml_path.exists():
        with open(yaml_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    bot          = raw.get("bot", {})
    capital      = raw.get("capital", {})
    market       = raw.get("market", {})
    risk_raw     = raw.get("risk", {})
    paper_raw    = raw.get("paper_broker", {})
    ml_raw       = raw.get("ml", {})
    data_raw     = raw.get("data", {})
    dash_raw     = raw.get("dashboard", {})
    opts_raw     = raw.get("options", {})
    broker_raw   = raw.get("broker", {})
    sentiment_raw= raw.get("sentiment", {})
    shoonya_raw  = raw.get("shoonya", {})

    opts_strat = opts_raw.pop("strategies", {}) if isinstance(opts_raw, dict) else {}
    if opts_strat:
        opts_raw.update(opts_strat)
    options_cfg = OptionsConfig(**{k: v for k, v in opts_raw.items()
                                   if k in OptionsConfig.model_fields}) if opts_raw else OptionsConfig()

    risk_cfg = RiskConfig(**{k: v for k, v in risk_raw.items()
                             if k in RiskConfig.model_fields}) if risk_raw else RiskConfig()

    def _env(key, fallback=""):
        v = os.getenv(key, "").strip().lstrip("\ufeff").strip()
        return v or str(fallback).strip()

    shoonya_cfg = ShounyaConfig(
        user_id     = _env("SHOONYA_USER",         shoonya_raw.get("user_id",     "")),
        password    = _env("SHOONYA_PASSWORD",      shoonya_raw.get("password",    "")),
        totp_secret = _env("SHOONYA_TOTP_SECRET",   shoonya_raw.get("totp_secret", "")),
        vendor_code = _env("SHOONYA_VENDOR_CODE",   shoonya_raw.get("vendor_code", "")),
        api_key     = _env("SHOONYA_API_KEY",       shoonya_raw.get("api_key",     "")),
        imei        = _env("SHOONYA_IMEI",          shoonya_raw.get("imei", "abc1234")) or "abc1234",
    )

    return ZeroBotConfig(
        name=bot.get("name", "ZeroBot"),
        version=bot.get("version", "1.1"),
        mode=bot.get("mode", "paper"),
        trading_mode=bot.get("trading_mode", "stocks"),
        log_level=bot.get("log_level", "INFO"),
        state_save_interval=bot.get("state_save_interval", 30),
        # Read capital from paper_broker.initial_capital (primary) or capital.initial (fallback)
        initial_capital=float(paper_raw.get("initial_capital", capital.get("initial", 100000))),
        broker_name=broker_raw.get("name", "paper") if isinstance(broker_raw, dict) else "paper",
        exchange=market.get("exchange", "NSE"),
        session_start=market.get("session_start", "09:15"),
        session_end=market.get("session_end", "15:25"),
        warmup_end=market.get("warmup_end", "09:30"),
        closing_avoid=market.get("closing_avoid", "15:20"),
        timezone=market.get("timezone", "Asia/Kolkata"),
        risk=risk_cfg,
        paper_broker=PaperBrokerConfig(**paper_raw) if paper_raw else PaperBrokerConfig(),
        ml_min_confidence=float(ml_raw.get("min_confidence", 65.0)),
        ml_retrain_interval_days=int(ml_raw.get("retrain_interval_days", 7)),
        use_finbert=bool(sentiment_raw.get("use_finbert", False)),
        symbols=data_raw.get("symbols", ZeroBotConfig.model_fields["symbols"].default),
        dashboard_host=dash_raw.get("host", "127.0.0.1"),
        dashboard_port=int(dash_raw.get("port", 8000)),
        options=options_cfg,
        shoonya=shoonya_cfg,
        groq_api_key=os.getenv("GROQ_API_KEY", ""),
    )


# [MEDIUM#15] Global singleton with validation
def _validate_config(cfg_inst: ZeroBotConfig) -> None:
    """Validate critical config values."""
    VALID_MODES = {"paper", "live"}
    if cfg_inst.mode not in VALID_MODES:
        raise ValueError(f"❌ Invalid mode '{cfg_inst.mode}'. Must be one of: {VALID_MODES}")
    
    VALID_TRADING_MODES = {"stocks", "options", "both"}
    if cfg_inst.trading_mode not in VALID_TRADING_MODES:
        raise ValueError(f"❌ Invalid trading_mode '{cfg_inst.trading_mode}'. Must be one of: {VALID_TRADING_MODES}")
    
    if cfg_inst.initial_capital <= 0:
        raise ValueError(f"❌ initial_capital must be > 0, got {cfg_inst.initial_capital}")
    
    if cfg_inst.risk.max_daily_loss_pct < 0 or cfg_inst.risk.max_daily_loss_pct > 100:
        raise ValueError(f"❌ max_daily_loss_pct must be 0-100, got {cfg_inst.risk.max_daily_loss_pct}")


# Global singleton — import this everywhere
_cfg_inst = load_config()
_validate_config(_cfg_inst)
cfg = _cfg_inst
