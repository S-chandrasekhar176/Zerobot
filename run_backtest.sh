#!/bin/bash
# ZeroBot — Linux backtest launcher
cd "$(dirname "$0")"
export TZ=Asia/Kolkata
export PYTHONUNBUFFERED=1
if [ -d "venv" ]; then source venv/bin/activate; fi
if [ -f "config/.env" ]; then
    set -a; source config/.env; set +a
fi
python3 run_backtest.py "$@"
