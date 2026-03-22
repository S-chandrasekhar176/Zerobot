# -*- coding: utf-8 -*-
"""
ZeroBot G2 — Broker Factory
6 clean modes, no silent fallbacks except P-mode.

MODE MAP:
  p_mode      : Yahoo data  + Paper execution
  s_paper     : Shoonya WS  + Paper execution    (paper money, real data)
  a_paper     : Angel One WS + Paper execution   (paper money, real data)
  dual        : Angel One WS + Shoonya execution (REAL money)
  a_live      : Angel One WS + Angel One exec    (REAL money)
  s_live      : Shoonya WS   + Shoonya execution (REAL money)
"""
from core.config import cfg
from core.logger import log


def get_broker(force=None):
    """
    Get broker instance.
    
    Args:
        force: Optional broker name to override config (for testing)
        
    Returns:
        Configured broker instance
    """
    mode = (force or cfg.broker_name).lower().strip()
    log.info(f"[BROKER] Initialising mode: {mode.upper()}")

    # ── P-MODE: Yahoo data + Paper execution ──────────────────────────────
    if mode in ("paper", "p_mode", "p-mode"):
        from broker.paper_broker import PaperBroker
        return PaperBroker(initial_capital=cfg.initial_capital)

    # ── S-PAPER: Shoonya WS data + Paper execution ────────────────────────
    elif mode in ("s_paper", "s-paper", "shoonya_paper"):
        from broker.shoonya_paper_broker import ShoonyaPaperBroker
        b = ShoonyaPaperBroker()
        b.connect_or_raise()     # raises RuntimeError if fails — no silent fallback
        return b

    # ── A-PAPER: Angel One WS data + Paper execution ──────────────────────
    elif mode in ("a_paper", "a-paper", "hybrid", "angel_paper"):
        from broker.angel_paper_broker import AngelPaperBroker
        b = AngelPaperBroker()
        b.connect_or_raise()
        return b

    # ── A-LIVE: Angel One data + Angel One execution (REAL MONEY) ─────────
    elif mode in ("a_live", "a-live", "angel_live", "angel"):
        from broker.angel_one import AngelOneBroker
        b = AngelOneBroker()
        b.connect_or_raise()
        return b

    # ── S-LIVE: Shoonya WS data + Shoonya execution (REAL MONEY) ──────────
    elif mode in ("s_live", "s-live", "shoonya_live", "shoonya"):
        from broker.shoonya_live_broker import ShoonyaLiveBroker
        b = ShoonyaLiveBroker()
        b.connect_or_raise()
        return b

    # ── DUAL: Angel One data + Shoonya execution (REAL MONEY) ─────────────
    elif mode in ("dual", "dual_mode", "dual-mode"):
        from broker.dual_broker import DualBroker
        b = DualBroker()
        b.connect_or_raise()
        return b

    else:
        raise ValueError(
            f"[BROKER] Unknown mode: '{mode}'\n"
            f"  Valid values: p_mode | s_paper | a_paper | dual | a_live | s_live"
        )
