@echo off
chcp 65001 >nul
cd /d "%~dp0"
title ANIME VAULT
where python >nul 2>nul
if errorlevel 1 goto NOPY
python server.py
pause
exit /b 0

:NOPY
echo.
echo   Python not found. Please install Python 3.9+ and check "Add to PATH".
echo   Download: https://www.python.org/downloads/windows/
echo.
pause
exit /b 1
