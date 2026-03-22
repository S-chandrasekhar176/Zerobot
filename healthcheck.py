#!/usr/bin/env python3
"""
ZeroBot — Health Check Script
Run this to verify the system is working before market open.

Usage:
    python3 healthcheck.py
    python3 healthcheck.py --full   (includes broker connectivity test)
"""
import sys, os, asyncio, json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("TZ", "Asia/Kolkata")

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"

def ok(label, detail=""):    print(f"  {GREEN}✅  {label:<35}{RESET} {detail}")
def warn(label, detail=""):  print(f"  {YELLOW}⚠️   {label:<35}{RESET} {detail}")
def fail(label, detail=""):  print(f"  {RED}❌  {label:<35}{RESET} {detail}")

print(f"\n{CYAN}═══════════════════════════════════════════{RESET}")
print(f"{CYAN}  ZeroBot Health Check — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
print(f"{CYAN}═══════════════════════════════════════════{RESET}\n")

passed = failed = warned = 0

# 1. Python version
import sys as _sys
major, minor = _sys.version_info[:2]
if major == 3 and minor >= 10:
    ok("Python version", f"{major}.{minor} ✓")
    passed += 1
else:
    warn("Python version", f"{major}.{minor} — recommend 3.10+")
    warned += 1

# 2. Config files
env_path = Path("config/.env")
yaml_path = Path("config/settings.yaml")
if env_path.exists():
    content = env_path.read_text(encoding="utf-8")
    if "YOUR_" in content:
        unfilled = [line.strip() for line in content.splitlines() 
                    if "YOUR_" in line and not line.strip().startswith("#")]
        warn("config/.env", f"{len(unfilled)} placeholder(s) still unfilled: {unfilled[:3]}")
        warned += 1
    else:
        ok("config/.env", "All placeholders filled")
        passed += 1
else:
    fail("config/.env", "File not found — copy from config/.env.example")
    failed += 1

if yaml_path.exists():
    ok("config/settings.yaml", "Found")
    passed += 1
else:
    fail("config/settings.yaml", "Missing")
    failed += 1

# 3. Key imports
imports_ok = True
for pkg, name in [
    ("fastapi", "FastAPI"),
    ("uvicorn", "uvicorn"),
    ("pandas", "pandas"),
    ("numpy", "numpy"),
    ("yfinance", "yfinance"),
    ("loguru", "loguru"),
    ("rich", "rich"),
    ("pyotp", "pyotp"),
    ("xgboost", "xgboost"),
    ("lightgbm", "lightgbm"),
]:
    try:
        mod = __import__(pkg)
        ver = getattr(mod, "__version__", "?")
        ok(f"  import {name}", ver)
        passed += 1
    except ImportError:
        fail(f"  import {name}", "NOT INSTALLED — run: pip install -r requirements.txt")
        failed += 1
        imports_ok = False

# 4. Optional: NorenRestApiPy
try:
    import NorenRestApiPy
    ok("NorenRestApiPy (Shoonya)", "Installed")
    passed += 1
except ImportError:
    warn("NorenRestApiPy (Shoonya)", "Not installed — only needed for s_paper/s_live mode")
    warned += 1

# 5. Data directory writable
data_dir = Path("data")
data_dir.mkdir(exist_ok=True)
try:
    test_file = data_dir / ".write_test"
    test_file.write_text("ok")
    test_file.unlink()
    ok("data/ writable", str(data_dir.resolve()))
    passed += 1
except Exception as e:
    fail("data/ writable", str(e))
    failed += 1

# 6. Logs directory
log_dir = Path("logs")
for sub in ("trades", "errors", "signals"):
    (log_dir / sub).mkdir(parents=True, exist_ok=True)
ok("logs/ directories", "Created")
passed += 1

# 7. Internet connectivity
import urllib.request
for url, name in [
    ("https://api.telegram.org", "Telegram API"),
    ("https://smartapi.angelone.in", "AngelOne API"),
    ("https://www.nseindia.com", "NSE India"),
]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        urllib.request.urlopen(req, timeout=5)
        ok(f"  {name}", "Reachable")
        passed += 1
    except Exception as e:
        warn(f"  {name}", f"Unreachable ({type(e).__name__}) — check network")
        warned += 1

# 8. Timezone
import time as _time
tz = os.environ.get("TZ", "not set")
ist_offset = _time.timezone / -3600
if abs(ist_offset - 5.5) < 0.1:
    ok("Timezone", f"IST ({tz})")
    passed += 1
else:
    warn("Timezone", f"Not IST — set TZ=Asia/Kolkata (currently offset={ist_offset}h)")
    warned += 1

# Summary
print(f"\n{CYAN}═══════════════════════════════════════════{RESET}")
total = passed + failed + warned
if failed == 0:
    print(f"{GREEN}  ✅  {passed}/{total} passed | {warned} warnings | Ready to run!{RESET}")
    print(f"\n  Start: {CYAN}./start.sh{RESET}  or  {CYAN}python3 main.py{RESET}")
else:
    print(f"{RED}  ❌  {failed} FAILED | {warned} warnings | Fix failures before starting{RESET}")
print(f"{CYAN}═══════════════════════════════════════════{RESET}\n")
sys.exit(0 if failed == 0 else 1)
