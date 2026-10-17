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
echo.
echo Optional: a local language model (about 2.5 GB download, runs on the CPU, never
echo the graphics card) understands wording the built-in rules do not. Farmhand works
echo without it.
choice /c YN /m "Install the local language model now"
if not errorlevel 2 (
    ".venv\Scripts\python.exe" -m companion.model --install || echo The model was not installed; Farmhand works without it.
)
".venv\Scripts\python.exe" -m companion.service --install || goto :failed
echo.
echo Done. In FS25, on foot: Left Alt + K to tell Farmhand what to do, Left Alt + L to stop.
pause
exit /b 0
:failed
echo.
echo Setup failed; see the messages above.
pause
exit /b 1
