param(
  [string]$PythonLauncher = "py"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $Root

if (-not (Get-Command $PythonLauncher -ErrorAction SilentlyContinue)) {
  throw "Python 3.11 is required. Install Python and make py/python available first."
}
$VenvPython = Join-Path $Root ".venv-v2\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
  if ($PythonLauncher -eq "py") {
    & $PythonLauncher -3.11 -m venv .venv-v2
  } else {
    & $PythonLauncher -m venv .venv-v2
  }
  if ($LASTEXITCODE -ne 0) { throw "V2 virtual environment creation failed" }
}
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r requirements-clinical-capture-v2.txt
if ($LASTEXITCODE -ne 0) { throw "V2 dependency installation failed" }
Write-Host "V2 environment ready: $VenvPython"
Write-Host "Run scripts\build_clinical_capture_v2.ps1 to create the portable bundle."
