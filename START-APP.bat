@echo off
setlocal enabledelayedexpansion
title Rebalancer - App chal rahi hai (ye window band mat karo)
cd /d "%~dp0"
color 0B
echo.
echo   ================================================================
echo      WEEKLY REBALANCER
echo   ================================================================
echo.

REM ---------- 1. Python ----------
python --version >nul 2>&1
if errorlevel 1 (
    color 0C
    echo   ####  PYTHON NAHI MILA  ####
    echo.
    echo   python.org/downloads se install karo.
    echo   Install karte waqt "Add python.exe to PATH" TICK karna.
    echo.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do echo   Python %%v

REM ---------- 2. dependencies ----------
set MISSING=
python -c "import fastapi"        >nul 2>&1 || set MISSING=!MISSING! fastapi
python -c "import uvicorn"        >nul 2>&1 || set MISSING=!MISSING! uvicorn
python -c "import multipart"      >nul 2>&1 || set MISSING=!MISSING! python-multipart
python -c "import yaml"           >nul 2>&1 || set MISSING=!MISSING! PyYAML
python -c "import requests"       >nul 2>&1 || set MISSING=!MISSING! requests
python -c "import tzdata"         >nul 2>&1 || set MISSING=!MISSING! tzdata

if not "!MISSING!"=="" (
    echo   Ye install karne hain:!MISSING!
    echo   Thoda time lagega, ek baar ka kaam hai...
    echo.
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        color 0C
        echo.
        echo   ####  INSTALL FAIL HO GAYA  ####
        echo   Upar ka laal text mujhe bhej do -- wahi asli wajah hai.
        echo.
        pause
        exit /b 1
    )
    echo.
)

REM ---------- 3. app khud theek hai? ----------
python -c "import web.api" 2>app-error.txt
if errorlevel 1 (
    color 0C
    echo   ####  APP LOAD NAHI HUI  ####
    echo.
    type app-error.txt
    echo.
    echo   Ye error app-error.txt mein bhi save ho gaya hai.
    echo   Wo file mujhe bhej dena.
    echo.
    pause
    exit /b 1
)
del app-error.txt >nul 2>&1

REM ---------- 4. credentials ----------
if exist "creds.bat" (
    call "creds.bat"
    echo   Dhan credentials mile - Live mode available hai.
) else (
    echo   creds.bat nahi hai - app DEMO mode mein khulegi.
)
echo.
echo   Server : http://127.0.0.1:8770
echo   Browser 3 second mein khul jaayega.
echo.
echo   ----------------------------------------------------------------
echo     Band karni ho toh YE WINDOW band kar do (ya Ctrl+C)
echo   ----------------------------------------------------------------
echo.

start "" /b cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8770"
python -m uvicorn web.api:app --host 127.0.0.1 --port 8770

echo.
echo   Server band ho gaya.
pause
