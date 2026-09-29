@echo off
rem Depop Seller - double-click this the first time on a new computer.
rem
rem It sets the app up if that has not been done yet (a minute or two), puts a "Depop Seller" icon
rem on the Desktop, and opens the app. After that, use the Desktop icon: it opens the app with no
rem window at all. Come back to this file when you want to see what the app is doing.
cd /d "%~dp0"

rem Installed with DepopSellerSetup.exe: Python ships next to this folder, nothing to set up.
if exist "..\runtime\pythonw.exe" (
    start "" "..\runtime\pythonw.exe" -m depop_seller hub
    exit /b 0
)

if exist ".venv\Scripts\python.exe" goto run

echo.
echo   Setting up Depop Seller on this computer. This takes a minute or two.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "setup.ps1"
if errorlevel 1 (
    echo.
    echo   Setup did not finish. Read the messages above, then try again.
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   Setup did not finish - the .venv folder is missing.
    pause
    exit /b 1
)
echo.
echo   Done. There is now a "Depop Seller" icon on your Desktop - use that from now on.
echo   Opening the app...
echo.
start "" ".venv\Scripts\pythonw.exe" -m depop_seller hub
echo   You can close this window now.
pause
exit /b 0

:run
".venv\Scripts\python.exe" -m depop_seller hub %*
if errorlevel 1 pause
