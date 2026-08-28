param(
  [switch]$SkipInstall,
  [switch]$BuildInstaller,
  [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = if ($PythonPath) { $PythonPath } else { Join-Path $Root ".venv-v2\Scripts\python.exe" }
if (-not (Test-Path -LiteralPath $Python)) {
  throw "Python executable not found: $Python. Run scripts\install_clinical_capture_v2.ps1 or pass -PythonPath."
}

Set-Location -LiteralPath $Root
if (-not $SkipInstall) {
  & $Python -m pip install -r requirements-clinical-capture-v2.txt
  if ($LASTEXITCODE -ne 0) { throw "V2 dependency installation failed" }
}

& $Python -m PyInstaller --noconfirm --clean packaging\clinical_capture_v2.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller V2 build failed" }

$Bundle = Join-Path $Root "dist\ClinicalCaptureV2"
$Executable = Join-Path $Bundle "ClinicalCaptureV2.exe"
if (-not (Test-Path -LiteralPath $Executable)) { throw "Build completed but executable was not found: $Executable" }
Copy-Item -LiteralPath (Join-Path $Root "packaging\ClinicalCaptureV2-Start.cmd") -Destination (Join-Path $Bundle "ClinicalCaptureV2-Start.cmd") -Force
Copy-Item -LiteralPath (Join-Path $Root "packaging\ClinicalCaptureV2-README.txt") -Destination (Join-Path $Bundle "ClinicalCaptureV2-README.txt") -Force
$PortableZip = Join-Path $Root "dist\ClinicalCaptureV2-portable.zip"
Compress-Archive -Path (Join-Path $Bundle "*") -DestinationPath $PortableZip -Force
Write-Host "V2 build output: $Executable"
Write-Host "V2 portable package: $PortableZip"

if ($BuildInstaller) {
  $Iscc = Get-Command iscc -ErrorAction SilentlyContinue
  if ($null -eq $Iscc) {
    throw "Inno Setup iscc.exe was not found. Install Inno Setup and rerun with -BuildInstaller."
  }
  & $Iscc.Source (Join-Path $Root "installer\ClinicalCaptureV2.iss")
  if ($LASTEXITCODE -ne 0) { throw "Inno Setup V2 build failed" }
  Write-Host "V2 installer output: $Root\dist\installer"
}
