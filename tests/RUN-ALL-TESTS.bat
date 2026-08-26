@echo off
title Rebalancer - Saare tests
cd /d "%~dp0\.."
color 0B
echo.
echo   ================================================================
echo      SAARE TESTS
echo      Thoda time lagega (stress tests dheeme hain)
echo   ================================================================
echo.
echo   [1/7] Unit tests...
python -m pytest tests -q
echo.
echo   [2/7] Planner fuzz (12,000 scenarios)...
python tests\stress_fuzz.py
echo.
echo   [3/7] Hafte-dar-hafte (52 week x 60 combos)...
python tests\stress_weeks.py
echo.
echo   [4/7] Mushkil situations (T+1, split, circuit)...
python tests\stress_hard.py
echo.
echo   [5/7] Behavioural (idempotence, 200 week)...
python tests\stress_deep.py
echo.
echo   [6/7] Executor + DB + CSV + config...
python tests\stress_exec.py
echo.
echo   [7/7] Deploy budget (2.6 lakh checks)...
python tests\stress_deploy.py
echo.
echo   ================================================================
echo      Ho gaya. Upar koi FAIL dikhe toh mujhe bhej do.
echo   ================================================================
pause
