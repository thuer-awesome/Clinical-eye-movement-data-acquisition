from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import os
import logging
import multiprocessing as mp
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import uvicorn


_INSTANCE_MUTEX_HANDLE: int | None = None
_ERROR_ALREADY_EXISTS = 183


def _acquire_instance_mutex(port: int) -> bool:
    """Reserve one workstation instance per TCP port, including startup time."""
    global _INSTANCE_MUTEX_HANDLE
    if os.name != "nt":  # pragma: no cover - the release target is Windows
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.GetLastError.restype = ctypes.c_ulong
        handle = kernel32.CreateMutexW(None, False, f"Local\\ClinicalCapture-{port}")
        if not handle:
            return True
        if int(kernel32.GetLastError()) == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _INSTANCE_MUTEX_HANDLE = int(handle)
        atexit.register(_release_instance_mutex)
        return True
    except Exception:
        # The mutex is a guardrail, not a reason to make an unusual Windows
        # runtime unusable.
        return True


def _release_instance_mutex() -> None:
    global _INSTANCE_MUTEX_HANDLE
    handle = _INSTANCE_MUTEX_HANDLE
    _INSTANCE_MUTEX_HANDLE = None
    if handle and os.name == "nt":
        try:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
        except Exception:
            pass


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    __package__ = "apps.clinical_capture"


def _boot_log(path: Path, message: str) -> None:
    """Small unbuffered startup breadcrumb for console-less Windows builds."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except OSError:
        pass


def _probe_existing_service(host: str, port: int) -> dict[str, object] | None:
    """Return the local health payload when this port already serves us."""
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    request = urllib.request.Request(
        f"http://{probe_host}:{port}/api/health",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=0.45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) and payload.get("service") == "clinical-capture" else None


def _wait_for_existing_service(host: str, port: int, *, timeout_seconds: float = 30.0) -> dict[str, object] | None:
    deadline = time.monotonic() + max(0.5, float(timeout_seconds))
    while time.monotonic() < deadline:
        existing = _probe_existing_service(host, port)
        if existing is not None:
            return existing
        time.sleep(0.25)
    return None


def _safe_open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _open_browser_when_service_ready(host: str, port: int, url: str) -> None:
    """Open the local UI once startup has finished, even on slower clinic PCs."""
    _wait_for_existing_service(host, port, timeout_seconds=30.0)
    _safe_open_browser(url)

from . import __version__
from .server import DEFAULT_CONFIG_PATH, create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clinical high-speed acquisition workstation")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    default_data_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ClinicalCapture" / "data"
    parser.add_argument("--data-dir", type=Path, default=default_data_dir, help="session output directory")
    parser.add_argument("--simulate", action="store_true", help="use visible synthetic camera streams")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8785)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--log-level", default="warning", choices=("critical", "error", "warning", "info", "debug"))
    return parser


def main(argv: list[str] | None = None) -> int:
    # Required for PyInstaller child processes on Windows.  Without this call a
    # frozen worker can re-enter the server entry point instead of the capture
    # target and the parent appears to hang during startup.
    mp.freeze_support()
    args = build_parser().parse_args(argv)
    boot_log = args.data_dir / "clinical_capture.log"
    _boot_log(boot_log, "parsed arguments")
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if not _acquire_instance_mutex(args.port):
        existing = _wait_for_existing_service(args.host, args.port)
        url = f"http://{args.host}:{args.port}/"
        if existing is not None and not args.no_browser:
            _safe_open_browser(url)
        print(f"Clinical Capture is already starting or running at {url}", flush=True)
        return 0
    existing = _probe_existing_service(args.host, args.port)
    if existing is not None:
        url = f"http://{args.host}:{args.port}/"
        _release_instance_mutex()
        if not args.no_browser:
            _safe_open_browser(url)
        print(f"Clinical Capture already running at {url}", flush=True)
        return 0
    try:
        args.data_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=str(args.data_dir / "clinical_capture.log"),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    except OSError:
        logging.basicConfig(level=logging.INFO)
    _boot_log(boot_log, "before create_app")
    app = create_app(args.config, data_dir=args.data_dir, simulate=True if args.simulate else None)
    _boot_log(boot_log, "after create_app")
    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser:
        threading.Thread(
            target=_open_browser_when_service_ready,
            args=(args.host, args.port, url),
            name="clinical-capture-browser",
            daemon=True,
        ).start()
    print(f"Clinical Capture {__version__} listening at {url}", flush=True)
    _boot_log(boot_log, "before uvicorn")
    # ``runw.exe`` gives frozen GUI builds no stdio streams.  Uvicorn's default
    # dictConfig targets sys.stderr/stdout and can stall before binding; the
    # application already writes a local diagnostic log, so disable that setup.
    uvicorn_config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(uvicorn_config)
    app.state.server = server
    try:
        server.run()
    finally:
        _release_instance_mutex()
    _boot_log(boot_log, "after uvicorn")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
