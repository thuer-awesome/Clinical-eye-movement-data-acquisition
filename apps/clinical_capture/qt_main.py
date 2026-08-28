"""Executable entry point for the native Clinical Capture V2 workstation."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    __package__ = "apps.clinical_capture"

from .qt_app import main


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
