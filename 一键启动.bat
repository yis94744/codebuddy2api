@echo off
title CodeBuddy2API
cd /d "%~dp0"
if exist "dist\CodeBuddy2API.exe" (
    start "" "dist\CodeBuddy2API.exe"
) else (
    start "" /b pythonw app.py
)
