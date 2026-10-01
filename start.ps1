param([switch]$WithWorker, [switch]$ImportNews)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    python -m venv .venv
    & $python -m pip install -r requirements.lock
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot 'data') -Force | Out-Null
$statePath = Join-Path $PSScriptRoot 'data\processes.json'
$processes = @{}
if (Test-Path -LiteralPath $statePath) {
    $old = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    foreach ($property in $old.PSObject.Properties) {
        $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$($property.Value)" -ErrorAction SilentlyContinue
        if ($existing -and $existing.CommandLine -like '*trialdelta*') { $processes[$property.Name] = $existing.ProcessId }
    }
}
if (-not $processes.ContainsKey('server')) {
    & $python -c "from trialdelta.app import app; from trialdelta.db import initialize; from trialdelta.auth import bootstrap; initialize(); bootstrap()"
    if ($LASTEXITCODE -ne 0) { throw 'Database initialization failed; no background processes started.' }
    $server = Start-Process -FilePath $python -ArgumentList '-m','uvicorn','trialdelta.app:app','--host','127.0.0.1','--port','8765','--no-proxy-headers' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput 'data\server.log' -RedirectStandardError 'data\server-error.log' -PassThru
    $processes['server'] = $server.Id
}
if ($WithWorker -and -not $processes.ContainsKey('worker')) {
    $workerArgs = @('-m','trialdelta.worker')
    if ($ImportNews) { $workerArgs += '--import-news' }
    $worker = Start-Process -FilePath $python -ArgumentList $workerArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput 'data\worker.log' -RedirectStandardError 'data\worker-error.log' -PassThru
    $processes['worker'] = $worker.Id
}
$processes | ConvertTo-Json | Set-Content -LiteralPath $statePath
$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 2
        if ($health.status -eq 'ok') { $ready = $true; break }
    } catch { Start-Sleep -Seconds 1 }
}
if (-not $ready) { throw 'TrialDelta did not become ready. See data/server-error.log.' }
Write-Output 'TrialDelta is ready at http://127.0.0.1:8765'
Write-Output 'Run .\stop.ps1 to stop only the processes recorded by this launcher.'
