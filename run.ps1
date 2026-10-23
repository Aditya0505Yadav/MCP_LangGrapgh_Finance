# PowerShell launcher:  .\run.ps1
# If scripts are blocked, run once:  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
& $py launch.py
