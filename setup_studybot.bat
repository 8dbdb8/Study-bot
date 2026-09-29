@echo off
setlocal

chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"

echo ========================================
echo StudyBot initial setup
echo ========================================
echo.

set "PYTHON_COMMAND="

where py >nul 2>&1
if not errorlevel 1 (
    py -3 --version >nul 2>&1
    if not errorlevel 1 set "PYTHON_COMMAND=py -3"
)

if not defined PYTHON_COMMAND (
    where python >nul 2>&1
    if not errorlevel 1 set "PYTHON_COMMAND=python"
)

if not defined PYTHON_COMMAND (
    echo [ERROR] Python 3 was not found.
    echo Install Python 3.11 or later, then run this script again.
    exit /b 1
)

%PYTHON_COMMAND% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.11 or later is required.
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/5] Creating Python virtual environment...
    %PYTHON_COMMAND% -m venv .venv
    if errorlevel 1 goto :setup_failed
) else (
    echo [1/5] Python virtual environment already exists.
)

echo [2/5] Installing Python packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :setup_failed

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :setup_failed

echo [3/5] Preparing local files...
if not exist "data" mkdir "data"

if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo Created .env from .env.example.
) else (
    echo Existing .env was kept unchanged.
)

echo [4/5] Checking Ollama...
where ollama >nul 2>&1

if errorlevel 1 (
    echo [WARNING] Ollama was not found.
    echo Install Ollama later to use AI analysis features.
) else (
    ollama list 2>nul | findstr /I /C:"qwen3:8b" >nul
    if errorlevel 1 (
        echo Downloading qwen3:8b. This may take a while...
        ollama pull qwen3:8b
        if errorlevel 1 (
            echo [WARNING] qwen3:8b could not be downloaded.
            echo Run "ollama pull qwen3:8b" after Ollama is running.
        )
    ) else (
        echo qwen3:8b is already installed.
    )
)

echo [5/5] Setup complete.
echo.
echo Next steps:
echo   1. Open .env and replace the placeholder with your Discord Bot token.
echo   2. Run start_studybot.bat.
exit /b 0

:setup_failed
echo.
echo [ERROR] Setup failed. Review the message above and try again.
exit /b 1
