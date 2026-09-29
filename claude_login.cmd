@echo off
rem One-time sign-in of the Claude Code command line (the Sell page's Draft button uses it).
rem A window opens: type /login , press Enter, finish the sign-in in the browser, then type /exit .
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo The project's .venv is missing. Open Claude Code in this folder and ask it to recreate it.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m depop_seller claude-login
if errorlevel 1 pause
