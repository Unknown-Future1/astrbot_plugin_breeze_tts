@echo off
chcp 65001 >nul 2>&1
title Stop Breeze TTS Server

echo.
echo   Stopping breeze-server.exe ...
echo.

rem 可选参数：端口号。给了就只结束占着这个端口的那个进程（更精准，不会误杀别的）。
if not "%~1"=="" goto byport

taskkill /IM breeze-server.exe /F >nul 2>&1
if errorlevel 1 (
    echo   No breeze-server.exe is running.
) else (
    echo   Stopped. VRAM released.
)
goto done

:byport
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p='%~1'; $c=@(Get-NetTCPConnection -LocalPort ([int]$p) -State Listen -ErrorAction SilentlyContinue); if ($c.Count -eq 0) { Write-Host ('  Nobody is listening on port ' + $p + '.') } else { $procId=[int]$c[0].OwningProcess; $pr=Get-Process -Id $procId -ErrorAction SilentlyContinue; if ($pr -and $pr.ProcessName -eq 'breeze-server') { taskkill /PID $procId /F >$null; Write-Host ('  Stopped PID ' + $procId + ' on port ' + $p + '. VRAM released.') } else { $who = if ($pr) { $pr.ProcessName } else { 'unknown' }; Write-Host ('  Port ' + $p + ' is held by ' + $who + ', not breeze-server. Nothing killed.') } }"

:done
echo.
timeout /t 2 >nul
