# ZeroBot — Production Fixes Applied
## Version: G2-PROD-1 | Date: 2025

### Critical fixes (cloud deployment blockers)

**FIX-P1: Removed runtime pip install**
- File: `main.py`
- Problem: Bot auto-ran `pip install NorenRestApiPy` mid-session on production server.
  This is dangerous — it can fail silently, cause partial installs, or race with running code.
- Fix: Removed auto-install. Now logs a clear error and exits checklist step.
  NorenRestApiPy is listed in requirements.txt — install it once with pip install -r requirements.txt.

**FIX-P2: Dashboard HTTP Basic Auth**
- File: `dashboard/api/main.py`
- Problem: Dashboard (port 8000) was exposed with no authentication.
  Anyone on the internet could see your positions, P&L, halt/resume the bot.
- Fix: Added HTTPBasic auth. Set DASHBOARD_PASS in config/.env to activate.
  If DASHBOARD_PASS is empty, auth is disabled (safe for localhost/VPN access).

**FIX-P3: Log directories auto-created**
- File: `core/logger.py`
- Problem: loguru crashed on first run if logs/trades/, logs/errors/, logs/signals/ didn't exist.
- Fix: Directories are now created automatically before logger is attached.

**FIX-P4: .env typo corrected**
- File: `config/.env`
- Problem: `ZEROBOTUSEFEATURE=0` (typo) → was never read correctly by the app.
- Fix: Corrected to `ZEROBOT_USE_FINBERT=0`.

**FIX-P5: Added Linux launcher (start.sh)**
- File: `start.sh` (new)
- Problem: Only start.bat existed (Windows only). Cloud servers run Linux.
- Fix: Created start.sh with venv activation, IST timezone, .env loading.

**FIX-P6: Added systemd service file**
- File: `deploy/zerobot.service` (new)
- Problem: No auto-start/restart on cloud server crash or reboot.
- Fix: systemd service with Restart=on-failure, MemoryMax=3G, IST timezone.
  Install: sudo cp deploy/zerobot.service /etc/systemd/system/ && sudo systemctl enable zerobot

**FIX-P7: Added nginx reverse proxy config**
- File: `deploy/nginx.conf` (new)
- Problem: No secure way to expose dashboard from cloud server.
- Fix: nginx config with WebSocket proxy_pass, SSL-ready. Dashboard stays on 127.0.0.1.

**FIX-P8: Removed stale database**
- File: `data/zerobot.db` (removed)
- Problem: Old DB had stale position state from previous sessions.
  Deploying with old DB can cause position count mismatches.
- Fix: Removed. Fresh DB created on first run automatically.

**FIX-P9: Added healthcheck.py**
- File: `healthcheck.py` (new)
- Use: `python3 healthcheck.py` before first run
- Checks: Python version, .env placeholders, all imports, connectivity, timezone, writable dirs.

**FIX-P10: Added DEPLOY_CLOUD.md**
- File: `DEPLOY_CLOUD.md` (new)
- Complete step-by-step guide for Hetzner Mumbai + AWS ap-south-1 deployment.

**FIX-P11: Added run_backtest.sh**  
- File: `run_backtest.sh` (new)
- Linux equivalent of run_backtest.bat.

**FIX-P12: requirements.txt — tzdata added**
- File: `requirements.txt`
- Problem: Cloud Linux servers may not have timezone data for zoneinfo.
- Fix: Added tzdata package.

### No regressions
All original features preserved. Zero functional changes to:
- Risk engine (13 gates)
- Strategy logic
- Broker connections
- ML ensemble
- Telegram commands
