@echo off
chcp 65001 >nul 2>&1
title Breeze TTS Server - Port 9000
cd /d "%~dp0"
echo Starting Breeze TTS 2 ... please keep this window open.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-breeze.ps1"
if errorlevel 1 pause
