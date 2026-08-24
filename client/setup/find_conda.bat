@echo off

REM Pure CMD discovery for locked-down corporate Windows systems. This file
REM deliberately does not load PowerShell scripts or request an execution-
REM policy bypass. It also does not use CALL-to-label helpers, which keeps it
REM reliable on mapped and network drives.
REM
REM NVTA_MINIFORGE_ONLY=1 searches only Miniforge.
REM NVTA_FALLBACK_ONLY=1 skips Miniforge and searches existing Conda fallbacks.
REM This helper intentionally does not use SETLOCAL because the selected values
REM must be returned to the calling batch file.

set "NVTA_EXPLICIT_MINIFORGE=%NVTA_MINIFORGE_EXE%"
set "NVTA_EXPLICIT_CONDA_FALLBACK=%NVTA_CONDA_EXE%"
set "NVTA_CONDA_EXE="
set "NVTA_CONDA_KIND="

if not defined NVTA_FALLBACK_ONLY if defined NVTA_EXPLICIT_MINIFORGE if exist "%NVTA_EXPLICIT_MINIFORGE%" (
    set "NVTA_CONDA_EXE=%NVTA_EXPLICIT_MINIFORGE%"
    set "NVTA_CONDA_KIND=Miniforge"
)

if not defined NVTA_FALLBACK_ONLY if not defined NVTA_CONDA_EXE if defined NVTA_EXPLICIT_CONDA_FALLBACK if exist "%NVTA_EXPLICIT_CONDA_FALLBACK%" (
    echo("%NVTA_EXPLICIT_CONDA_FALLBACK%"| "%SystemRoot%\System32\findstr.exe" /i /c:"miniforge" >nul
    if not errorlevel 1 set "NVTA_CONDA_EXE=%NVTA_EXPLICIT_CONDA_FALLBACK%"
    if not errorlevel 1 set "NVTA_CONDA_KIND=Miniforge"
)

if not defined NVTA_FALLBACK_ONLY if not defined NVTA_CONDA_EXE if defined NVTA_MINIFORGE_HOME if exist "%NVTA_MINIFORGE_HOME%\Scripts\conda.exe" (
    set "NVTA_CONDA_EXE=%NVTA_MINIFORGE_HOME%\Scripts\conda.exe"
    set "NVTA_CONDA_KIND=Miniforge"
)
if not defined NVTA_FALLBACK_ONLY if not defined NVTA_CONDA_EXE if defined NVTA_MINIFORGE_HOME if exist "%NVTA_MINIFORGE_HOME%\condabin\conda.bat" (
    set "NVTA_CONDA_EXE=%NVTA_MINIFORGE_HOME%\condabin\conda.bat"
    set "NVTA_CONDA_KIND=Miniforge"
)

if not defined NVTA_FALLBACK_ONLY if not defined NVTA_CONDA_EXE if exist "%LOCALAPPDATA%\Miniforge3\Scripts\conda.exe" (
    set "NVTA_CONDA_EXE=%LOCALAPPDATA%\Miniforge3\Scripts\conda.exe"
    set "NVTA_CONDA_KIND=Miniforge"
)
if not defined NVTA_FALLBACK_ONLY if not defined NVTA_CONDA_EXE if exist "%USERPROFILE%\Miniforge3\Scripts\conda.exe" (
    set "NVTA_CONDA_EXE=%USERPROFILE%\Miniforge3\Scripts\conda.exe"
    set "NVTA_CONDA_KIND=Miniforge"
)
if not defined NVTA_FALLBACK_ONLY if not defined NVTA_CONDA_EXE if exist "%ProgramData%\Miniforge3\Scripts\conda.exe" (
    set "NVTA_CONDA_EXE=%ProgramData%\Miniforge3\Scripts\conda.exe"
    set "NVTA_CONDA_KIND=Miniforge"
)
if not defined NVTA_FALLBACK_ONLY if not defined NVTA_CONDA_EXE if exist "%ProgramFiles%\Miniforge3\Scripts\conda.exe" (
    set "NVTA_CONDA_EXE=%ProgramFiles%\Miniforge3\Scripts\conda.exe"
    set "NVTA_CONDA_KIND=Miniforge"
)

if not defined NVTA_FALLBACK_ONLY if not defined NVTA_CONDA_EXE (
    for /f "delims=" %%C in ('where.exe conda.exe 2^>nul') do (
        if not defined NVTA_CONDA_EXE (
            echo("%%~fC"| "%SystemRoot%\System32\findstr.exe" /i /c:"miniforge" >nul
            if not errorlevel 1 set "NVTA_CONDA_EXE=%%~fC"
            if not errorlevel 1 set "NVTA_CONDA_KIND=Miniforge"
        )
    )
)

if defined NVTA_MINIFORGE_ONLY if not defined NVTA_CONDA_EXE (
    echo [INFO] No existing Miniforge installation was found.
    set "NVTA_EXPLICIT_MINIFORGE="
    set "NVTA_EXPLICIT_CONDA_FALLBACK="
    exit /b 1
)

if not defined NVTA_MINIFORGE_ONLY if not defined NVTA_CONDA_EXE if defined NVTA_EXPLICIT_CONDA_FALLBACK if exist "%NVTA_EXPLICIT_CONDA_FALLBACK%" (
    set "NVTA_CONDA_EXE=%NVTA_EXPLICIT_CONDA_FALLBACK%"
    set "NVTA_CONDA_KIND=Existing Conda fallback"
)
if not defined NVTA_MINIFORGE_ONLY if not defined NVTA_CONDA_EXE if exist "%USERPROFILE%\Miniconda3\Scripts\conda.exe" (
    set "NVTA_CONDA_EXE=%USERPROFILE%\Miniconda3\Scripts\conda.exe"
    set "NVTA_CONDA_KIND=Miniconda fallback"
)
if not defined NVTA_MINIFORGE_ONLY if not defined NVTA_CONDA_EXE if exist "%USERPROFILE%\Anaconda3\Scripts\conda.exe" (
    set "NVTA_CONDA_EXE=%USERPROFILE%\Anaconda3\Scripts\conda.exe"
    set "NVTA_CONDA_KIND=Anaconda fallback"
)
if not defined NVTA_MINIFORGE_ONLY if not defined NVTA_CONDA_EXE if exist "%LOCALAPPDATA%\Miniconda3\Scripts\conda.exe" (
    set "NVTA_CONDA_EXE=%LOCALAPPDATA%\Miniconda3\Scripts\conda.exe"
    set "NVTA_CONDA_KIND=Miniconda fallback"
)
if not defined NVTA_MINIFORGE_ONLY if not defined NVTA_CONDA_EXE if exist "%LOCALAPPDATA%\Anaconda3\Scripts\conda.exe" (
    set "NVTA_CONDA_EXE=%LOCALAPPDATA%\Anaconda3\Scripts\conda.exe"
    set "NVTA_CONDA_KIND=Anaconda fallback"
)
if not defined NVTA_MINIFORGE_ONLY if not defined NVTA_CONDA_EXE if exist "%ProgramData%\Miniconda3\Scripts\conda.exe" (
    set "NVTA_CONDA_EXE=%ProgramData%\Miniconda3\Scripts\conda.exe"
    set "NVTA_CONDA_KIND=Miniconda fallback"
)
if not defined NVTA_MINIFORGE_ONLY if not defined NVTA_CONDA_EXE if exist "%ProgramData%\Anaconda3\Scripts\conda.exe" (
    set "NVTA_CONDA_EXE=%ProgramData%\Anaconda3\Scripts\conda.exe"
    set "NVTA_CONDA_KIND=Anaconda fallback"
)
if not defined NVTA_MINIFORGE_ONLY if not defined NVTA_CONDA_EXE if exist "%USERPROFILE%\Mambaforge\Scripts\conda.exe" (
    set "NVTA_CONDA_EXE=%USERPROFILE%\Mambaforge\Scripts\conda.exe"
    set "NVTA_CONDA_KIND=Mambaforge fallback"
)
if not defined NVTA_MINIFORGE_ONLY if not defined NVTA_CONDA_EXE (
    for /f "delims=" %%C in ('where.exe conda.exe 2^>nul') do if not defined NVTA_CONDA_EXE (
        set "NVTA_CONDA_EXE=%%~fC"
        set "NVTA_CONDA_KIND=PATH Conda fallback"
    )
)

set "NVTA_EXPLICIT_MINIFORGE="
set "NVTA_EXPLICIT_CONDA_FALLBACK="

if not defined NVTA_CONDA_EXE (
    echo [ERROR] No usable Miniforge or existing Conda fallback was found.
    echo [ERROR] Run setup_environment.bat to install user-local Miniforge.
    exit /b 1
)

REM Do not let an already-active environment from another Conda distribution
REM override the explicitly selected executable. Public callers use SETLOCAL,
REM so the user's shell is unchanged.
set "CONDA_PREFIX="
set "CONDA_DEFAULT_ENV="
set "CONDA_PROMPT_MODIFIER="
set "CONDA_SHLVL="
set "CONDA_EXE="
set "CONDA_PYTHON_EXE="
set "_CE_CONDA="
set "_CE_M="

call "%NVTA_CONDA_EXE%" --version >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Conda could not run: "%NVTA_CONDA_EXE%"
    set "NVTA_CONDA_EXE="
    set "NVTA_CONDA_KIND="
    exit /b 1
)

echo [OK] Using %NVTA_CONDA_KIND%: "%NVTA_CONDA_EXE%"
exit /b 0
