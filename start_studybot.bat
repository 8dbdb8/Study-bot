@echo off
setlocal

chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python virtual environment was not found.
    echo Run setup_studybot.bat first.
    exit /b 1
)

if not exist ".env" (
    echo [ERROR] .env was not found.
    echo Run setup_studybot.bat and set DISCORD_TOKEN in .env.
    exit /b 1
)

findstr /X /C:"DISCORD_TOKEN=replace_with_your_discord_bot_token" ".env" >nul
if not errorlevel 1 (
    echo [ERROR] DISCORD_TOKEN is still the placeholder value.
    echo Open .env and set your actual Discord Bot token.
    exit /b 1
)

echo [%date% %time%] StudyBot starting... >> studybot.log

where ollama >nul 2>&1

if errorlevel 1 (
    echo [%date% %time%] Ollama was not found. AI features may be unavailable. >> studybot.log
) else (
    REM Ollamaが動いているか確認
    powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing http://localhost:11434/api/tags -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"

    if errorlevel 1 (
        echo [%date% %time%] Ollama starting... >> studybot.log
        start "" /min ollama serve
        timeout /t 5 /nobreak >nul
    )
)

REM StudyBot起動
".venv\Scripts\python.exe" -u bot.py >> studybot.log 2>&1

set "BOT_EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] StudyBot stopped with exit code %BOT_EXIT_CODE%. >> studybot.log
exit /b %BOT_EXIT_CODE%
