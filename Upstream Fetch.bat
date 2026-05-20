@echo off
echo ==========================================
echo   Syncing with Upstream (Original Repo)
echo ==========================================

:: Check if we're in a git repo
git rev-parse --git-dir >nul 2>&1
if %errorlevel% neq 0 ( echo ERROR: Not a git repository. & pause & exit /b 1 )

:: Store current branch name
for /f "tokens=*" %%a in ('git rev-parse --abbrev-ref HEAD') do set CURRENT_BRANCH=%%a
echo Current branch: %CURRENT_BRANCH%

:: Block running from main branch
if "%CURRENT_BRANCH%"=="main" (
    echo.
    echo ERROR: You are on 'main' branch!
    echo Please switch to your own branch first:
    echo   git checkout my-changes
    echo Or create it:
    echo   git checkout -b my-changes
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
echo [1/4] Fetching upstream...
git fetch upstream
if %errorlevel% neq 0 ( echo ERROR: Failed to fetch upstream. & pause & exit /b 1 )

:: Switch to main and merge
echo.
echo [2/4] Updating main...
git checkout main
git merge upstream/main
if %errorlevel% neq 0 ( echo ERROR: Merge failed on main. & pause & exit /b 1 )

:: Push updated main to your fork
echo.
echo [3/4] Pushing main to your fork...
git push origin main
if %errorlevel% neq 0 ( echo ERROR: Push to origin failed. & pause & exit /b 1 )

:: Go back to your branch and rebase
echo.
echo [4/4] Rebasing your branch: %CURRENT_BRANCH%...
git checkout %CURRENT_BRANCH%
git rebase main
if %errorlevel% neq 0 (
    echo.
    echo WARNING: Rebase has conflicts!
    echo Fix conflicts in your editor, then run:
    echo   git rebase --continue
    echo Or to cancel:
    echo   git rebase --abort
    pause
    exit /b 1
)

:: Force push your branch
echo.
echo Pushing %CURRENT_BRANCH% to your fork...
git push origin %CURRENT_BRANCH% --force-with-lease
if %errorlevel% neq 0 ( echo ERROR: Push failed. & pause & exit /b 1 )

echo.
echo ==========================================
echo   Done! Your branch '%CURRENT_BRANCH%' is up to date.
echo ==========================================
pause