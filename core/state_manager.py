# -*- coding: utf-8 -*-
"""
ZeroBot v2 — State Manager (Full Fix)
- Capital always loaded from settings.yaml
- All trades/signals/positions saved to PostgreSQL
- Fallback to JSON if DB not available
- uptime tracking from bot start
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from core.logger import log
from core.config import cfg

_FALLBACK_FILE = Path(__file__).parent.parent / "logs" / "state_backup.json"
_FALLBACK_FILE.parent.mkdir(exist_ok=True)


class BotState:
    def __init__(self):
        self.mode: str = cfg.mode
        self.status: str = "STOPPED"
        self.capital: float = cfg.initial_capital      # Always from settings.yaml
        self.available_margin: float = cfg.initial_capital
        self.daily_pnl: float = 0.0
        self.total_pnl: float = 0.0
        self.daily_trades: int = 0
        self.daily_wins: int = 0
        self.daily_losses: int = 0
        self.consecutive_losses: int = 0
        self.open_positions: Dict[str, Any] = {}
        self.active_orders: Dict[str, Any] = {}
        self.halted_reason: Optional[str] = None
        self.started_at: Optional[datetime] = None
        self.last_saved: Optional[datetime] = None
        self.peak_capital: float = cfg.initial_capital
        self.all_time_high: float = cfg.initial_capital
        # market_data: written by engine realtime feed and _run_strategy_cycle
        # so risk engine gates (VIX, etc.) always have a real value to read.
        # P16-FIX: Default VIX = 18.0 (borderline) instead of 15.0.
        # When live VIX fetch fails repeatedly, using 15.0 would incorrectly
        # allow trading as if markets are calm. 18.0 is the VIX halt threshold
        # so a failed fetch will NOT incorrectly unblock the VIX gate.
        self.market_data: Dict[str, Any] = {"india_vix": 18.0}

    @property
    def is_halted(self) -> bool:
        """True when the bot has been halted (risk limit breach or manual stop)."""
        return self.status == "HALTED"

    @is_halted.setter
    def is_halted(self, value: bool):
        """Setting is_halted=True halts the bot; False resumes it."""
        self.status = "HALTED" if value else "RUNNING"

    @property
    def total_capital(self) -> float:
        return self.capital + self.daily_pnl

    @property
    def drawdown_pct(self) -> float:
        if self.peak_capital == 0:
            return 0.0
        return max(0.0, (self.peak_capital - self.total_capital) / self.peak_capital * 100)

    @property
    def win_rate(self) -> float:
        total = self.daily_wins + self.daily_losses
        return round(self.daily_wins / total, 4) if total > 0 else 0.0

    @property
    def daily_loss_used_pct(self) -> float:
        limit = self.capital * (cfg.risk.max_daily_loss_pct / 100)
        loss = abs(min(0, self.daily_pnl))
        return (loss / limit * 100) if limit > 0 else 0.0

    def update_pnl(self, pnl_delta: float):
        self.daily_pnl += pnl_delta
        self.total_pnl += pnl_delta
        if self.total_capital > self.peak_capital:
            self.peak_capital = self.total_capital
        if self.total_capital > self.all_time_high:
            self.all_time_high = self.total_capital

    def to_dict(self) -> Dict:
        return {
            "mode": self.mode,
            "status": self.status,
            "capital": self.capital,
            "available_margin": self.available_margin,
            "daily_pnl": self.daily_pnl,
            "total_pnl": self.total_pnl,
            "daily_trades": self.daily_trades,
            "daily_wins": self.daily_wins,
            "daily_losses": self.daily_losses,
            "consecutive_losses": self.consecutive_losses,
            "open_positions": self.open_positions,
            "halted_reason": self.halted_reason,
            "peak_capital": self.peak_capital,
            "all_time_high": self.all_time_high,
            "last_saved": datetime.now().isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "BotState":
        state = cls()
        skip = {"capital", "available_margin"}  # always re-read from cfg
        datetime_fields = {"started_at", "last_saved"}
        for k, v in data.items():
            if k in skip:
                continue
            if k in datetime_fields and isinstance(v, str):
                try:
                    v = datetime.fromisoformat(v)
                except Exception:
                    v = None
            if hasattr(state, k) and not callable(getattr(type(state), k, None)):
                setattr(state, k, v)
        # Always enforce configured capital
        state.capital = cfg.initial_capital
        state.available_margin = cfg.initial_capital
        # PATCH7-FIX-CRITICAL: Reset daily P&L and peak capital on every startup.
        # ZeroBot is an intraday bot — each session is a fresh day.
        # Carrying over daily_pnl from a previous DB session causes a false
        # drawdown reading (e.g. peak=55000, daily_pnl=-24750 → 45% drawdown
        # on startup with zero trades). Must reset both together.
        state.daily_pnl = 0.0
        state.peak_capital = cfg.initial_capital   # fresh peak for new session
        # PATCH12 FIX: Reset all daily counters on every startup.
        # ZeroBot is intraday-only — daily_trades/wins/losses from previous
        # session carry over and show "1 trade" / wrong win-rate on startup.
        state.daily_trades = 0
        state.daily_wins = 0
        state.daily_losses = 0
        state.consecutive_losses = 0
        # PATCH13 FIX: Clear stale intraday positions on startup.
        # ZeroBot is intraday-only — all positions auto-squareoff at 3:15 PM.
        # Any positions in DB state are from a previous session and are no
        # longer real. Restoring them blocks the 5/5 position gate immediately.
        state.open_positions = {}
        state.active_orders = {}
        return state


class StateManager:
    def __init__(self):
        self.state = BotState()
        self._db_available = False
        self._engine = None
        self._Session = None
        # P14: In-memory fallbacks — survive DB outages and show data immediately
        self._closed_trades_mem: list = []   # max 500 closed trades this session
        self._risk_blocks_mem:   list = []   # max 200 risk block events this session
        # P15: Per-strategy and per-model win/loss tracking
        self._strategy_stats: dict = {}  # {strategy_name: {wins, losses, total_pnl, avg_conf}}
        self._ml_model_stats: dict = {   # ML model prediction accuracy tracking
            "xgboost":  {"correct": 0, "total": 0, "total_conf": 0.0},
            "lightgbm": {"correct": 0, "total": 0, "total_conf": 0.0},
            "ensemble": {"correct": 0, "total": 0, "total_conf": 0.0},
        }
        self._init_db()

    def _init_db(self):
        # P5-SQLITE: Use unified db.py — auto-selects SQLite or PostgreSQL
        try:
            from core.db import get_engine
            from sqlalchemy.orm import sessionmaker
            from sqlalchemy import text
            self._engine = get_engine()
            self._Session = sessionmaker(bind=self._engine)
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            self._db_available = True
            dialect = self._engine.dialect.name
            log.info(f"✅ Database connected ({dialect.upper()})")
            self._create_tables()
            self._restore_from_db()
        except Exception as e:
            log.warning(f"Database unavailable ({e}) — JSON fallback")
            self._db_available = False
            self._restore_from_json()

    def _create_tables(self):
        try:
            from database.models import Base
            Base.metadata.create_all(self._engine)
            log.info("✅ DB tables ready")
        except Exception as e:
            log.error(f"Table creation failed: {e}")

    def _restore_from_db(self):
        try:
            with self._Session() as session:
                from sqlalchemy import text
                result = session.execute(
                    text("SELECT state_json FROM bot_state ORDER BY saved_at DESC LIMIT 1")
                ).fetchone()
                if result:
                    raw = result[0]
                    data = raw if isinstance(raw, dict) else json.loads(raw)
                    self.state = BotState.from_dict(data)
                    log.info(
                        f"✅ State restored from DB | Capital: ₹{self.state.capital:,.0f} "
                        f"| PnL: ₹{self.state.total_pnl:+,.0f} "
                        f"| peak_capital reset → ₹{self.state.peak_capital:,.0f} (was in DB, now reset to initial)"
                    )
                else:
                    log.info("No previous state in DB — fresh start")
        except Exception as e:
            log.warning(f"DB state restore failed: {e}")

    def _restore_from_json(self):
        try:
            if _FALLBACK_FILE.exists():
                data = json.loads(_FALLBACK_FILE.read_text(encoding="utf-8"))
                self.state = BotState.from_dict(data)
                log.info(f"State from JSON | Capital: ₹{self.state.capital:,.0f}")
        except Exception as e:
            log.warning(f"JSON restore failed: {e}")

    async def save(self):
        try:
            state_dict = self.state.to_dict()
            self.state.last_saved = datetime.now()
            if self._db_available and self._Session:
                try:
                    with self._Session() as session:
                        from database.models import BotStateRecord
                        record = BotStateRecord(
                            state_json=state_dict,
                            capital=self.state.total_capital,
                            daily_pnl=self.state.daily_pnl,
                            mode=self.state.mode,
                        )
                        session.add(record)
                        session.commit()
                        return
                except Exception as e:
                    log.warning(f"DB save failed: {e}")
                    self._db_available = False
            _FALLBACK_FILE.write_text(json.dumps(state_dict, default=str, indent=2), encoding="utf-8")
        except Exception as e:
            log.error(f"State save failed: {e}")

    async def save_trade(self, trade_data: Dict):
        """Save every trade to DB and to in-memory cache for instant dashboard access."""
        # P14: Always cache closed trades in memory regardless of DB state
        if trade_data.get("status") == "CLOSED":
            entry = {
                **trade_data,
                "saved_at": datetime.now().isoformat(),
            }
            # Convert datetime objects to ISO strings for JSON serialisation
            for k, v in entry.items():
                if isinstance(v, datetime):
                    entry[k] = v.isoformat()
            self._closed_trades_mem.insert(0, entry)
            if len(self._closed_trades_mem) > 500:
                self._closed_trades_mem.pop()

            # P15: Update per-strategy win/loss stats
            strategy = trade_data.get("strategy", "Unknown") or "Unknown"
            net_pnl = trade_data.get("net_pnl", 0) or 0
            conf = float(trade_data.get("confidence", 0) or 0)
            if strategy not in self._strategy_stats:
                self._strategy_stats[strategy] = {
                    "wins": 0, "losses": 0, "total_pnl": 0.0,
                    "total_won": 0.0,   # Sum of P&L from winning trades only
                    "total_lost": 0.0,  # Sum of P&L from losing trades only (negative)
                    "capital_deployed": 0.0,  # Sum of (entry_price × qty) across all trades
                    "avg_conf": 0.0, "conf_samples": 0,
                    "best_pnl": 0.0, "worst_pnl": 0.0,
                }
            s = self._strategy_stats[strategy]
            if net_pnl > 0:
                s["wins"] += 1
                s["total_won"] = round(s.get("total_won", 0) + net_pnl, 2)
            else:
                s["losses"] += 1
                s["total_lost"] = round(s.get("total_lost", 0) + net_pnl, 2)
            # Capital deployed = entry_price × qty
            entry_price = float(trade_data.get("entry_price", 0) or 0)
            qty = int(trade_data.get("qty", 0) or 0)
            if entry_price > 0 and qty > 0:
                s["capital_deployed"] = round(s.get("capital_deployed", 0) + entry_price * qty, 2)
            s["total_pnl"] = round(s.get("total_pnl", 0) + net_pnl, 2)
            s["best_pnl"] = max(s.get("best_pnl", 0), net_pnl)
            s["worst_pnl"] = min(s.get("worst_pnl", 0), net_pnl)
            if conf > 0:
                n = s["conf_samples"]
                s["avg_conf"] = round((s["avg_conf"] * n + conf) / (n + 1), 2)
                s["conf_samples"] = n + 1

        if not self._db_available or not self._Session:
            return
        try:
            with self._Session() as session:
                from database.models import Trade
                valid_cols = {c.name for c in Trade.__table__.columns}
                clean = {k: v for k, v in trade_data.items() if k in valid_cols}
                # BUG-FIX P16: opened_at is stored as ISO string; DB DateTime columns
                # need datetime objects — coerce strings to datetime to prevent save failure
                for dt_col in ("entry_time", "exit_time", "trade_date"):
                    if dt_col in clean and isinstance(clean[dt_col], str):
                        try:
                            from datetime import datetime as _dt
                            clean[dt_col] = _dt.fromisoformat(clean[dt_col].replace("Z", ""))
                        except Exception:
                            clean.pop(dt_col, None)  # drop if unparseable (column has default)
                # Map gross_pnl → pnl if pnl not already set (DB column is "pnl" not "gross_pnl")
                if "pnl" not in clean and "pnl" in valid_cols:
                    gp = trade_data.get("gross_pnl")
                    if gp is not None:
                        clean["pnl"] = float(gp)
                # Map confidence → signal_conf for DB column name
                if "signal_conf" not in clean and "signal_conf" in valid_cols:
                    conf = trade_data.get("confidence")
                    if conf is not None:
                        clean["signal_conf"] = float(conf)
                trade = Trade(**clean)
                session.add(trade)
                session.commit()
                log.debug(f"DB trade saved: {trade_data.get('symbol')} {trade_data.get('side')}")
        except Exception as e:
            log.error(f"Trade DB save failed: {e}")

    async def save_signal(self, signal_data: Dict):
        """Save every signal (acted on or blocked) to DB."""
        if not self._db_available or not self._Session:
            return
        try:
            with self._Session() as session:
                from database.models import Signal
                session.add(Signal(
                    symbol=signal_data.get("symbol", ""),
                    signal_type=signal_data.get("side", ""),
                    confidence=float(signal_data.get("confidence", 0)),
                    strategy=signal_data.get("strategy", ""),
                    acted_on=bool(signal_data.get("acted_on", False)),
                    blocked_reason=signal_data.get("blocked_reason"),
                    features=signal_data.get("features"),
                ))
                session.commit()
        except Exception as e:
            log.debug(f"Signal save: {e}")

    async def save_position(self, pos_data: Dict, is_open: bool = True):
        """Upsert open/closed position to DB."""
        if not self._db_available or not self._Session:
            return
        try:
            with self._Session() as session:
                from database.models import Position
                pos = Position(
                    symbol=pos_data.get("symbol", ""),
                    side=pos_data.get("side", "LONG"),
                    qty=int(pos_data.get("qty", 0)),
                    avg_price=float(pos_data.get("avg_price", 0)),
                    current_price=float(pos_data.get("current_price", 0)),
                    unrealized_pnl=float(pos_data.get("unrealized_pnl", 0)),
                    stop_loss=pos_data.get("stop_loss"),
                    target=pos_data.get("target"),
                    strategy=pos_data.get("strategy", ""),
                    is_open=is_open,
                    mode=self.state.mode,
                )
                session.add(pos)
                session.commit()
        except Exception as e:
            log.debug(f"Position save: {e}")

    async def save_risk_event(self, event_type: str, reason: str, symbol: str = None, severity: str = "INFO"):
        """Save every risk block/breach to DB + in-memory cache for instant dashboard access."""
        # P14: Always record in memory regardless of DB state
        self._risk_blocks_mem.insert(0, {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "symbol": symbol or "—",
            "severity": severity,
            "reason": reason,
        })
        if len(self._risk_blocks_mem) > 200:
            self._risk_blocks_mem.pop()

        if not self._db_available or not self._Session:
            return
        try:
            with self._Session() as session:
                from database.models import RiskEvent
                session.add(RiskEvent(event_type=event_type, symbol=symbol, reason=reason, severity=severity))
                session.commit()
        except Exception as e:
            log.debug(f"Risk event save: {e}")

    async def save_ohlcv(self, symbol: str, df):
        """Save OHLCV candles to DB for historical reference."""
        if not self._db_available or not self._Session:
            return
        try:
            import pandas as pd
            with self._Session() as session:
                from database.models import OHLCVDaily
                # P5-SQLITE: Use session.merge() — works for both SQLite and PostgreSQL
                for ts, row in df.iterrows():
                    try:
                        rec = OHLCVDaily(
                            symbol=symbol,
                            date=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                            open=float(row.get("open", 0)),
                            high=float(row.get("high", 0)),
                            low=float(row.get("low", 0)),
                            close=float(row.get("close", 0)),
                            volume=int(row.get("volume", 0)),
                        )
                        session.merge(rec)
                    except Exception:
                        pass
                session.commit()
            log.debug(f"OHLCV saved: {symbol} {len(df)} rows")
        except Exception as e:
            log.debug(f"OHLCV save: {e}")

    async def save_model_run(self, model_name: str, symbol: str, metrics: Dict):
        """Save ML model training results to DB."""
        if not self._db_available or not self._Session:
            return
        try:
            with self._Session() as session:
                from database.models import ModelRun
                session.add(ModelRun(
                    model_name=model_name,
                    version=f"{symbol}_{datetime.now().strftime('%Y%m%d')}",
                    accuracy=metrics.get("accuracy"),
                    f1_score=metrics.get("f1"),
                    precision=metrics.get("precision"),
                    recall=metrics.get("recall"),
                    is_active=True,
                ))
                session.commit()
        except Exception as e:
            log.debug(f"Model run save: {e}")

    def get_closed_trades(self, symbol: str = None, strategy: str = None, limit: int = 100) -> List[Dict]:
        """Return closed trades filtered by symbol and/or strategy.
        Alias used by RiskEngine._win_rate() for dynamic Kelly sizing."""
        trades = self.get_trade_history(limit=limit, symbol=symbol)
        if strategy:
            trades = [t for t in trades if t.get("strategy") == strategy]
        # Only return trades that have a recorded PnL (i.e. closed)
        return [t for t in trades if t.get("net_pnl") is not None]

    def get_trade_history(self, limit: int = 100, symbol: str = None) -> List[Dict]:
        # BUG-FIX P16: Query ONLY CLOSED trades from DB — previously returned ALL trades
        # (OPEN + CLOSED) so the API filter for status==CLOSED sometimes found nothing.
        # Always merge DB closed records with _closed_trades_mem; mem wins for richer fields.
        if self._db_available and self._Session:
            try:
                with self._Session() as session:
                    from database.models import Trade
                    from sqlalchemy import desc
                    q = session.query(Trade).filter(Trade.status == "CLOSED").order_by(desc(Trade.entry_time))
                    if symbol:
                        q = q.filter(Trade.symbol == symbol)
                    rows = [{c.name: getattr(t, c.name) for c in Trade.__table__.columns} for t in q.limit(limit).all()]
                    if rows:
                        # Merge with memory cache: memory has richer fields (gross_pnl, confidence, stop_loss, target)
                        mem_map = {(t.get("symbol"), str(t.get("entry_time", ""))[:19]): t
                                   for t in self._closed_trades_mem}
                        merged = []
                        for r in rows:
                            key = (r.get("symbol"), str(r.get("entry_time", ""))[:19])
                            mem_entry = mem_map.get(key)
                            if mem_entry:
                                merged.append({**r, **mem_entry})  # mem fields take priority (richer)
                            else:
                                merged.append(r)
                        return merged
            except Exception as e:
                log.error(f"Trade history DB query: {e}")
        # Fallback: return in-memory closed trades (always populated with all fields)
        mem = self._closed_trades_mem
        if symbol:
            mem = [t for t in mem if t.get("symbol") == symbol]
        return mem[:limit]

    def add_risk_block(self, symbol: str, reason: str, severity: str = "WARN"):
        """P14: Record a risk block event in memory for dashboard display."""
        self._risk_blocks_mem.insert(0, {
            "timestamp": datetime.now().isoformat(),
            "event_type": "RISK_BLOCK",
            "symbol": symbol,
            "severity": severity,
            "reason": reason,
        })
        if len(self._risk_blocks_mem) > 200:
            self._risk_blocks_mem.pop()

    def get_db_stats(self) -> Dict:
        """Count rows in every table — for dashboard DB panel."""
        if not self._db_available or not self._Session:
            return {"connected": False}
        try:
            from sqlalchemy import text
            counts = {}
            tables = ["trades","signals","positions","ohlcv_daily","model_runs","risk_events","audit_log","bot_state"]
            with self._Session() as session:
                for t in tables:
                    try:
                        n = session.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                        counts[t] = n
                    except Exception:
                        counts[t] = 0
            return {"connected": True, **counts}
        except Exception as e:
            return {"connected": False, "error": str(e)}

    def reset_daily(self):
        self.state.daily_pnl = 0.0
        self.state.daily_trades = 0
        self.state.daily_wins = 0
        self.state.daily_losses = 0
        self.state.consecutive_losses = 0
        log.info("Daily state reset")

    def get_summary(self) -> Dict:
        s = self.state
        uptime_secs = 0
        if s.started_at:
            if isinstance(s.started_at, datetime):
                uptime_secs = int((datetime.now() - s.started_at).total_seconds())

        # Capital always from config — never allow 0
        capital = s.total_capital
        if capital <= 0:
            capital = cfg.initial_capital

        available = s.available_margin
        if available <= 0:
            available = capital

        last_saved_str = None
        if s.last_saved:
            last_saved_str = s.last_saved.isoformat() if isinstance(s.last_saved, datetime) else str(s.last_saved)

        return {
            "mode": s.mode,
            "status": s.status,
            "capital": round(capital, 2),
            "total_capital": round(capital, 2),
            "initial_capital": round(cfg.initial_capital, 2),
            "available_margin": round(available, 2),
            "daily_pnl": round(s.daily_pnl, 2),
            "daily_pnl_pct": round((s.daily_pnl / capital * 100) if capital else 0, 2),
            "total_pnl": round(s.total_pnl, 2),
            "win_rate": round(s.win_rate, 2),
            "open_positions": len(s.open_positions),
            "open_positions_detail": [{**v, "symbol": k} for k, v in s.open_positions.items()],
            "daily_trades": s.daily_trades,
            "daily_wins": s.daily_wins,
            "daily_losses": s.daily_losses,
            "drawdown_pct": round(s.drawdown_pct, 2),
            "daily_loss_used_pct": round(s.daily_loss_used_pct, 2),
            "consecutive_losses": s.consecutive_losses,
            "all_time_high": round(s.all_time_high, 2),
            "peak_capital": round(s.peak_capital, 2),
            "is_halted": s.status == "HALTED",
            "db_connected": self._db_available,
            "last_saved": last_saved_str,
            "uptime": uptime_secs,
        }


# Global singleton
state_mgr = StateManager()
