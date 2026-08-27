# PyInstaller onedir build.  Keeping the UI as data makes it editable without
# introducing a Node runtime on the clinical workstation.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


ROOT = Path(SPEC).resolve().parents[1]
UI_DIR = ROOT / "apps" / "clinical_capture" / "ui"
CONFIG_FILE = ROOT / "configs" / "clinical-capture.json"

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "cv2",
    *collect_submodules("pupil_detectors"),
    *collect_submodules("apps.clinical_capture"),
]

pupil_detector_binaries = collect_dynamic_libs("pupil_detectors")
pupil_detector_datas = collect_data_files("pupil_detectors")

a = Analysis(
    [str(ROOT / "apps" / "clinical_capture" / "main.py")],
    pathex=[str(ROOT)],
    binaries=pupil_detector_binaries,
    datas=[(str(UI_DIR), "apps/clinical_capture/ui"), (str(CONFIG_FILE), "configs"), *pupil_detector_datas],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "tensorflow", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    name="ClinicalCapture",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    exclude_binaries=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ClinicalCapture",
)
