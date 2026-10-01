$ErrorActionPreference = 'Stop'
$statePath = Join-Path $PSScriptRoot 'data\processes.json'
if (-not (Test-Path -LiteralPath $statePath)) { Write-Output 'No recorded TrialDelta processes.'; return }
$recorded = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
foreach ($property in $recorded.PSObject.Properties) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($property.Value)" -ErrorAction SilentlyContinue
    $expectedPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if ($process -and $process.ExecutablePath -eq $expectedPython -and $process.CommandLine -match 'trialdelta\.(app:app|worker)') {
        # Windows venv launchers have one application child; stop only matching descendants.
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($process.ProcessId)" -ErrorAction SilentlyContinue
        foreach ($child in $children) {
            if ($child.CommandLine -and $child.CommandLine.Contains($expectedPython) -and $child.CommandLine -match 'trialdelta\.(app:app|worker)') {
                Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
            }
        }
        Stop-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
        Write-Output "Stopped TrialDelta $($property.Name)."
    }
}
Set-Content -LiteralPath $statePath -Value '{}'
