# Starts the dashboard server (which also runs the daily schedule).
# .\run.ps1 stop (or -Stop) stops the app that is already running.
param([string]$Command = "", [switch]$Stop)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if ($Stop -or $Command -in @("stop", "--stop")) {
    if (-not (Test-Path ".venv")) { Write-Output "Stock Analyzer is not installed yet, so it is not running."; exit 0 }
    & .\.venv\Scripts\python.exe -m stock_analyzer.server --stop
    exit $LASTEXITCODE
}
if ($Command) { Write-Error "Unknown argument '$Command'. Use: .\run.ps1 [stop]" }
if (-not (Test-Path ".venv")) {
    python -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install -q -e .
}
& .\.venv\Scripts\python.exe -m stock_analyzer.server
