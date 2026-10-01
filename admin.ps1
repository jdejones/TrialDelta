$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'The project virtual environment is missing. Run .\start.ps1 once to install dependencies, then launch .\admin.ps1.'
}
& $python -m trialdelta.admin_app
if ($LASTEXITCODE -ne 0) {
    throw 'The administrator app could not start. Verify Python includes Tkinter and the database configuration is available.'
}
