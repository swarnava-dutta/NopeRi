@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Capture script path before any blocks.
set "SCRIPT_PATH=%~f0"

rem Open a visible debug window if this was launched by double-click/startup.
if not defined NOPERI_LAUNCHED (
    set "NOPERI_LAUNCHED=1"
    start "Noperi" cmd /k call "!SCRIPT_PATH!"
    exit /b
)

rem Config.
set "ROOT=%~dp0"
set "VENV_PYTHON=!ROOT!.venv\Scripts\python.exe"
set "LOG_DIR=!ROOT!logs"
set "STATUS_FILE=!LOG_DIR!\noperi_status.txt"
set "LOG_FILE=!LOG_DIR!\noperi_hidden.log"
set "EXIT_CODE=1"

chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

if not exist "!LOG_DIR!" mkdir "!LOG_DIR!" >nul 2>&1

rem Move to project folder.
call :write_status STARTING "Opening project folder"
pushd "!ROOT!"
if errorlevel 1 (
    call :write_status FAILED "Could not open project folder"
    echo ERROR: Could not open project folder: !ROOT!
    set "EXIT_CODE=1"
    goto :done
)

rem Resolve Python.
if exist "!VENV_PYTHON!" (
    set "PYTHON_EXE=!VENV_PYTHON!"
    echo [DEBUG] Using venv Python: !VENV_PYTHON!
) else (
    set "PYTHON_EXE=python"
    echo [DEBUG] Venv not found, falling back to system Python
)

echo [DEBUG] Working directory : !CD!
echo [DEBUG] Log file          : !LOG_FILE!
echo.

rem Sanity checks.
if not exist "main.py" (
    call :write_status FAILED "main.py not found"
    echo ERROR: main.py not found in !CD!
    set "EXIT_CODE=1"
    goto :done
)

"!PYTHON_EXE!" --version >nul 2>&1
if errorlevel 1 (
    call :write_status FAILED "Python not found"
    echo ERROR: Python not found. Is it installed and on PATH?
    set "EXIT_CODE=1"
    goto :done
)

rem Run. All log writing is delegated to run_noperi.ps1 so exactly ONE writer
rem owns the file with one encoding (UTF-8, no BOM). Never append to !LOG_FILE!
rem from cmd with ">> echo": that writes ANSI, PowerShell 5.1's Tee-Object
rem writes UTF-16LE, and mixing the two left the old log ~48% NUL bytes and
rem unreadable in any single encoding.
call :write_status RUNNING "Noperi is running"

echo Starting Noperi...
echo ----------------------------------------

powershell -NoProfile -ExecutionPolicy Bypass -File "!ROOT!run_noperi.ps1" -PythonExe "!PYTHON_EXE!" -Script "!CD!\main.py" -LogFile "!LOG_FILE!"
set "EXIT_CODE=!ERRORLEVEL!"

echo ----------------------------------------
popd

rem Result.
if not "!EXIT_CODE!"=="0" (
    call :write_status FAILED "Noperi failed with exit code !EXIT_CODE!"
    echo.
    echo ERROR: Noperi failed with exit code !EXIT_CODE!
) else (
    call :write_status SUCCESS "Noperi completed successfully"
    echo.
    echo Noperi completed successfully.
)

:done
echo.
echo ========================================
echo  Done. Launcher terminal stays open for debugging.
echo  Log: !LOG_FILE!
echo  Type 'exit' to close.
echo ========================================
exit /b !EXIT_CODE!

:write_status
> "!STATUS_FILE!" (
    echo status=%~1
    echo message=%~2
    echo updated_at=%DATE% %TIME%
    echo log_file=!LOG_FILE!
)
exit /b 0
