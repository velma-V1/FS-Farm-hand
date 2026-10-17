@echo off
rem One-time setup of the Farmhand companion on the Windows PC next to FS25.
rem Double-click it. It creates .venv here, installs the pinned runtime
rem requirements and starts the companion now and at every logon, windowless
rem (python -m companion.service --install). Needs Python 3.12 (py launcher).
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv || goto :failed
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || goto :failed
".venv\Scripts\python.exe" -m companion.service --install || goto :failed
echo.
echo Done. In FS25, press Left Alt + J on foot to bring a tractor and forage wagon.
pause
exit /b 0
:failed
echo.
echo Setup failed; see the messages above.
pause
exit /b 1
