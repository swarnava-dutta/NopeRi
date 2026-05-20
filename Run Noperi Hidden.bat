@echo off
setlocal

set "ROOT=%~dp0"
set "VENV_PYTHONW=%ROOT%.venv\Scripts\pythonw.exe"

if exist "%VENV_PYTHONW%" (
    start "" /b /d "%ROOT%" "%VENV_PYTHONW%" "main.py"
) else (
    start "" /b /d "%ROOT%" pythonw "main.py"
)

exit /b 0
