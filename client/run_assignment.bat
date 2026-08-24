@echo off
setlocal

if not defined NVTA_CONDA_ENV set "NVTA_CONDA_ENV=dtalite_pipeline"
call "%~dp0setup\find_conda.bat"
if errorlevel 1 exit /b 1

call "%NVTA_CONDA_EXE%" run --no-capture-output --name "%NVTA_CONDA_ENV%" python -m nvta_taplite_workflow assignment %*
exit /b %ERRORLEVEL%

