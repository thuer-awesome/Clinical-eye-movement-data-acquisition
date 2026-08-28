# PyInstaller onedir build for the native PySide6 version 2 workstation.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


ROOT = Path(SPEC).resolve().parents[1]
CONFIG_FILE = ROOT / "configs" / "clinical-capture.json"

# PyInstaller's native PySide6 hooks collect QtCore, QtGui, QtWidgets and the
# required platform plugins from these hidden imports.  Do not use
# ``collect_data_files("PySide6")`` or ``collect_dynamic_libs("PySide6")``:
# both pull optional Qt3D/WebEngine/QML stacks into a Widgets-only workstation.
pyside_hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "shiboken6",
]
pupil_detector_binaries = collect_dynamic_libs("pupil_detectors")
pupil_detector_datas = collect_data_files("pupil_detectors")

hiddenimports = [
    "cv2",
    "apps.clinical_capture.qt_app",
    "apps.clinical_capture.qt_main",
    *collect_submodules("pupil_detectors"),
    *pyside_hiddenimports,
]

a = Analysis(
    [str(ROOT / "apps" / "clinical_capture" / "qt_main.py")],
    pathex=[str(ROOT)],
    binaries=pupil_detector_binaries,
    datas=[(str(CONFIG_FILE), "configs"), *pupil_detector_datas],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "tensorflow", "matplotlib", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets"],
    noarchive=False,
)

# Qt 6 on supported Windows 10/11 systems imports the unversioned ICU API from
# the operating system.  On developer machines with Poppler/Conda on PATH,
# PyInstaller can mistakenly collect a versioned third-party ``icuuc.dll``
# whose exports end in ``_73``/``_78``.  That shadow DLL makes QtCore fail with
# Windows loader error 127 (procedure not found).  Keep it out of the bundle so
# the same system ICU used by the installed PySide6 wheel is loaded.
def _is_external_versioned_icu(entry):
    name = Path(entry[0]).name.lower()
    return name == "icuuc.dll" or (name.startswith("icudt") and name.endswith(".dll"))


a.binaries = [entry for entry in a.binaries if not _is_external_versioned_icu(entry)]
a.datas = [entry for entry in a.datas if not _is_external_versioned_icu(entry)]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    name="ClinicalCaptureV2",
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
    name="ClinicalCaptureV2",
)
