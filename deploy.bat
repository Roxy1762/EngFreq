@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

cmd /c ""%APP_DIR%\start.bat" --bootstrap"
exit /b %errorlevel%
