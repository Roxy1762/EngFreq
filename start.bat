@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
cd /d "%APP_DIR%"

set "VENV_DIR=%APP_DIR%\.venv"
set "BOOTSTRAP=0"
set "INSTALL_ONLY=0"

:parse_args
if "%~1"=="" goto after_args
if /i "%~1"=="--bootstrap" set "BOOTSTRAP=1"
if /i "%~1"=="--install-only" (
    set "BOOTSTRAP=1"
    set "INSTALL_ONLY=1"
)
if /i "%~1"=="--help" goto usage
shift
goto parse_args

:after_args
echo [INFO] App dir: %APP_DIR%
call :resolve_python
if errorlevel 1 exit /b 1
call :ensure_venv
if errorlevel 1 exit /b 1
call :ensure_env
if errorlevel 1 exit /b 1
call :ensure_requirements
if errorlevel 1 exit /b 1
call :ensure_resources

if "%INSTALL_ONLY%"=="1" (
    echo [OK] Bootstrap complete.
    exit /b 0
)

echo [INFO] Starting server on http://127.0.0.1:8000
"%PYTHON%" run.py --prod
exit /b %errorlevel%

:usage
echo Usage: start.bat [--bootstrap] [--install-only]
echo.
echo   --bootstrap    Create venv, install requirements, prepare resources.
echo   --install-only Run bootstrap steps and exit.
exit /b 0

:resolve_python
if exist "%VENV_DIR%\Scripts\python.exe" (
    "%VENV_DIR%\Scripts\python.exe" -m pip --version >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON=%VENV_DIR%\Scripts\python.exe"
        exit /b 0
    )
)

set "SYSTEM_PYTHON="
call :try_python "python" "python"
if defined SYSTEM_PYTHON exit /b 0
call :try_python "py -3.13" "py -3.13"
if defined SYSTEM_PYTHON exit /b 0
call :try_python "py -3.12" "py -3.12"
if defined SYSTEM_PYTHON exit /b 0
call :try_python "py -3.11" "py -3.11"
if defined SYSTEM_PYTHON exit /b 0
call :try_python "py -3.10" "py -3.10"
if defined SYSTEM_PYTHON exit /b 0
call :try_python "python3" "python3"
if defined SYSTEM_PYTHON exit /b 0
call :try_python "py -3" "py -3"
if defined SYSTEM_PYTHON exit /b 0

echo [ERROR] Python 3.10+ was not found.
exit /b 1

:try_python
%~2 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "SYSTEM_PYTHON=%~1"
exit /b 0

:ensure_venv
if exist "%VENV_DIR%\Scripts\python.exe" (
    "%VENV_DIR%\Scripts\python.exe" -m pip --version >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON=%VENV_DIR%\Scripts\python.exe"
        exit /b 0
    )
)

if exist "%VENV_DIR%" (
    echo [WARN] Found incomplete virtual environment. Removing it...
    rmdir /s /q "%VENV_DIR%"
    if exist "%VENV_DIR%" (
        echo [ERROR] Could not remove broken virtual environment.
        exit /b 1
    )
)

echo [INFO] Creating virtual environment...
%SYSTEM_PYTHON% -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo [ERROR] Failed to create virtual environment.
    exit /b 1
)

set "PYTHON=%VENV_DIR%\Scripts\python.exe"
exit /b 0

:ensure_env
set "WRITE_ENV=0"
if not exist "%APP_DIR%\.env" set "WRITE_ENV=1"
if exist "%APP_DIR%\.env" (
    findstr /r /c:"^ADMIN_PASSWORD=." "%APP_DIR%\.env" >nul 2>&1
    if errorlevel 1 set "WRITE_ENV=1"
)
if "%WRITE_ENV%"=="0" goto env_done

echo [INFO] Writing default .env file...
(
    echo HOST=0.0.0.0
    echo PORT=8000
    echo ADMIN_USERNAME=admin
    echo ADMIN_PASSWORD=admin123
    echo AI_MODEL=claude-opus-4-7
    echo AI_BATCH_SIZE=25
    echo AI_PROMPT_CACHING=true
    echo VOCAB_PROVIDER=free_dict
    echo PROMPT_DOMAIN=gaokao
    echo PROMPT_VERSION=v2
    echo LLM_RETRY_ATTEMPTS=3
    echo WEIGHT_BODY=1.0
    echo WEIGHT_STEM=1.5
    echo WEIGHT_OPTION=3.0
    echo OCR_LANGUAGE=eng
    echo MAX_UPLOAD_MB=50
    echo UPLOAD_DIR=data/uploads
    echo RESULTS_DIR=data/exports
    echo OCR_CACHE_DIR=data/ocr_cache
    echo FILE_STORE_DIR=data/files
    echo DB_PATH=app.db
) > "%APP_DIR%\.env"

echo [INFO] Default admin user: admin
echo [INFO] Default admin password: admin123

:env_done
if not exist "%APP_DIR%\data" mkdir "%APP_DIR%\data" >nul 2>&1
if not exist "%APP_DIR%\data\uploads" mkdir "%APP_DIR%\data\uploads" >nul 2>&1
if not exist "%APP_DIR%\data\exports" mkdir "%APP_DIR%\data\exports" >nul 2>&1
if not exist "%APP_DIR%\data\ocr_cache" mkdir "%APP_DIR%\data\ocr_cache" >nul 2>&1
if not exist "%APP_DIR%\data\files" mkdir "%APP_DIR%\data\files" >nul 2>&1
exit /b 0

:ensure_requirements
"%PYTHON%" -c "import fastapi, uvicorn, sqlalchemy" >nul 2>&1
if errorlevel 1 set "BOOTSTRAP=1"

if "%BOOTSTRAP%"=="0" exit /b 0

echo [INFO] Installing Python packages...
"%PYTHON%" -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] Failed to upgrade pip.
    exit /b 1
)

"%PYTHON%" -m pip install -r "%APP_DIR%\requirements.txt"
if errorlevel 1 (
    echo [ERROR] Failed to install requirements.
    exit /b 1
)

exit /b 0

:ensure_resources
if "%BOOTSTRAP%"=="0" exit /b 0

echo [INFO] Preparing language resources...
"%PYTHON%" -c "import spacy; spacy.load('en_core_web_sm')" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Downloading spaCy model en_core_web_sm...
    "%PYTHON%" -m spacy download en_core_web_sm
)

"%PYTHON%" -c "import nltk; [nltk.download(pkg, quiet=True) for pkg in ('wordnet', 'averaged_perceptron_tagger', 'punkt', 'stopwords')]" >nul 2>&1
exit /b 0
