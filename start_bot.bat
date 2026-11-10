@echo off
chcp 65001 >nul
title Kleinanzeigen bot
cd /d "%~dp0"
:loop
echo [%date% %time%] Starting bot...
python kleinanzeigen_bot.py
echo [%date% %time%] Bot stopped, restart in 30 seconds. Close this window to stop.
timeout /t 30 /nobreak >nul
goto loop
