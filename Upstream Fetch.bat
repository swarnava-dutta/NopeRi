@echo off
setlocal enabledelayedexpansion

echo ==========================================
echo   Syncing with Upstream (Original Repo)
echo ==========================================

:: Check if we're in a git repo
git rev-parse --git-dir >nul 2>&1
if %errorlevel% neq 0 ( echo ERROR: Not a git repository. & pause & exit /b 1 )

:: Check for uncommitted changes BEFORE doing anything
git diff --quiet
if %errorlevel% neq 0 (
    echo.
    echo ERROR: You have unstaged changes.
    echo Commit or stash them first:
    echo   git stash
    pause
    exit /b 1
)
git diff --cached --quiet
if %errorlevel% neq 0 (
    echo.
    echo ERROR: You have staged but uncommitted changes.
    echo Commit or stash them first.
    pause
    exit /b 1
)

:: Store current branch name
for /f "tokens=*" %%a in ('git rev-parse --abbrev-ref HEAD') do set CURRENT_BRANCH=%%a
echo Current branch: %CURRENT_BRANCH%

:: Block running from main branch
if "%CURRENT_BRANCH%"=="main" (
    echo.
    echo ERROR: You are on 'main' branch!
    echo Please switch to your own branch first:
    echo   git checkout swarnava
    echo Or create it:
    echo   git checkout -b swarnava
    pause
    exit /b 1
)

:: Verify upstream remote exists
git remote get-url upstream >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo ERROR: No 'upstream' remote found.
    echo Add it first:
    echo   git remote add upstream https://github.com/ORIGINAL_OWNER/REPO_NAME.git
    pause
    exit /b 1
)

:: Show current remotes for info
echo.
echo Current remotes:
git remote -v

:: Verify origin is not pointing to upstream
for /f "tokens=*" %%a in ('git remote get-url origin') do set ORIGIN_URL=%%a
for /f "tokens=*" %%a in ('git remote get-url upstream') do set UPSTREAM_URL=%%a

if "%ORIGIN_URL%"=="%UPSTREAM_URL%" (
    echo.
    echo ERROR: origin and upstream are pointing to the same repo!
    echo origin is set to: %ORIGIN_URL%
    echo.
    echo Fix it by running:
    echo   git remote set-url origin https://github.com/YOUR_USERNAME/REPO_NAME.git
    pause
    exit /b 1
)

:: Fetch latest from upstream
echo.
echo [1/5] Fetching upstream...
git fetch upstream
if %errorlevel% neq 0 ( echo ERROR: Failed to fetch upstream. & pause & exit /b 1 )

:: Fetch origin too, so we know the true remote state of our branch
echo.
echo [2/5] Fetching origin...
git fetch origin
if %errorlevel% neq 0 ( echo ERROR: Failed to fetch origin. & pause & exit /b 1 )

:: Safety check: has origin/%CURRENT_BRANCH% moved ahead of what we last saw?
:: (i.e. did someone else, or another machine, push to this branch?)
git rev-parse origin/%CURRENT_BRANCH% >nul 2>&1
if %errorlevel% equ 0 (
    for /f "tokens=*" %%a in ('git merge-base HEAD origin/%CURRENT_BRANCH%') do set MERGE_BASE=%%a
    for /f "tokens=*" %%a in ('git rev-parse origin/%CURRENT_BRANCH%') do set REMOTE_HEAD=%%a
    if not "!MERGE_BASE!"=="!REMOTE_HEAD!" (
        echo.
        echo WARNING: origin/%CURRENT_BRANCH% has commits your local branch doesn't have.
        echo Someone else may have pushed here, or you pushed from another machine.
        echo Run 'git pull origin %CURRENT_BRANCH%' first and re-run this script.
        pause
        exit /b 1
    )
)

:: Switch to main and merge
echo.
echo [3/5] Updating main...
git checkout main
git merge upstream/main
if %errorlevel% neq 0 (
    echo ERROR: Merge failed on main. Resolve conflicts manually, then re-run.
    pause
    exit /b 1
)

:: Push updated main to your fork
echo.
echo [4/5] Pushing main to your fork...
git push origin main
if %errorlevel% neq 0 ( echo ERROR: Push to origin failed. & pause & exit /b 1 )

:: Go back to your branch and rebase
echo.
echo [5/5] Rebasing your branch: %CURRENT_BRANCH%...
git checkout %CURRENT_BRANCH%
git rebase main
if %errorlevel% neq 0 (
    echo.
    echo ==========================================
    echo   REBASE CONFLICT
    echo ==========================================
    echo Git paused the rebase because one or more files conflict
    echo ^(e.g. both you and upstream edited the same lines^).
    echo.
    echo 1. Open the conflicted file^(s^) and look for ^<^<^<^<^<^<^< markers
    echo 2. Edit to keep the correct code, then remove the markers
    echo 3. Run:
    echo      git add ^<file^>
    echo      git rebase --continue
    echo    ^(repeat if more commits conflict^)
    echo.
    echo To abort and go back to before this script ran:
    echo      git rebase --abort
    pause
    exit /b 1
)

:: Force push your branch
echo.
echo Pushing %CURRENT_BRANCH% to your fork...
git push origin %CURRENT_BRANCH% --force-with-lease
if %errorlevel% neq 0 (
    echo ERROR: Push failed. If lease was rejected, someone pushed to
    echo origin/%CURRENT_BRANCH% while this script was running. Run
    echo 'git fetch origin' and investigate before forcing.
    pause
    exit /b 1
)

echo.
echo ==========================================
echo   Done! Your branch '%CURRENT_BRANCH%' is up to date.
echo ==========================================
pause