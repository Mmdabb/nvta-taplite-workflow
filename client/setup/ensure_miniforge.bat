@echo off

REM Corporate-safe Miniforge bootstrap.
REM - Uses CMD and signed Windows executables.
REM - Never loads an unsigned PowerShell script file.
REM - Never requests an execution-policy bypass.
REM - Installs only user-local Miniforge; other Conda distributions are
REM   existing-installation fallbacks and are never downloaded.
REM This helper intentionally does not use SETLOCAL because selected values
REM must be returned to setup_environment.bat.

echo [INFO] Miniforge bootstrap revision: 2026-08-11c

if not defined NVTA_MINIFORGE_MIN_CONDA_VERSION set "NVTA_MINIFORGE_MIN_CONDA_VERSION=24.11.0"
if not defined NVTA_MINIFORGE_HOME set "NVTA_MINIFORGE_HOME=%LOCALAPPDATA%\Miniforge3"
if not defined NVTA_MINIFORGE_RELEASE_API set "NVTA_MINIFORGE_RELEASE_API=https://api.github.com/repos/conda-forge/miniforge/releases/latest"

set "NVTA_SAVED_CONDA_FALLBACK=%NVTA_CONDA_EXE%"
set "NVTA_CONDA_EXE="
set "NVTA_CONDA_KIND="
set "NVTA_CONDA_VERSION="
set "NVTA_MINIFORGE_ACTION="
set "NVTA_MINIFORGE_ONLY=1"
call "%~dp0find_conda.bat" >nul 2>nul
set "NVTA_MINIFORGE_FIND_EXIT=%ERRORLEVEL%"
set "NVTA_MINIFORGE_ONLY="

if not "%NVTA_MINIFORGE_FIND_EXIT%"=="0" goto install_miniforge

set "NVTA_CONDA_VERSION_FILE=%TEMP%\nvta_conda_version_%RANDOM%_%RANDOM%.txt"
call "%NVTA_CONDA_EXE%" --version > "%NVTA_CONDA_VERSION_FILE%" 2>nul
for /f "usebackq tokens=2" %%V in ("%NVTA_CONDA_VERSION_FILE%") do if not defined NVTA_CONDA_VERSION set "NVTA_CONDA_VERSION=%%V"
del /q "%NVTA_CONDA_VERSION_FILE%" >nul 2>nul
set "NVTA_CONDA_VERSION_FILE="

if not defined NVTA_CONDA_VERSION goto install_miniforge
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -Command "try { if ([version]$env:NVTA_CONDA_VERSION -ge [version]$env:NVTA_MINIFORGE_MIN_CONDA_VERSION) { exit 0 } } catch { }; exit 1" >nul 2>nul
if not errorlevel 1 (
    set "NVTA_MINIFORGE_ACTION=Reused compatible Miniforge"
    goto selected_conda
)

echo [INFO] Existing Miniforge Conda %NVTA_CONDA_VERSION% is older than required %NVTA_MINIFORGE_MIN_CONDA_VERSION%; updating it from conda-forge...
call "%NVTA_CONDA_EXE%" update --name base --yes --override-channels --channel "https://conda.anaconda.org/conda-forge" --strict-channel-priority conda
if errorlevel 1 goto install_miniforge

set "NVTA_CONDA_VERSION="
set "NVTA_CONDA_VERSION_FILE=%TEMP%\nvta_conda_version_%RANDOM%_%RANDOM%.txt"
call "%NVTA_CONDA_EXE%" --version > "%NVTA_CONDA_VERSION_FILE%" 2>nul
for /f "usebackq tokens=2" %%V in ("%NVTA_CONDA_VERSION_FILE%") do if not defined NVTA_CONDA_VERSION set "NVTA_CONDA_VERSION=%%V"
del /q "%NVTA_CONDA_VERSION_FILE%" >nul 2>nul
set "NVTA_CONDA_VERSION_FILE="
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -Command "try { if ([version]$env:NVTA_CONDA_VERSION -ge [version]$env:NVTA_MINIFORGE_MIN_CONDA_VERSION) { exit 0 } } catch { }; exit 1" >nul 2>nul
if errorlevel 1 goto install_miniforge
set "NVTA_MINIFORGE_ACTION=Updated existing Miniforge"
goto selected_conda

