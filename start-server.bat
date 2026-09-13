@echo off
chcp 65001 >nul
title WorkBuddy2API - Local API Service
cd /d "%~dp0"
echo Starting WorkBuddy2API on http://127.0.0.1:8787 ...
echo Web panel: http://127.0.0.1:8787/
echo.
".venv\Scripts\python.exe" converter.py --desensitize --log converter.log --port 8787
echo.
echo Service stopped. Press any key to exit.
pause >nul
