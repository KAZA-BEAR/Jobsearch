@echo off
REM ===================================================================
REM  build.bat - builds dist\EUJobSearch.exe from source on Windows.
REM
REM  Double-click it, or run from a terminal:
REM      build.bat              normal build (pauses at the end)
REM      build.bat /nopause     no prompts - for scripts / CI
REM      build.bat /nojobspy    skip the optional LinkedIn/Indeed engine
REM                             (smaller .exe, LinkedIn / Indeed panel disabled)
REM      build.bat /fresh       delete and recreate the .venv first
REM  Flags can be combined. Requires Python 3.10+ on PATH.
REM
REM  Nothing here changes system settings: no setx, no registry, no
REM  admin rights. Everything installs into .venv next to this file.
REM ===================================================================

setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "NOPAUSE="
set "NOJOBSPY="
set "FRESH="
for %%A in (%*) do (
    if /i "%%~A"=="/nopause"  set "NOPAUSE=1"
    if /i "%%~A"=="/nojobspy" set "NOJOBSPY=1"
    if /i "%%~A"=="/fresh"    set "FRESH=1"
)

echo.
echo  === EU Job Search - Windows build ===
echo.

REM ---- 1. Python ------------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo  [X] Python not found on PATH.
    echo      Install it from python.org and tick "Add Python to PATH".
    goto :fail
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo  [X] Python 3.10 or newer is required.
    goto :fail
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
echo  [1/7] Python !PYVER!

REM ---- 2. Close a running copy (it locks dist\EUJobSearch.exe) ---------
tasklist /FI "IMAGENAME eq EUJobSearch.exe" 2>nul | find /i "EUJobSearch.exe" >nul
if not errorlevel 1 (
    echo  [X] EUJobSearch.exe is running. Close it and run the build again.
    goto :fail
)

REM ---- 3. Build environment ------------------------------------------
if defined FRESH if exist ".venv" (
    echo  [2/7] Removing old .venv ...
    rmdir /s /q .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo  [2/7] Creating .venv ...
    python -m venv .venv || goto :fail
) else (
    echo  [2/7] Using existing .venv
)
set "PY=%~dp0.venv\Scripts\python.exe"

REM ---- 4. Dependencies -------------------------------------------------
echo  [3/7] Installing dependencies ...
"%PY%" -m pip install --upgrade pip --quiet --disable-pip-version-check || goto :fail
"%PY%" -m pip install -r requirements.txt --quiet --disable-pip-version-check || goto :fail

if defined NOJOBSPY (
    echo        skipping JobSpy ^(/nojobspy^)
) else (
    REM Optional: bundles the LinkedIn/Indeed search engine (~150 MB of
    REM numpy/pandas). A failure here does not stop the build - the
    REM LinkedIn / Indeed panel then just reports "not installed".
    "%PY%" -c "import jobspy" >nul 2>&1
    if errorlevel 1 (
        echo        installing JobSpy ^(optional^) ...
        "%PY%" install_jobspy.py >nul 2>&1
        if errorlevel 1 echo  [!] JobSpy install failed - continuing without it.
    ) else (
        echo        JobSpy already installed
    )
)

"%PY%" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo  [X] Tkinter is missing. Re-run the Python installer, choose
    echo      "Modify", and enable "tcl/tk and IDLE".
    goto :fail
)

REM ---- 5. Check the sources --------------------------------------------
echo  [4/7] Compiling every .py file ...
for %%F in (*.py) do (
    "%PY%" -m py_compile "%%F"
    if errorlevel 1 (
        echo  [X] Syntax error in %%F
        goto :fail
    )
)
echo  [5/7] Import check ...
REM Imports every module the .exe bundles, so a missing dependency or a
REM broken import fails here in seconds instead of inside the built .exe.
set "JOBSEARCH_DATA_DIR=%TEMP%\EUJobSearch-buildcheck"
"%PY%" -c "import job_gui, daily_sweep, fit_score, linkedin_jobs, eu_student_jobs, robotics_track, us_asia_jobs, jobspy_provider, ba_jobsuche, personio_jobs, workday_jobs, research_jobs, company_radar, system_checks, ats_pipeline, ats_checks, ats_plan, ats_embed, ats_latex, ats_prompts, ats_regions; from fit_score import load_default_scorer; assert load_default_scorer() is not None, 'profile.json missing or invalid'; print('       all modules import, profile.json OK')" || goto :fail
set "JOBSEARCH_DATA_DIR="

REM ---- 6. Build ----------------------------------------------------------
echo  [6/7] Building the .exe (1-3 minutes) ...
if exist "build" rmdir /s /q build
if exist "dist\EUJobSearch.exe" del /q "dist\EUJobSearch.exe"
"%PY%" -m PyInstaller jobsearch.spec --clean --noconfirm --log-level WARN || goto :fail

REM ---- 7. Result ---------------------------------------------------------
if not exist "dist\EUJobSearch.exe" (
    echo  [X] PyInstaller finished but dist\EUJobSearch.exe was not produced.
    goto :fail
)
for %%A in ("dist\EUJobSearch.exe") do set /a "SIZE_MB=%%~zA / 1048576"
echo  [7/7] Built dist\EUJobSearch.exe  (!SIZE_MB! MB)
echo.
echo  The .exe runs on any Windows PC without Python. Your data lives in
echo      %%APPDATA%%\EUJobSearch   (jobs database, sweep settings, CV outputs)
echo  Your profile.json is read from the file you pick with Browse (remembered),
echo  else %%APPDATA%%\EUJobSearch, else next to or one folder above the .exe.
echo.

if defined NOPAUSE exit /b 0
choice /C YN /M "  Launch it now"
if not errorlevel 2 start "" "dist\EUJobSearch.exe"
exit /b 0

:fail
echo.
echo  [X] Build failed - see the messages above.
if not defined NOPAUSE pause
exit /b 1
