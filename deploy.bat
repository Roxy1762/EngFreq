@echo off
:: Production deployment helper (Windows).
::
:: Usage:
::   deploy.bat             Bootstrap + start in production mode (foreground)
::   deploy.bat --docker    Build and start with docker compose
::   deploy.bat --stop      Stop running daemon (taskkill via PID file)
::   deploy.bat --status    Report daemon status
::   deploy.bat --healthcheck  Ping /healthz once
::
setlocal EnableExtensions EnableDelayedExpansion

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

set "PID_FILE=%APP_DIR%\data\server.pid"
set "LOG_FILE=%APP_DIR%\data\server.log"
if not defined PORT set "PORT=8000"
if not defined HEALTHCHECK_URL set "HEALTHCHECK_URL=http://127.0.0.1:%PORT%/healthz"

if "%~1"=="--help" (
    echo Usage: deploy.bat [--docker^|--stop^|--status^|--healthcheck]
    exit /b 0
)

if "%~1"=="--docker" (
    where docker >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Docker not installed
        exit /b 1
    )
    cd /d "%APP_DIR%"
    echo [INFO] Building ^& starting via docker compose...
    docker compose up -d --build
    exit /b %errorlevel%
)

if "%~1"=="--stop" (
    if exist "%PID_FILE%" (
        set /p PID=<"%PID_FILE%"
        taskkill /pid !PID! /f >nul 2>&1
        del "%PID_FILE%" >nul 2>&1
        echo [OK] Daemon stopped.
    ) else (
        echo [INFO] No PID file.
    )
    exit /b 0
)

if "%~1"=="--status" (
    if exist "%PID_FILE%" (
        set /p PID=<"%PID_FILE%"
        tasklist /fi "PID eq !PID!" | findstr !PID! >nul 2>&1
        if errorlevel 1 (
            echo [INFO] Daemon is not running ^(stale PID file^).
            del "%PID_FILE%" >nul 2>&1
            exit /b 1
        )
        echo [OK] Daemon running ^(pid=!PID!^).
        exit /b 0
    )
    echo [INFO] Daemon is not running.
    exit /b 1
)

if "%~1"=="--healthcheck" (
    where curl >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] curl not installed
        exit /b 2
    )
    curl -fsS --max-time 5 "%HEALTHCHECK_URL%" >nul
    if errorlevel 1 (
        echo [FAIL] Healthcheck failed: %HEALTHCHECK_URL%
        exit /b 1
    )
    echo [OK] Healthcheck passed: %HEALTHCHECK_URL%
    exit /b 0
)

:: Default: bootstrap + start
call "%APP_DIR%\start.bat" --bootstrap
exit /b %errorlevel%
