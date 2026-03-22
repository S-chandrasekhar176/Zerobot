# -*- coding: utf-8 -*-
"""
ZeroBot v1.1 — Unified Database Layer (Patch 6)
────────────────────────────────────────────────
AUTO-SELECTS engine based on .env:
  • USE_SQLITE=true  → SQLite (data/zerobot.db) — zero install
  • USE_SQLITE=false → PostgreSQL using DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD
  • DB_URL=...       → Direct URL override (any SQLAlchemy-compatible DB)

Your current setup (PostgreSQL/PgAdmin4) works as-is.
To switch to SQLite: set USE_SQLITE=true in config/.env
"""
import os
from pathlib import Path
from core.logger import log

# ── Detect which DB to use ────────────────────────────────────────────────────
_USE_SQLITE_ENV = os.getenv("USE_SQLITE", "true").strip().lower()
_DB_URL_OVERRIDE = os.getenv("DB_URL", "").strip()

# Priority: DB_URL override → USE_SQLITE flag → default SQLite
if _DB_URL_OVERRIDE:
    # Direct URL provided — use it as-is
    DB_URL     = _DB_URL_OVERRIDE
    _USE_SQLITE = DB_URL.startswith("sqlite")
    log.info(f"🗄️  Database: URL override → {DB_URL.split('@')[-1]}")
elif _USE_SQLITE_ENV == "false":
    # PostgreSQL mode — build URL from individual env vars
    _pg_host = os.getenv("DB_HOST", "localhost")
    _pg_port = os.getenv("DB_PORT", "5432")
    _pg_name = os.getenv("DB_NAME", "zerobot")
    _pg_user = os.getenv("DB_USER", "zerobot_user")
    _pg_pass = os.getenv("DB_PASSWORD", "")
    DB_URL     = f"postgresql://{_pg_user}:{_pg_pass}@{_pg_host}:{_pg_port}/{_pg_name}"
    _USE_SQLITE = False
    log.info(f"🐘 Database: PostgreSQL → {_pg_host}:{_pg_port}/{_pg_name} (user={_pg_user})")
else:
    # Default: SQLite — works without any setup
    _DB_FILE   = Path(__file__).parent.parent / "data" / "zerobot.db"
    _DB_FILE.parent.mkdir(exist_ok=True)
    DB_URL     = f"sqlite:///{_DB_FILE}"
    _USE_SQLITE = True
    log.info(f"📦 Database: SQLite → {_DB_FILE}")


def get_engine():
    """Return a SQLAlchemy engine for the configured database."""
    from sqlalchemy import create_engine, event
    kwargs = {}
    if _USE_SQLITE:
        # SQLite needs check_same_thread=False for async use
        kwargs["connect_args"] = {"check_same_thread": False}
        engine = create_engine(DB_URL, **kwargs)
        # Enable WAL mode for concurrent reads (important for dashboard)
        @event.listens_for(engine, "connect")
        def set_wal(conn, _):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=10000")
    else:
        engine = create_engine(
            DB_URL,
            pool_size=5, max_overflow=10, pool_pre_ping=True
        )
    return engine


def get_upsert(engine, table, rows: list, conflict_col: str = "id"):
    """
    Portable UPSERT — works for both SQLite and PostgreSQL.
    SQLite uses INSERT OR REPLACE; PostgreSQL uses ON CONFLICT DO UPDATE.
    """
    from sqlalchemy import text
    dialect = engine.dialect.name
    if not rows:
        return
    cols = list(rows[0].keys())
    if dialect == "sqlite":
        placeholders = ", ".join(f":{c}" for c in cols)
        sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    else:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        return _pg_upsert(engine, table, rows, conflict_col)
    with engine.connect() as conn:
        for row in rows:
            conn.execute(text(sql), row)
        conn.commit()


def _pg_upsert(engine, table, rows, conflict_col):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy import Table, MetaData
    meta = MetaData()
    meta.reflect(bind=engine, only=[table])
    tbl = meta.tables[table]
    with engine.connect() as conn:
        for row in rows:
            stmt = pg_insert(tbl).values(**row)
            cols = {k: stmt.excluded[k] for k in row if k != conflict_col}
            stmt = stmt.on_conflict_do_update(index_elements=[conflict_col], set_=cols)
            conn.execute(stmt)
        conn.commit()
