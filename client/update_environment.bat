@echo off
setlocal

if not defined NVTA_CONDA_ENV set "NVTA_CONDA_ENV=dtalite_pipeline"
if not defined NVTA_PYPI_INDEX_URL set "NVTA_PYPI_INDEX_URL=https://pypi.org/simple"
if not defined NVTA_WORKFLOW_REQUIREMENT set "NVTA_WORKFLOW_REQUIREMENT=nvta-taplite-workflow"

call "%~dp0setup\find_conda.bat"
if errorlevel 1 exit /b 1

echo [INFO] Updating "%NVTA_WORKFLOW_REQUIREMENT%" in "%NVTA_CONDA_ENV%"...
call "%NVTA_CONDA_EXE%" run --no-capture-output --name "%NVTA_CONDA_ENV%" python -m pip install --disable-pip-version-check --index-url "%NVTA_PYPI_INDEX_URL%" --upgrade --pre --only-binary=:all: "%NVTA_WORKFLOW_REQUIREMENT%"
if errorlevel 1 exit /b 1

call "%NVTA_CONDA_EXE%" run --no-capture-output --name "%NVTA_CONDA_ENV%" python -m nvta_taplite_workflow doctor
exit /b %ERRORLEVEL%

