@echo off
title Rebalancer - Setup (ek baar ka kaam)
cd /d "%~dp0"
color 0B
echo.
echo   ================================================================
echo      REBALANCER  --  SETUP
echo      Ye sirf EK BAAR chalana hai. 2 minute lagenge.
echo   ================================================================
echo.

echo   [1/3] Python check kar rahe hain...
python --version >nul 2>&1
if errorlevel 1 (
    color 0C
    echo.
    echo   ####  PYTHON INSTALL NAHI HAI  ####
    echo.
    echo   1. Ye link kholo:  https://www.python.org/downloads/
    echo   2. Bada peela button dabao - "Download Python"
    echo   3. Installer chalao
    echo   4. **BAHUT ZAROORI** - pehli screen par neeche
    echo      "Add python.exe to PATH" ka box TICK karo
    echo   5. Install karo, phir ye file dobara double-click karo
    echo.
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do echo         Python %%v mil gaya. Theek hai.
echo.

echo   [2/3] Libraries install kar rahe hain (thoda time lagega)...
python -m pip install --upgrade pip --quiet >nul 2>&1
python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    color 0C
    echo.
    echo   ####  INSTALL FAIL HO GAYA  ####
    echo   Internet chal raha hai? Dobara try karo.
    echo.
    pause
    exit /b 1
)
echo         Ho gaya.
echo.

echo   [3/3] Dhan ke credentials...
if exist "creds.bat" (
    echo         creds.bat pehle se hai. Badalna hai toh use notepad se kholo.
) else (
    copy /y "creds.bat.example" "creds.bat" >nul
    echo.
    echo   ----------------------------------------------------------------
    echo     Ab NOTEPAD khulega. Usme do line hain:
    echo.
    echo        set DHAN_CLIENT_ID=your_client_id_here
    echo        set DHAN_ACCESS_TOKEN=your_access_token_here
    echo.
    echo     "your_client_id_here" ki jagah apna client id likho
    echo     "your_access_token_here" ki jagah apna token likho
    echo.
    echo     Kahan se milega:  dhan.co par login -^> Profile
    echo                       -^> DhanHQ Trading APIs -^> Access Token
    echo.
    echo     Likhne ke baad  Ctrl+S  dabao, phir notepad band kar do.
    echo   ----------------------------------------------------------------
    echo.
    pause
    notepad "creds.bat"
)
echo.

echo   Connection test kar rahe hain...
echo.
call "creds.bat"
python -m rebalancer.cli holdings
if errorlevel 1 (
    color 0E
    echo.
    echo   ####  CONNECTION NAHI BANA  ####
    echo   Upar wala error padho. Aksar wajah:
    echo     - creds.bat mein token galat ya purana hai
    echo     - token expire ho gaya (Dhan se naya lo)
    echo.
    pause
    exit /b 1
)
color 0A
echo.
echo   ================================================================
echo      SETUP POORA HO GAYA
echo.
echo      Ab bas ye karna hai:
echo        watchlist.csv mein apni nayi list daalo
echo        2-PLAN.bat  double-click karo
echo   ================================================================
echo.
pause
