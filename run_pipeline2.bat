@echo off
setlocal enabledelayedexpansion

set AA_ROOT=C:\Users\sinan\Desktop\AA_DOWNLOADS2
set AA_BOOKS=C:\Users\sinan\Desktop\AA_DOWNLOADS2\books
set MAIN_LIST=%AA_ROOT%\aa_links.txt

set WORKFLOW_MODE=%~1

if /i "!WORKFLOW_MODE!"=="resume" goto :resume_mode
goto :fresh_mode

:fresh_mode
echo ================================================
echo HEARTH AUTOMATED PIPELINE - LAPTOP 2
echo ================================================
echo.
echo [1/3] Checking prerequisites...
where python >nul 2>nul
if !errorlevel! neq 0 (
    echo ERROR: Python not found in PATH. Install Python 3.8+ first.
    pause
    exit /b 1
)

if not exist "%MAIN_LIST%" (
    echo ERROR: aa_links.txt not found!
    echo Please ensure your link file exists in %AA_ROOT%
    pause
    exit /b 1
)
echo ✓ aa_links.txt found

echo ✓ Python executable found

echo.
echo [2/3] Verifying output directory...
if not exist "%AA_BOOKS%" (
    echo Creating download directory...
    mkdir "%AA_BOOKS%"
)
echo ✓ Download folder ready: %AA_BOOKS%

echo.
echo [3/3] Starting hearth downloader...
echo ℹ Turkish filter is ACTIVE - will skip non-Turkish books automatically
echo ℹ Keep browser window open for CAPTCHAs
echo ℹ Press Ctrl+C or run stop_pipeline.ps1 to pause anytime
echo.
cd /d %AA_ROOT%
python hearth.py text "%AA_BOOKS%" full 5
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
echo.
pause
exit /b 0

:resume_mode
echo ================================================
echo HEARTH AUTOMATED PIPELINE - RESUME MODE
echo ================================================
echo.
echo [1/2] Resuming from previous session...
echo ℹ aa_links.txt contains remaining unprocessed links
echo ℹ Skips already-completed and non-Turkish items automatically

echo.
echo [2/2] Starting hearth downloader...
echo ℹ Keep browser window open for CAPTCHAs
echo.
cd /d %AA_ROOT%
python hearth.py text "%AA_BOOKS%" full 5

echo.
echo ✓ Pipeline resumed successfully!
echo.
pause
exit /b 0
