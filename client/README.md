# Permanent NVTA client launchers

These files contain no workflow implementation. Run `setup_environment.bat`
once, then use either the Python or batch launchers with the documented command
arguments. Future workflow and resource releases are installed by
`update_environment.bat` or by rerunning setup.

The client folder may be placed on a local, mapped, or UNC network path. Logs
from assignment runs are written under the selected output's `logs` folder,
with the caller's `logs` folder used only as a fallback.

## Python launchers

From this folder, with the environment active:

```powershell
python run_assignment.py --scenario-dir "C:\path\to\scenario"
python run_postprocessing.py "C:\path\to\scenario"
```

Without activation, use your Miniforge `conda.exe`:

```powershell
conda run --no-capture-output -n dtalite_pipeline python run_assignment.py --scenario-dir "C:\path\to\scenario"
conda run --no-capture-output -n dtalite_pipeline python run_postprocessing.py "C:\path\to\scenario"
```

## Batch launchers

These locate Miniforge and call the same installed Python entry points:

```bat
run_assignment.bat --scenario-dir "C:\path\to\scenario"
run_postprocessing.bat "C:\path\to\scenario"
```