:install_miniforge
set "NVTA_CONDA_EXE="
set "NVTA_CONDA_KIND="
set "NVTA_CONDA_VERSION="
set "NVTA_MINIFORGE_ASSET_URL="
set "NVTA_MINIFORGE_EXPECTED_SHA256="

if exist "%NVTA_MINIFORGE_HOME%\Scripts\conda.exe" (
    echo [WARN] The managed Miniforge location exists but could not be updated: "%NVTA_MINIFORGE_HOME%"
    goto existing_conda_fallback
)

set "NVTA_MINIFORGE_TEMP=%TEMP%\nvta_miniforge_%RANDOM%_%RANDOM%"
set "NVTA_MINIFORGE_INSTALLER=%NVTA_MINIFORGE_TEMP%\Miniforge3-Windows-x86_64.exe"
set "NVTA_MINIFORGE_RELEASE_FILE=%NVTA_MINIFORGE_TEMP%\latest-release.json"
set "NVTA_MINIFORGE_METADATA_FILE=%NVTA_MINIFORGE_TEMP%\asset-metadata.txt"
if not exist "%NVTA_MINIFORGE_TEMP%" mkdir "%NVTA_MINIFORGE_TEMP%" >nul 2>nul
if not exist "%NVTA_MINIFORGE_TEMP%" (
    echo [WARN] Could not create the user temporary download folder: "%NVTA_MINIFORGE_TEMP%"
    goto existing_conda_fallback
)

if defined NVTA_MINIFORGE_INSTALLER_PATH (
    if not exist "%NVTA_MINIFORGE_INSTALLER_PATH%" (
        echo [WARN] NVTA_MINIFORGE_INSTALLER_PATH does not exist: "%NVTA_MINIFORGE_INSTALLER_PATH%"
        goto download_cleanup_and_fallback
    )
    set "NVTA_MINIFORGE_INSTALLER=%NVTA_MINIFORGE_INSTALLER_PATH%"
    set "NVTA_MINIFORGE_EXPECTED_SHA256=%NVTA_MINIFORGE_INSTALLER_SHA256%"
    goto verify_installer
)

echo [INFO] Downloading official Miniforge release metadata...
where.exe curl.exe >nul 2>nul
if errorlevel 1 goto download_release_metadata_with_windows
curl.exe --fail --location --silent --show-error --retry 8 --retry-all-errors --retry-delay 5 --connect-timeout 30 --output "%NVTA_MINIFORGE_RELEASE_FILE%" "%NVTA_MINIFORGE_RELEASE_API%"
set "NVTA_MINIFORGE_DOWNLOAD_EXIT=%ERRORLEVEL%"
echo [INFO] curl release-metadata exit code: %NVTA_MINIFORGE_DOWNLOAD_EXIT%
if "%NVTA_MINIFORGE_DOWNLOAD_EXIT%"=="0" goto release_metadata_downloaded
echo [WARN] curl could not download release metadata; trying the Windows web client...

:download_release_metadata_with_windows
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -Command "try { Invoke-WebRequest -UseBasicParsing -Uri $env:NVTA_MINIFORGE_RELEASE_API -OutFile $env:NVTA_MINIFORGE_RELEASE_FILE; exit 0 } catch { Write-Error $_; exit 1 }"
set "NVTA_MINIFORGE_DOWNLOAD_EXIT=%ERRORLEVEL%"
if not "%NVTA_MINIFORGE_DOWNLOAD_EXIT%"=="0" goto release_metadata_download_failed

