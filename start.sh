#!/bin/bash
# ═══════════════════════════════════════════════════════
#  ZeroBot — Linux/macOS launcher
#  Usage: ./start.sh
#  Cloud: managed by systemd (see zerobot.service)
# ═══════════════════════════════════════════════════════
set -e
cd "$(dirname "$0")"

# Colour helpers
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

echo -e "${GREEN}═══════════════════════════════════${NC}"
echo -e "${GREEN}  ZeroBot NSE — Linux Launcher      ${NC}"
echo -e "${GREEN}═══════════════════════════════════${NC}"

# Force IST timezone
export TZ=Asia/Kolkata
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8

# Activate virtualenv if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
    echo -e "${GREEN}[OK] venv activated${NC}"
elif [ -d ".venv" ]; then
    source .venv/bin/activate
    echo -e "${GREEN}[OK] .venv activated${NC}"
else
    echo -e "${YELLOW}[WARN] No venv found — using system Python${NC}"
fi

# Check Python version
PY_VER=$(python3 --version 2>&1)
echo -e "${GREEN}[OK] $PY_VER${NC}"

# Load .env
if [ -f "config/.env" ]; then
    set -a
    source config/.env
    set +a
    echo -e "${GREEN}[OK] config/.env loaded${NC}"
else
    echo -e "${RED}[ERR] config/.env not found — copy config/.env.example to config/.env and fill credentials${NC}"
    exit 1
fi

echo -e "${GREEN}[OK] Timezone: $(date '+%Z %z')${NC}"
echo ""

exec python3 main.py
