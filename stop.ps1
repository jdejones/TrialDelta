$ErrorActionPreference = 'Stop'
$statePath = Join-Path $PSScriptRoot 'data\processes.json'
if (-not (Test-Path -LiteralPath $statePath)) { Write-Output 'No recorded TrialDelta processes.'; return }
$recorded = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
$expectedPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$stoppedWorkerIds = [System.Collections.Generic.List[int]]::new()
try {
    foreach ($property in $recorded.PSObject.Properties) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($property.Value)" -ErrorAction SilentlyContinue
        if ($process -and $process.ExecutablePath -eq $expectedPython -and $process.CommandLine -match 'trialdelta\.(app:app|worker)') {
            # Windows venv launchers have one application child; stop only matching descendants.
            $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($process.ProcessId)" -ErrorAction SilentlyContinue
            foreach ($child in $children) {
                if ($child.CommandLine -and $child.CommandLine.Contains($expectedPython) -and $child.CommandLine -match 'trialdelta\.(app:app|worker)') {
                    Stop-Process -Id $child.ProcessId -ErrorAction Stop
                    Wait-Process -Id $child.ProcessId -Timeout 5 -ErrorAction SilentlyContinue
                    if (Get-Process -Id $child.ProcessId -ErrorAction SilentlyContinue) {
                        throw 'A TrialDelta child process did not stop; its health status was not cleared.'
                    }
                    if ($child.CommandLine -match 'trialdelta\.worker(?:\s|$)') {
                        $stoppedWorkerIds.Add([int]$child.ProcessId)
                    }
                }
            }
            # A venv launcher can exit as soon as its child stops.
            if (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue) {
                Stop-Process -Id $process.ProcessId -ErrorAction Stop
                Wait-Process -Id $process.ProcessId -Timeout 5 -ErrorAction SilentlyContinue
            }
            if (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue) {
                throw 'A TrialDelta process did not stop; its health status was not cleared.'
            }
            if ($process.CommandLine -match 'trialdelta\.worker(?:\s|$)') {
                $stoppedWorkerIds.Add([int]$process.ProcessId)
            }
            Write-Output "Stopped TrialDelta $($property.Name)."
        }
    }
} finally {
    # Preserve verified intentional stops even if stopping another process fails.
    if ($stoppedWorkerIds.Count -gt 0) {
        # The health module has no database imports and never starts a collector.
        Push-Location -LiteralPath $PSScriptRoot
        try {
            & $expectedPython -m trialdelta.worker_health --mark-stopped @stoppedWorkerIds
            if ($LASTEXITCODE -ne 0) { throw 'Processes stopped, but their health status could not be updated.' }
        } finally {
            Pop-Location
        }
    }
}
Set-Content -LiteralPath $statePath -Value '{}'
