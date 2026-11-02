# Start the AlphaForge offline API (127.0.0.1:8000) and the testing dashboard (127.0.0.1:8501).
# Windows PowerShell 5.1+. Uses `uv run python -m ...` so blocked .exe launchers are not needed.
#
#   powershell -ExecutionPolicy Bypass -File scripts\start_dashboard.ps1 [-Ingest]
#
# -Ingest populates the local database from the recorded offline fixtures first (idempotent):
# events (real SEC/Fed/BLS/BEA snapshots) and market data (SYNTHETIC prices, real ALFRED yields).
param([switch]$Ingest)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

uv sync
uv run python -m finance_agent init-db
if ($Ingest) {
    uv run python -m finance_agent events ingest --offline
    uv run python -m finance_agent market ingest --start 2024-01-02 --end 2026-10-30 --offline
}

$api = Start-Process -PassThru -NoNewWindow uv -ArgumentList @(
    "run", "python", "-m", "finance_agent", "serve", "--offline", "--host", "127.0.0.1", "--port", "8000"
)
Write-Host "Offline API started (pid $($api.Id)) on http://127.0.0.1:8000"
try {
    uv run python -m finance_agent.ui
}
finally {
    taskkill /PID $api.Id /T /F | Out-Null  # uv + its python child
}
