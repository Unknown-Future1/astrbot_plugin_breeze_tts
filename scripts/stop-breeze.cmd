@echo off
chcp 65001 >nul 2>&1
title Stop Breeze TTS Server

echo.
echo   Stopping breeze-server.exe ...
echo.

taskkill /IM breeze-server.exe /F >nul 2>&1
if errorlevel 1 (
    echo   No breeze-server.exe is running.
) else (
    echo   Stopped. VRAM released.
)

echo.
timeout /t 2 >nul
