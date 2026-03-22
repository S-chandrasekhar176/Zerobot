@echo off
:: ZeroBot G2 — Windows Launcher
title ZeroBot G2 — NSE India Algo Trader
chcp 65001 >nul 2>&1

echo.
echo  +===========================================================+
echo  ^|         ZeroBot G2 — NSE India Algo Trader               ^|
echo  ^|  A-Paper mode (default): Angel One data + Paper money     ^|
echo  +===========================================================+
echo.

:: ── Kill any running ZeroBot instances ──────────────────────────
echo [0/5] Stopping any existing ZeroBot instances...
taskkill /F /FI "WINDOWTITLE eq ZeroBot*" /T >nul 2>&1
:: Kill any python process holding port 8000
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr :8000') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 /nobreak >nul 2>&1
echo   Done.

python --version 2>nul
if errorlevel 1 (echo ERROR: Python not found in PATH & pause & exit /b 1)

:: ── Install dependencies ─────────────────────────────────────────
echo [1/5] Installing core requirements...
pip install -r requirements.txt -q --exists-action i

echo [2/5] Installing Angel One SmartAPI...
pip uninstall SmartApi -y -q 2>nul
pip install smartapi-python==1.3.4 pycryptodome pyotp -q --exists-action i

echo [3/5] Installing Shoonya API...
pip install NorenRestApiPy -q --exists-action i

:: ── Create folders ───────────────────────────────────────────────
echo [4/5] Setting up folders...
if not exist "data" mkdir data
if not exist "models\saved" mkdir models\saved
if not exist "logs\errors" mkdir logs\errors
if not exist "logs\signals" mkdir logs\signals
if not exist "logs\trades" mkdir logs\trades

:: ── Clear Python cache ───────────────────────────────────────────
for /d /r . %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d" 2>nul

:: ── Check config ─────────────────────────────────────────────────
if not exist "config\.env" (
    if exist "config\.env.example" (
        copy "config\.env.example" "config\.env" >nul
        echo   Created config/.env — fill in your credentials
    )
)

echo [5/5] Starting ZeroBot G2...
echo.
echo  +-----------------------------------------------------------+
echo  ^|  Dashboard : http://127.0.0.1:8000                       ^|
echo  ^|  Stop      : Ctrl+C then 'taskkill /F /IM python.exe'    ^|
echo  +-----------------------------------------------------------+
echo.
python -X utf8 main.py
pause
