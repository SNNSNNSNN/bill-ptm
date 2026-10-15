@echo off
setlocal enabledelayedexpansion

:: Portable paths - uses the folder this .bat file lives in
set AA_ROOT=%~dp0
set AA_ROOT=%AA_ROOT:~0,-1%
set AA_BOOKS=%AA_ROOT%\books
set MAIN_LIST=%AA_BOOKS%\aa_links.txt

:: SET WORKFLOW_MODE=resume for continuation, or remove/comment for fresh start
set WORKFLOW_MODE=%~1

if /i "!WORKFLOW_MODE!"=="resume" goto :resume_mode
goto :fresh_mode

:fresh_mode
echo ================================================
echo HEARTH AUTOMATED PIPELINE - FRESH START (UNIFIED)
echo ================================================
echo.
echo [1/3] Checking prerequisites...
where python >nul 2>nul
if !errorlevel! neq 0 (
    echo ERROR: Python not found in PATH. Install Python 3.8+ first.
    pause
    exit /b 1
)
echo + Python executable found

:: Check for main list inside the books folder (where hearth.py reads it)
if not exist "%MAIN_LIST%" (
    echo ERROR: aa_links.txt not found!
    echo Please place your link file here: %MAIN_LIST%
    pause
    exit /b 1
)
echo + aa_links.txt found

echo.
echo [2/3] Verifying output directory...
if not exist "%AA_BOOKS%" (
    echo Creating download directory...
    mkdir "%AA_BOOKS%"
)
echo + Download folder ready: %AA_BOOKS%

echo.
echo [3/3] Starting hearth downloader...
echo   Turkish filter is ACTIVE - will skip non-Turkish books automatically
echo   Keep browser window open for CAPTCHAs
echo   Press Ctrl+C or run stop_pipeline.ps1 to pause anytime
echo.
cd /d %AA_ROOT%
python hearth.py text "%AA_BOOKS%" full 5 --no-fast
if !errorlevel! neq 0 (
    echo ERROR: hearth.py failed with error code !errorlevel!
    pause
    exit /b 1
)

echo.
echo ================================================
echo Pipeline complete!
echo ================================================
echo Downloaded books: %AA_BOOKS%
echo Remaining links:  %MAIN_LIST% (auto-updated after each run)
echo Checkpoint:       .hearth_filter_progress.json (survives restarts)
echo.
pause
exit /b 0

:resume_mode
echo ================================================
echo HEARTH AUTOMATED PIPELINE - RESUME MODE
echo ================================================
echo.
echo [1/2] Resuming from previous session...
echo   aa_links.txt contains remaining unprocessed links
echo   Skips already-completed and non-Turkish items automatically

echo.
echo [2/2] Starting hearth downloader...
echo   Keep browser window open for CAPTCHAs
echo.
cd /d %AA_ROOT%
python hearth.py text "%AA_BOOKS%" full 5 --no-fast

echo.
echo + Pipeline resumed successfully!
echo.
pause
exit /b 0