:release_metadata_downloaded

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -Command "$r=Get-Content -Raw -LiteralPath $env:NVTA_MINIFORGE_RELEASE_FILE|ConvertFrom-Json; $a=@($r.assets|Where-Object {$_.name -eq 'Miniforge3-Windows-x86_64.exe'})[0]; if ($null -eq $a -or $a.digest -notmatch '^sha256:[0-9a-fA-F]{64}$') { exit 1 }; Set-Content -LiteralPath $env:NVTA_MINIFORGE_METADATA_FILE -Value @($a.browser_download_url,$a.digest.Substring(7)) -Encoding ASCII" >nul 2>nul
set "NVTA_MINIFORGE_METADATA_EXIT=%ERRORLEVEL%"
if not "%NVTA_MINIFORGE_METADATA_EXIT%"=="0" (
    echo [WARN] Official release metadata did not contain a valid signed Windows x64 asset digest.
    goto download_cleanup_and_fallback
)

set /p "NVTA_MINIFORGE_ASSET_URL=" < "%NVTA_MINIFORGE_METADATA_FILE%"
for /f "usebackq skip=1 delims=" %%H in ("%NVTA_MINIFORGE_METADATA_FILE%") do if not defined NVTA_MINIFORGE_EXPECTED_SHA256 set "NVTA_MINIFORGE_EXPECTED_SHA256=%%H"
if not defined NVTA_MINIFORGE_ASSET_URL goto download_cleanup_and_fallback
if not defined NVTA_MINIFORGE_EXPECTED_SHA256 goto download_cleanup_and_fallback

echo [INFO] Downloading the official signed Miniforge Windows x64 installer...
where.exe curl.exe >nul 2>nul
if errorlevel 1 goto download_miniforge_with_windows
curl.exe --fail --location --silent --show-error --retry 8 --retry-all-errors --retry-delay 5 --connect-timeout 30 --output "%NVTA_MINIFORGE_INSTALLER%" "%NVTA_MINIFORGE_ASSET_URL%"
set "NVTA_MINIFORGE_DOWNLOAD_EXIT=%ERRORLEVEL%"
echo [INFO] curl Miniforge-installer exit code: %NVTA_MINIFORGE_DOWNLOAD_EXIT%
if "%NVTA_MINIFORGE_DOWNLOAD_EXIT%"=="0" goto miniforge_downloaded
echo [WARN] curl could not download Miniforge; trying the Windows web client...

:download_miniforge_with_windows
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -Command "try { Invoke-WebRequest -UseBasicParsing -Uri $env:NVTA_MINIFORGE_ASSET_URL -OutFile $env:NVTA_MINIFORGE_INSTALLER; exit 0 } catch { Write-Error $_; exit 1 }"
set "NVTA_MINIFORGE_DOWNLOAD_EXIT=%ERRORLEVEL%"
if not "%NVTA_MINIFORGE_DOWNLOAD_EXIT%"=="0" goto miniforge_download_failed

:miniforge_downloaded

:verify_installer
if not defined NVTA_MINIFORGE_EXPECTED_SHA256 goto verify_installer_signature
set "NVTA_MINIFORGE_ACTUAL_SHA256="
set "NVTA_MINIFORGE_HASH_OUTPUT=%NVTA_MINIFORGE_TEMP%\installer-sha256.txt"
"%SystemRoot%\System32\certutil.exe" -hashfile "%NVTA_MINIFORGE_INSTALLER%" SHA256 > "%NVTA_MINIFORGE_HASH_OUTPUT%" 2>nul
for /f "usebackq skip=1 tokens=* delims=" %%H in ("%NVTA_MINIFORGE_HASH_OUTPUT%") do if not defined NVTA_MINIFORGE_ACTUAL_SHA256 set "NVTA_MINIFORGE_ACTUAL_SHA256=%%H"
set "NVTA_MINIFORGE_ACTUAL_SHA256=%NVTA_MINIFORGE_ACTUAL_SHA256: =%"
echo [INFO] Expected Miniforge SHA-256: %NVTA_MINIFORGE_EXPECTED_SHA256%
echo [INFO] Downloaded Miniforge SHA-256: %NVTA_MINIFORGE_ACTUAL_SHA256%
if /i not "%NVTA_MINIFORGE_ACTUAL_SHA256%"=="%NVTA_MINIFORGE_EXPECTED_SHA256%" (
    echo [WARN] Miniforge installer SHA-256 verification failed.
    goto download_cleanup_and_fallback
)

