# ZeroBot G2 — All Fixes Applied
Last updated: 2026-03-15

## Errors Fixed in This Build

### ERROR 1 — SmartAPI not installed (Angel One hybrid/live mode broken)
**Symptom:** `SmartAPI not installed. Run: pip install smartapi-python pyotp`
**Root cause:** `requirements.txt` had `smartapi-python` commented out
**Fix:** 
- `requirements.txt` → `smartapi-python==1.3.4` and `pycryptodome` now uncommented
- `start.bat` → explicitly runs `pip install smartapi-python pycryptodome pyotp` as a separate step
- Also uninstalls the wrong package (`SmartApi`) if accidentally installed

### ERROR 2 — NumPy models deleted on every startup  
**Symptom:** `⚠️ Deleted 23 NumPy 1.x incompatible model(s)` on every restart → 2-minute retrain loop
**Root cause:** `main.py` deleted ALL `.pkl` files whenever `numpy >= 2`, even freshly-trained ones
**Fix:** `main.py` → now attempts to actually load each model; only deletes if it raises a version error

### BUG-1 — get_funds() Unicode typo → funds always ₹0
`broker/angel_one.py` line 170: `"totalpayín"` had accented `í` → fixed

### BUG-2 — square_off_all() crashes with RuntimeError  
`broker/angel_one.py`: sync method calling `asyncio.create_task()` → converted to async

### BUG-3 — _get_symbol_token() returns "0" for 90% of symbols
`broker/angel_one.py`: only 17 hardcoded tokens → now uses `searchScrip()` dynamic lookup

### BUG-4 — is_configured missing totp_secret check
`core/config.py`: TOTP secret now required → added `missing_fields` property

### BUG-5 — modifyOrder sends quantity=0
`broker/angel_one.py`: `str(new_qty or 0)` → guarded, only sends qty if > 0

### BUG-6 — 500ms sleep per live order + false fill events
`broker/angel_one.py`: removed blocking sleep, fill event only fires on COMPLETE status

### BUG-7 — ShadowBroker async/sync mismatch
`broker/angel_one.py`: `place_order` made async-compatible across both broker classes

### BUG-8 — Empty token in ShadowBroker LTP
`broker/angel_one.py`: uses proper `get_ltp()` instead of passing `""` as token

### BUG-9 — Double bus listener registration
`dashboard/api/main.py`: removed duplicate import-time call, added idempotency guard

### BUG-10 — cfg.__dict__[] bypasses Pydantic validation
`core/config.py` + `dashboard/api/main.py`: added `cfg.set()` helper, all mutations use it

### BUG-11 — O(n²) win_rate scan per WebSocket poll
`dashboard/api/main.py`: added `_win_rate_cache` + dirty flag → O(1) per position

### DASH-1 — JS SyntaxError: semicolon inside template literal
`dashboard/frontend/index.html`: ROI td had `;font-family` inside `${}` → moved outside

### DASH-2/3 — ReferenceError: setText and fmtINR undefined
`dashboard/frontend/index.html`: `loadPortfolio()` used undefined functions → fixed to `_setEl()` + `fmtRS()`

### DASH-4 — TypeError: $('restatus') null crash
`dashboard/frontend/index.html`: `id="restatus"` doesn't exist → wrapped in null guard

## Quick Start
1. Fill in `config/.env` with your credentials
2. Double-click `start.bat`
3. Open `http://127.0.0.1:8000` in your browser
