@echo off
setlocal enabledelayedexpansion
title Rebalancer - Diagnostic (ye app NAHI kholta)
cd /d "%~dp0"
color 0B
echo.
echo   ================================================================
echo      DIAGNOSTIC
echo      Ye sirf CHECK karta hai. App kholne ke liye START-APP.bat
echo   ================================================================
echo.

set BAD=0

python --version 2>nul || (echo   [X ] Python NAHI mila & set BAD=1)
echo.
echo   --- Libraries ---
for %%m in (fastapi uvicorn multipart yaml requests tzdata) do (
    python -c "import %%m" >nul 2>&1 && (echo   [OK] %%m) || (echo   [X ] %%m  -- MISSING & set BAD=1)
)
echo.
echo   --- Files ---
for %%f in (config.yaml watchlist.csv requirements.txt web\api.py web\routes.py web\creds.py web\paper.py web\static\index.html web\static\app.js web\static\style.css rebalancer\planner.py rebalancer\dhan.py rebalancer\models.py rebalancer\config.py rebalancer\store.py rebalancer\executor.py rebalancer\watchlist.py rebalancer\instruments.py rebalancer\report.py rebalancer\tz.py) do (
    if exist "%%f" (echo   [OK] %%f) else (echo   [X ] %%f  -- MISSING & set BAD=1)
)
if exist "creds.bat" (echo   [OK] creds.bat) else (echo   [--] creds.bat  -- nahi hai, Demo mode chalega)
echo.
echo   --- App load test ---
python -c "import web.api; print('   [OK] app load ho gayi')" 2>diagnostic.txt || (
    echo   [X ] app load NAHI hui. Error:
    echo.
    type diagnostic.txt
    echo.
    echo   Ye diagnostic.txt mein save hai -- mujhe bhej do.
    set BAD=1
)
echo.
echo   --- Timezone ---
python -c "from rebalancer.tz import IST; import datetime; print('   [OK] IST =', datetime.datetime.now(IST).strftime('%%H:%%M'))" 2>nul || (echo   [X ] timezone & set BAD=1)
echo.
echo   ================================================================
if "!BAD!"=="1" (
    color 0C
    echo      KUCH GADBAD HAI -- upar jo [X ] hai wahi problem hai.
    echo   ================================================================
    echo.
    pause
    exit /b 1
)
color 0A
echo      SAB THEEK HAI
echo   ================================================================
echo.
echo   App abhi kholni hai? (Y dabao, ya kuch aur dabao chhodne ke liye)
choice /c YN /n /m "   [Y/N]: " >nul 2>&1
if errorlevel 2 goto :fin
if errorlevel 1 (
    echo.
    echo   START-APP.bat chala rahe hain...
    echo.
    call "START-APP.bat"
    exit /b 0
)
:fin
echo.
echo   Theek hai. App kholne ke liye START-APP.bat double-click karo.
echo.
pause
