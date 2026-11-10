@echo off
chcp 65001 >nul
title Kleinanzeigen strict bot
cd /d "%~dp0"
rem "python" may be the empty Microsoft Store alias - prefer the "py" launcher
set PY=python
where py >nul 2>nul && set PY=py
%PY% --version
if errorlevel 1 (
  echo Python not found. Install it from python.org - Windows installer 64-bit.
  pause
  exit /b 1
)
:loop
echo [%date% %time%] Starting bot...
%PY% strict_bot.py
echo [%date% %time%] Bot stopped, restart in 30 seconds. Close this window to stop.
timeout /t 30 /nobreak >nul
goto loop
