@echo off
setlocal
chcp 65001 >nul
title StudyBot Control
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0manage_studybot.ps1"
if errorlevel 1 (
    echo.
    echo StudyBot control could not finish. Press any key to close.
    pause >nul
)
