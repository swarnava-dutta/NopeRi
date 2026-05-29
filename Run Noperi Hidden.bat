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

rem Run. Use PowerShell Tee-Object because cmd.exe has no built-in tee.
call :write_status RUNNING "Noperi is running"
>> "!LOG_FILE!" echo.
>> "!LOG_FILE!" echo [%DATE% %TIME%] Starting Noperi with "!PYTHON_EXE!"

echo Starting Noperi...
echo ----------------------------------------

set "NOPERI_PYTHON_EXE=!PYTHON_EXE!"
set "NOPERI_MAIN_SCRIPT=!CD!\main.py"
set "NOPERI_LOG_FILE=!LOG_FILE!"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& { $enc = New-Object System.Text.UTF8Encoding $false; [Console]::OutputEncoding = $enc; $OutputEncoding = $enc; & $env:NOPERI_PYTHON_EXE $env:NOPERI_MAIN_SCRIPT 2>&1 | Tee-Object -FilePath $env:NOPERI_LOG_FILE -Append; exit $LASTEXITCODE }"
set "EXIT_CODE=!ERRORLEVEL!"

echo ----------------------------------------
>> "!LOG_FILE!" echo [%DATE% %TIME%] Noperi exited with code !EXIT_CODE!
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
