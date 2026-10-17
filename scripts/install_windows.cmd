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
echo Optional: push-to-talk voice (about 250 MB; hold Left Alt + V in game). It needs a
echo microphone only when you speak; without one, typing keeps working.
choice /c YN /m "Install voice support now"
if not errorlevel 2 (
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements-voice.txt && ".venv\Scripts\python.exe" -m companion.voice --install || echo Voice was not installed; typing works without it.
)
".venv\Scripts\python.exe" -m companion.service --install || goto :failed
echo.
echo Done. In FS25, on foot: Left Alt + K to tell Farmhand what to do, Left Alt + L to stop,
echo Left Alt + N to choose an AI model (optional; offered once when FS25 first connects).
pause
exit /b 0
:failed
echo.
echo Setup failed; see the messages above.
pause
exit /b 1
