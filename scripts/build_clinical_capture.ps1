param(
  [switch]$SkipInstall,
  [switch]$BuildInstaller,
  [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = if ($PythonPath) { $PythonPath } else { Join-Path $Root ".venv\Scripts\python.exe" }
if (-not (Test-Path -LiteralPath $Python)) {
  throw "Python executable not found: $Python. Run scripts\install_clinical_capture.ps1 or pass -PythonPath."
}

Set-Location -LiteralPath $Root
if (-not $SkipInstall) {
  & $Python -m pip install -r requirements-clinical-capture.txt
  if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
}

& $Python -m PyInstaller --noconfirm --clean packaging\clinical_capture.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

$Executable = Join-Path $Root "dist\ClinicalCapture\ClinicalCapture.exe"
if (-not (Test-Path -LiteralPath $Executable)) { throw "Build completed but executable was not found: $Executable" }
Write-Host "Build output: $Executable"
$StartScript = Join-Path $Root "dist\ClinicalCapture\ClinicalCapture-Start.cmd"
Copy-Item -LiteralPath (Join-Path $Root "packaging\ClinicalCapture-Start.cmd") -Destination $StartScript -Force
Copy-Item -LiteralPath (Join-Path $Root "packaging\ClinicalCapture-README.txt") -Destination (Join-Path $Root "dist\ClinicalCapture\ClinicalCapture-README.txt") -Force
$PortableZip = Join-Path $Root "dist\ClinicalCapture-portable.zip"
Compress-Archive -Path (Join-Path $Root "dist\ClinicalCapture\*") -DestinationPath $PortableZip -Force
Write-Host "Portable package: $PortableZip"

if ($BuildInstaller) {
  $Iscc = Get-Command iscc -ErrorAction SilentlyContinue
  if ($null -eq $Iscc) {
    throw "Inno Setup iscc.exe was not found. Install Inno Setup and rerun with -BuildInstaller."
  }
  & $Iscc.Source (Join-Path $Root "installer\ClinicalCapture.iss")
  if ($LASTEXITCODE -ne 0) { throw "Inno Setup build failed" }
  Write-Host "Installer output: $Root\dist\installer"
}
