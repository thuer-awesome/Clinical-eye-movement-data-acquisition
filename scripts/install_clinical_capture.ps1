param(
  [string]$PythonLauncher = "py"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $Root

if (-not (Get-Command $PythonLauncher -ErrorAction SilentlyContinue)) {
  throw "Python 3.11 is required. Install Python and make py/python available first."
}
if (-not (Test-Path -LiteralPath (Join-Path $Root ".venv\Scripts\python.exe"))) {
  if ($PythonLauncher -eq "py") {
    & $PythonLauncher -3.11 -m venv .venv
  } else {
    & $PythonLauncher -m venv .venv
  }
  if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed" }
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements-clinical-capture.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
& $PSScriptRoot\build_clinical_capture.ps1 -SkipInstall
