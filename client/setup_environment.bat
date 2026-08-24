@echo off
setlocal

pushd "%~dp0" >nul
if errorlevel 1 (
    echo [ERROR] Could not open the client folder: "%~dp0"
    exit /b 1
)

if not defined NVTA_CONDA_ENV set "NVTA_CONDA_ENV=dtalite_pipeline"
if not defined NVTA_CONDA_CHANNEL set "NVTA_CONDA_CHANNEL=https://conda.anaconda.org/conda-forge"
if not defined NVTA_CONDA_REPODATA set "NVTA_CONDA_REPODATA=current_repodata.json"
if not defined NVTA_PYPI_INDEX_URL set "NVTA_PYPI_INDEX_URL=https://pypi.org/simple"
if not defined NVTA_MINIFORGE_MIN_CONDA_VERSION set "NVTA_MINIFORGE_MIN_CONDA_VERSION=24.11.0"
if not defined NVTA_WORKFLOW_REQUIREMENT set "NVTA_WORKFLOW_REQUIREMENT=nvta-taplite-workflow"

set "ENV_NAME=%NVTA_CONDA_ENV%"
if defined NVTA_SETUP_LOG_DIR (
    set "LOG_DIR=%NVTA_SETUP_LOG_DIR%"
) else (
    set "LOG_DIR=%~dp0logs"
)
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>nul
if not exist "%LOG_DIR%" (
    echo [ERROR] Setup log folder is not writable: "%LOG_DIR%"
    popd
    exit /b 1
)
set "LOG_FILE=%LOG_DIR%\setup_environment_log.txt"
set "EXIT_CODE=1"

echo ========================================== > "%LOG_FILE%"
echo NVTA TAPLite Package Environment Setup >> "%LOG_FILE%"
echo Started at: %date% %time% >> "%LOG_FILE%"
echo Client folder: "%CD%" >> "%LOG_FILE%"
echo Conda environment: "%ENV_NAME%" >> "%LOG_FILE%"
echo Package requirement: "%NVTA_WORKFLOW_REQUIREMENT%" >> "%LOG_FILE%"
echo ========================================== >> "%LOG_FILE%"

echo [INFO] Ensuring a compatible Miniforge installation is available...
call "%~dp0setup\ensure_miniforge.bat" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [ERROR] Miniforge setup failed. See "%LOG_FILE%".
    goto :finish
)
echo [INFO] Using Conda: "%NVTA_CONDA_EXE%"

set "CONDA_ENV_EXISTS=0"
for /f "tokens=1" %%E in ('call "%NVTA_CONDA_EXE%" env list 2^>nul') do if /I "%%E"=="%ENV_NAME%" set "CONDA_ENV_EXISTS=1"

if "%CONDA_ENV_EXISTS%"=="1" (
    echo [INFO] Removing the existing "%ENV_NAME%" environment for a clean refresh...
    call "%NVTA_CONDA_EXE%" env remove --name "%ENV_NAME%" --yes >> "%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo [ERROR] Could not remove "%ENV_NAME%". Close programs using it and retry.
        goto :finish
    )
)

echo [INFO] Creating a fresh "%ENV_NAME%" environment from conda-forge...
call "%NVTA_CONDA_EXE%" create --name "%ENV_NAME%" --yes --override-channels --channel "%NVTA_CONDA_CHANNEL%" --strict-channel-priority --repodata-fn "%NVTA_CONDA_REPODATA%" --no-default-packages --file "%~dp0setup\conda_requirements.txt" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [ERROR] Conda environment creation failed. See "%LOG_FILE%".
    goto :finish
)

echo [INFO] Installing the NVTA workflow package and its pinned dependencies from PyPI...
call "%NVTA_CONDA_EXE%" run --no-capture-output --name "%ENV_NAME%" python -m pip install --disable-pip-version-check --index-url "%NVTA_PYPI_INDEX_URL%" --upgrade --pre --only-binary=:all: "%NVTA_WORKFLOW_REQUIREMENT%" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [ERROR] Workflow package installation failed. See "%LOG_FILE%".
    goto :finish
)

echo [INFO] Verifying the installed package, resources, and TAPLite engine...
call "%NVTA_CONDA_EXE%" run --no-capture-output --name "%ENV_NAME%" python -m nvta_taplite_workflow doctor >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [ERROR] Installed workflow verification failed. See "%LOG_FILE%".
    goto :finish
)

echo [OK] Environment setup completed successfully.
echo [OK] Environment setup completed successfully. >> "%LOG_FILE%"
set "EXIT_CODE=0"

:finish
popd
if not defined NVTA_NO_PAUSE pause
exit /b %EXIT_CODE%