:verify_installer_signature
set "NVTA_MINIFORGE_SIGNATURE_FILE=%NVTA_MINIFORGE_INSTALLER%"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -Command "$s=Get-AuthenticodeSignature -LiteralPath $env:NVTA_MINIFORGE_SIGNATURE_FILE; if ($s.Status -eq 'Valid') { exit 0 }; Write-Error ('Miniforge Authenticode status: '+$s.Status); exit 1"
if errorlevel 1 (
    echo [WARN] The Miniforge installer does not have a valid trusted Authenticode signature.
    goto download_cleanup_and_fallback
)

echo [INFO] Installing signed Miniforge for the current user at "%NVTA_MINIFORGE_HOME%"...
start /wait "" "%NVTA_MINIFORGE_INSTALLER%" /InstallationType=JustMe /RegisterPython=0 /AddToPath=0 /S /D=%NVTA_MINIFORGE_HOME%
set "NVTA_MINIFORGE_INSTALL_EXIT=%ERRORLEVEL%"
if not "%NVTA_MINIFORGE_INSTALL_EXIT%"=="0" (
    echo [WARN] The Miniforge installer exited with code %NVTA_MINIFORGE_INSTALL_EXIT%.
    goto download_cleanup_and_fallback
)
if not exist "%NVTA_MINIFORGE_HOME%\Scripts\conda.exe" (
    echo [WARN] Miniforge installation completed without a usable conda.exe.
    goto download_cleanup_and_fallback
)

set "NVTA_CONDA_EXE=%NVTA_MINIFORGE_HOME%\Scripts\conda.exe"
set "NVTA_CONDA_KIND=Miniforge"
set "NVTA_MINIFORGE_ACTION=Installed Miniforge"
set "NVTA_CONDA_VERSION_FILE=%TEMP%\nvta_conda_version_%RANDOM%_%RANDOM%.txt"
call "%NVTA_CONDA_EXE%" --version > "%NVTA_CONDA_VERSION_FILE%" 2>nul
for /f "usebackq tokens=2" %%V in ("%NVTA_CONDA_VERSION_FILE%") do if not defined NVTA_CONDA_VERSION set "NVTA_CONDA_VERSION=%%V"
del /q "%NVTA_CONDA_VERSION_FILE%" >nul 2>nul
set "NVTA_CONDA_VERSION_FILE="
goto download_cleanup_and_selected

:release_metadata_download_failed
echo [WARN] Could not download official Miniforge release metadata.
goto download_cleanup_and_fallback

:miniforge_download_failed
echo [WARN] Could not download the official Miniforge installer.
goto download_cleanup_and_fallback

:download_cleanup_and_fallback
if defined NVTA_MINIFORGE_TEMP if exist "%NVTA_MINIFORGE_TEMP%\latest-release.json" del /q "%NVTA_MINIFORGE_TEMP%\latest-release.json" >nul 2>nul
if defined NVTA_MINIFORGE_TEMP if exist "%NVTA_MINIFORGE_TEMP%\asset-metadata.txt" del /q "%NVTA_MINIFORGE_TEMP%\asset-metadata.txt" >nul 2>nul
if defined NVTA_MINIFORGE_TEMP if exist "%NVTA_MINIFORGE_TEMP%\installer-sha256.txt" del /q "%NVTA_MINIFORGE_TEMP%\installer-sha256.txt" >nul 2>nul
if defined NVTA_MINIFORGE_TEMP if exist "%NVTA_MINIFORGE_TEMP%\Miniforge3-Windows-x86_64.exe" del /q "%NVTA_MINIFORGE_TEMP%\Miniforge3-Windows-x86_64.exe" >nul 2>nul
if defined NVTA_MINIFORGE_TEMP if exist "%NVTA_MINIFORGE_TEMP%" rd "%NVTA_MINIFORGE_TEMP%" >nul 2>nul
goto existing_conda_fallback

