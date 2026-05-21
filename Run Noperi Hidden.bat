@echo off
setlocal

set "ROOT=%~dp0"
set "VENV_PYTHON=%ROOT%.venv\Scripts\python.exe"

pushd "%ROOT%"
if errorlevel 1 (
    echo ERROR: Could not open project folder:
    echo %ROOT%
    pause
    exit /b 1
)

if exist "%VENV_PYTHON%" (
    set "PYTHON_EXE=%VENV_PYTHON%"
) else (
    set "PYTHON_EXE=python"
)

echo Starting Noperi...
"%PYTHON_EXE%" "main.py"
set "EXIT_CODE=%ERRORLEVEL%"

popd

if not "%EXIT_CODE%"=="0" (
    echo.
    echo ERROR: Noperi failed with exit code %EXIT_CODE%.
    pause
    exit /b %EXIT_CODE%
)

exit /b 0
