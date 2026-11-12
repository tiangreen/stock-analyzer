# Starts the dashboard server (which also runs the daily schedule).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv")) {
    python -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install -q -e .
}
& .\.venv\Scripts\python.exe -m stock_analyzer.server
