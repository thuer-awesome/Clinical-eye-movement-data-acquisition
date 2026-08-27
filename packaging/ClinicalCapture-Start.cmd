@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0ClinicalCapture.exe" (
  echo ClinicalCapture.exe not found in this folder.
  pause
  exit /b 1
)
start "Clinical Capture" "%~dp0ClinicalCapture.exe"
endlocal
