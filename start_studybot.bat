@echo off

chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

cd /d C:\Users\akash\Documents\study-bot

echo [%date% %time%] StudyBot starting... >> studybot.log

REM Ollamaが動いているか確認
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing http://localhost:11434/api/tags -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"

if errorlevel 1 (
    echo [%date% %time%] Ollama starting... >> studybot.log
    start "" /min ollama serve
    timeout /t 5 /nobreak >nul
)

REM StudyBot起動
".venv\Scripts\python.exe" -u bot.py >> studybot.log 2>&1