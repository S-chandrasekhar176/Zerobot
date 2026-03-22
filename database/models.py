# -*- coding: utf-8 -*-
"""
ZeroBot — PostgreSQL Database Models (SQLAlchemy ORM)
Tables: trades, positions, signals, ohlcv_1min, ohlcv_daily,
        model_runs, risk_events, audit_log, bot_state
"""
from datetime import datetime
from typing import Optional
from sqlalchemy import (
    create_engine, Column, Integer, Float, String, Boolean,
    DateTime, JSON, Text, BigInteger, Index, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy.pool import NullPool
from core.config import cfg
from core.logger import log

Base = declarative_base()


# ═══════════════════════════════════════════════════════════════
#  TRADES — Every executed trade (paper or live)
# ═══════════════════════════════════════════════════════════════
class Trade(Base):
    __tablename__ = "trades"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    trade_date  = Column(DateTime, default=datetime.utcnow, index=True)
    symbol      = Column(String(30), nullable=False, index=True)
    side        = Column(String(5), nullable=False)   # BUY | SELL
    qty         = Column(Integer, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price  = Column(Float, nullable=True)
    entry_time  = Column(DateTime, nullable=False)
    exit_time   = Column(DateTime, nullable=True)
    pnl         = Column(Float, nullable=True)
    pnl_pct     = Column(Float, nullable=True)
    brokerage   = Column(Float, default=0.0)
    stt         = Column(Float, default=0.0)
    other_costs = Column(Float, default=0.0)
    net_pnl     = Column(Float, nullable=True)      # PnL after all costs
    strategy    = Column(String(50), nullable=True)
    signal_conf = Column(Float, nullable=True)      # ML confidence %
    trigger     = Column(String(100), nullable=True) # What triggered signal
    mode        = Column(String(10), default="paper") # paper | live
    order_id    = Column(String(50), nullable=True)
    status      = Column(String(20), default="OPEN") # OPEN | CLOSED | CANCELLED
    notes       = Column(Text, nullable=True)


# ═══════════════════════════════════════════════════════════════
#  SIGNALS — Every signal generated (acted on or not)
# ═══════════════════════════════════════════════════════════════
class Signal(Base):
    __tablename__ = "signals"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    generated_at= Column(DateTime, default=datetime.utcnow, index=True)
    symbol      = Column(String(30), nullable=False)
    signal_type = Column(String(10), nullable=False)  # BUY | SELL | HOLD
    confidence  = Column(Float, nullable=False)
    strategy    = Column(String(50), nullable=True)
    features    = Column(JSON, nullable=True)          # Feature values at signal time
    acted_on    = Column(Boolean, default=False)
    blocked_reason = Column(String(200), nullable=True)
    trade_id    = Column(Integer, nullable=True)       # FK to trades if acted


# ═══════════════════════════════════════════════════════════════
#  POSITIONS — Current & historical positions
# ═══════════════════════════════════════════════════════════════
class Position(Base):
    __tablename__ = "positions"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    symbol       = Column(String(30), nullable=False, index=True)
    side         = Column(String(5), nullable=False)   # LONG | SHORT
    qty          = Column(Integer, nullable=False)
    avg_price    = Column(Float, nullable=False)
    current_price= Column(Float, nullable=True)
    unrealized_pnl = Column(Float, default=0.0)
    stop_loss    = Column(Float, nullable=True)
    target       = Column(Float, nullable=True)
    strategy     = Column(String(50), nullable=True)
    opened_at    = Column(DateTime, default=datetime.utcnow)
    closed_at    = Column(DateTime, nullable=True)
    is_open      = Column(Boolean, default=True)
    mode         = Column(String(10), default="paper")


# ═══════════════════════════════════════════════════════════════
#  OHLCV — Price data (partitioned by timeframe)
# ═══════════════════════════════════════════════════════════════
class OHLCV1Min(Base):
    __tablename__ = "ohlcv_1min"
    __table_args__ = (
        UniqueConstraint("symbol", "timestamp", name="uq_ohlcv_1min_sym_ts"),
        Index("ix_ohlcv_1min_sym_ts", "symbol", "timestamp"),
    )

    id        = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol    = Column(String(30), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    open      = Column(Float, nullable=False)
    high      = Column(Float, nullable=False)
    low       = Column(Float, nullable=False)
    close     = Column(Float, nullable=False)
    volume    = Column(BigInteger, nullable=False)


class OHLCVDaily(Base):
    __tablename__ = "ohlcv_daily"
    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uq_ohlcv_daily_sym_date"),
    )

    id      = Column(Integer, primary_key=True, autoincrement=True)
    symbol  = Column(String(30), nullable=False, index=True)
    date    = Column(DateTime, nullable=False)
    open    = Column(Float)
    high    = Column(Float)
    low     = Column(Float)
    close   = Column(Float)
    volume  = Column(BigInteger)
    adj_close = Column(Float, nullable=True)


# ═══════════════════════════════════════════════════════════════
#  MODEL RUNS — Track ML model training & performance
# ═══════════════════════════════════════════════════════════════
class ModelRun(Base):
    __tablename__ = "model_runs"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    trained_at  = Column(DateTime, default=datetime.utcnow)
    model_name  = Column(String(50), nullable=False)  # xgboost | lstm | ensemble
    version     = Column(String(20), nullable=True)
    accuracy    = Column(Float, nullable=True)
    precision   = Column(Float, nullable=True)
    recall      = Column(Float, nullable=True)
    f1_score    = Column(Float, nullable=True)
    sharpe      = Column(Float, nullable=True)
    train_period= Column(String(50), nullable=True)
    features    = Column(JSON, nullable=True)
    hyperparams = Column(JSON, nullable=True)
    model_path  = Column(String(200), nullable=True)
    is_active   = Column(Boolean, default=True)


# ═══════════════════════════════════════════════════════════════
#  RISK EVENTS — Every blocked order, breach, alert
# ═══════════════════════════════════════════════════════════════
class RiskEvent(Base):
    __tablename__ = "risk_events"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    timestamp   = Column(DateTime, default=datetime.utcnow, index=True)
    event_type  = Column(String(50), nullable=False)  # BLOCKED | BREACH | HALT | RESUME
    symbol      = Column(String(30), nullable=True)
    reason      = Column(String(500), nullable=False)
    severity    = Column(String(20), default="INFO")  # CRITICAL | HIGH | MEDIUM | INFO
    details     = Column(JSON, nullable=True)


# ═══════════════════════════════════════════════════════════════
#  AUDIT LOG — Immutable record of all bot actions
# ═══════════════════════════════════════════════════════════════
class AuditLog(Base):
    __tablename__ = "audit_log"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    action    = Column(String(100), nullable=False)
    actor     = Column(String(50), default="bot")   # bot | user | system
    details   = Column(JSON, nullable=True)
    ip        = Column(String(45), nullable=True)


# ═══════════════════════════════════════════════════════════════
#  BOT STATE — Persisted state snapshots
# ═══════════════════════════════════════════════════════════════
class BotStateRecord(Base):
    __tablename__ = "bot_state"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    saved_at   = Column(DateTime, default=datetime.utcnow)
    state_json = Column(JSON, nullable=False)
    capital    = Column(Float, nullable=False)
    daily_pnl  = Column(Float, default=0.0)
    mode       = Column(String(10), default="paper")


# ═══════════════════════════════════════════════════════════════
#  DATABASE ENGINE & SESSION
# ═══════════════════════════════════════════════════════════════
_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        try:
            _engine = create_engine(
                cfg.database.url,
                pool_size=cfg.database.pool_size,
                max_overflow=cfg.database.max_overflow,
                echo=cfg.database.echo,
                pool_pre_ping=True,   # Reconnect if connection died
                connect_args={"client_encoding": "utf8"},
            )
            log.info("✅ Database engine created")
        except Exception as e:
            log.error(f"Database engine creation failed: {e}")
            raise
    return _engine


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autocommit=False, autoflush=False)
    return _SessionLocal()


def init_db():
    """Create all tables if they don't exist."""
    try:
        engine = get_engine()
        Base.metadata.create_all(engine)
        log.info("✅ Database tables initialized")
    except Exception as e:
        log.error(f"Database init failed: {e}")
        raise


def drop_all():
    """WARNING: Drops ALL tables. Use only in dev."""
    engine = get_engine()
    Base.metadata.drop_all(engine)
    log.warning("⚠️ All tables dropped")
