@echo off
setlocal
set "APP_DIR=%~dp0"
cd /d "%APP_DIR%"
if not exist "%APP_DIR%ClinicalCaptureV2.exe" (
  echo ClinicalCaptureV2.exe not found in this folder.
  pause
  exit /b 1
)
if not exist "%APP_DIR%_internal\python311.dll" (
  echo This folder is an incomplete PyInstaller build and cannot run.
  echo Use the executable from dist\ClinicalCaptureV2 or extract the full ClinicalCaptureV2-portable.zip.
  echo Keep the _internal folder next to ClinicalCaptureV2.exe.
  pause
  exit /b 2
)
start "Clinical Capture V2" "%APP_DIR%ClinicalCaptureV2.exe"
endlocal