:download_cleanup_and_selected
if defined NVTA_MINIFORGE_TEMP if exist "%NVTA_MINIFORGE_TEMP%\latest-release.json" del /q "%NVTA_MINIFORGE_TEMP%\latest-release.json" >nul 2>nul
if defined NVTA_MINIFORGE_TEMP if exist "%NVTA_MINIFORGE_TEMP%\asset-metadata.txt" del /q "%NVTA_MINIFORGE_TEMP%\asset-metadata.txt" >nul 2>nul
if defined NVTA_MINIFORGE_TEMP if exist "%NVTA_MINIFORGE_TEMP%\installer-sha256.txt" del /q "%NVTA_MINIFORGE_TEMP%\installer-sha256.txt" >nul 2>nul
if defined NVTA_MINIFORGE_TEMP if exist "%NVTA_MINIFORGE_TEMP%\Miniforge3-Windows-x86_64.exe" del /q "%NVTA_MINIFORGE_TEMP%\Miniforge3-Windows-x86_64.exe" >nul 2>nul
if defined NVTA_MINIFORGE_TEMP if exist "%NVTA_MINIFORGE_TEMP%" rd "%NVTA_MINIFORGE_TEMP%" >nul 2>nul
goto selected_conda

:existing_conda_fallback
set "NVTA_CONDA_EXE=%NVTA_SAVED_CONDA_FALLBACK%"
set "NVTA_FALLBACK_ONLY=1"
call "%~dp0find_conda.bat"
set "NVTA_FALLBACK_FIND_EXIT=%ERRORLEVEL%"
set "NVTA_FALLBACK_ONLY="
if not "%NVTA_FALLBACK_FIND_EXIT%"=="0" (
    echo [ERROR] Miniforge could not be installed or updated, and no existing Conda fallback is available.
    exit /b 1
)
set "NVTA_CONDA_VERSION_FILE=%TEMP%\nvta_conda_version_%RANDOM%_%RANDOM%.txt"
call "%NVTA_CONDA_EXE%" --version > "%NVTA_CONDA_VERSION_FILE%" 2>nul
for /f "usebackq tokens=2" %%V in ("%NVTA_CONDA_VERSION_FILE%") do if not defined NVTA_CONDA_VERSION set "NVTA_CONDA_VERSION=%%V"
del /q "%NVTA_CONDA_VERSION_FILE%" >nul 2>nul
set "NVTA_CONDA_VERSION_FILE="
set "NVTA_MINIFORGE_ACTION=Used existing %NVTA_CONDA_KIND% after Miniforge failure"

:selected_conda
set "NVTA_SAVED_CONDA_FALLBACK="
set "NVTA_MINIFORGE_FIND_EXIT="
set "NVTA_FALLBACK_FIND_EXIT="
set "NVTA_MINIFORGE_INSTALL_EXIT="
set "NVTA_MINIFORGE_TEMP="
set "NVTA_MINIFORGE_INSTALLER="
set "NVTA_MINIFORGE_RELEASE_FILE="
set "NVTA_MINIFORGE_METADATA_FILE="
set "NVTA_MINIFORGE_ASSET_URL="
set "NVTA_MINIFORGE_EXPECTED_SHA256="
set "NVTA_MINIFORGE_ACTUAL_SHA256="
set "NVTA_MINIFORGE_SIGNATURE_FILE="
set "NVTA_MINIFORGE_DOWNLOAD_EXIT="
set "NVTA_MINIFORGE_METADATA_EXIT="
set "NVTA_MINIFORGE_HASH_OUTPUT="

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
    echo [ERROR] The selected Conda executable could not run: "%NVTA_CONDA_EXE%"
    exit /b 1
)

echo CONDA_EXE=%NVTA_CONDA_EXE%
echo CONDA_KIND=%NVTA_CONDA_KIND%
echo CONDA_VERSION=%NVTA_CONDA_VERSION%
echo MINIFORGE_ACTION=%NVTA_MINIFORGE_ACTION%
exit /b 0
