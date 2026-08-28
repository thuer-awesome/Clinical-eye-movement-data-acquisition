@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0ClinicalCaptureV2.exe" (
  echo ClinicalCaptureV2.exe not found in this folder.
  pause
  exit /b 1
)
start "Clinical Capture V2" "%~dp0ClinicalCaptureV2.exe"
endlocal
