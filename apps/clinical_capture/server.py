from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Mapping

try:  # Keep module import informative when dependencies are not installed yet.
    from fastapi import Request
except ImportError:  # pragma: no cover
    Request = Any  # type: ignore[misc,assignment]

from . import __version__
from .camera_settings import (
    FRAME_TRANSFORMS,
    MAX_CAMERA_INDEX,
    apply_camera_settings,
    load_camera_settings,
    save_camera_settings,
    settings_document,
    settings_path_for,
)
from .manager import ClinicalCaptureManager
from .models import ClinicalConfig, SessionMetadata


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "clinical-capture.json"
UI_ROOT = Path(__file__).resolve().parent / "ui"


def load_config(
    config_path: str | Path | None = None,
    *,
    data_dir: str | Path | None = None,
    simulate: bool | None = None,
) -> ClinicalConfig:
    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    if path.exists():
        return ClinicalConfig.from_json(path, data_dir=data_dir, simulate=simulate)
    return ClinicalConfig.defaults(data_dir=data_dir, simulate=bool(simulate))


def create_app(
    config_path: str | Path | None = None,
    *,
    data_dir: str | Path | None = None,
    simulate: bool | None = None,
    autostart: bool = True,
):
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import FileResponse, Response
        from fastapi.staticfiles import StaticFiles
        from starlette.concurrency import run_in_threadpool
    except ImportError as error:  # pragma: no cover - exercised only in a bare Python install
        raise RuntimeError("FastAPI is required. Install requirements-clinical-capture.txt") from error

    base_config = load_config(config_path, data_dir=data_dir, simulate=simulate)
    camera_settings_path = settings_path_for(base_config.data_dir)
    config, settings_confirmed, settings_error = load_camera_settings(base_config, camera_settings_path)
    if config.simulate:
        settings_confirmed = True
    manager = ClinicalCaptureManager(
        config,
        device_settings_confirmed=settings_confirmed,
        device_settings_error=settings_error,
    )

    @asynccontextmanager
    async def lifespan(_app: Any):
        if autostart:
            await run_in_threadpool(manager.start)
        try:
            yield
        finally:
            await run_in_threadpool(manager.close)

    app = FastAPI(title="Clinical Capture", version=__version__, lifespan=lifespan)
    app.state.manager = manager
    app.state.camera_settings_path = camera_settings_path

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        status = manager.status()
        return {
            "ok": True,
            "service": "clinical-capture",
            "version": __version__,
            "state": status["app_state"],
            "simulate": status["simulate"],
        }

    @app.get("/api/config")
    def public_config() -> dict[str, Any]:
        return {"version": __version__, "config": manager.config.public_dict()}

    def device_settings_payload() -> dict[str, Any]:
        payload = settings_document(manager.config)
        payload.update(
            {
                "confirmed": manager.device_settings_confirmed,
                "saved": camera_settings_path.exists(),
                "path": str(camera_settings_path),
                "load_error": manager.device_settings_error,
                "max_camera_index": MAX_CAMERA_INDEX,
                "frame_transforms": list(FRAME_TRANSFORMS),
            }
        )
        return payload

    def binding_issues(status_payload: Mapping[str, Any]) -> list[str]:
        preflight_payload = status_payload.get("preflight")
        if not isinstance(preflight_payload, Mapping):
            return ["未收到相机预检状态"]
        cameras_payload = preflight_payload.get("cameras")
        if not isinstance(cameras_payload, Mapping):
            return ["未收到双眼相机状态"]
        issues: list[str] = []
        for role in ("right_eye", "left_eye"):
            item = cameras_payload.get(role)
            if not isinstance(item, Mapping):
                issues.append(f"{role}: 未收到状态")
                continue
            if item.get("state") not in {"ready", "recording"}:
                issues.append(f"{role}: {item.get('error') or '相机未就绪'}")
                continue
            fps = float(item.get("capture_fps") or 0.0)
            minimum = float(item.get("minimum_fps") or 0.0)
            if fps <= 0 or (minimum > 0 and fps < minimum):
                issues.append(f"{role}: 帧率 {fps:.1f} 低于 {minimum:.1f}")
            age_ms = item.get("last_frame_age_ms")
            if isinstance(age_ms, (int, float)) and float(age_ms) > manager.config.stale_frame_timeout_seconds * 1000:
                issues.append(f"{role}: 帧流超时")
            if not manager.config.simulate and not bool(item.get("transform_verified")):
                issues.append(f"{role}: 画面方向尚未通过首帧验证")
        return issues

    @app.get("/api/device-settings")
    def device_settings() -> dict[str, Any]:
        return device_settings_payload()

    @app.post("/api/cameras/scan")
    async def scan_cameras(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        max_index = payload.get("max_index", 3) if isinstance(payload, Mapping) else 3
        if isinstance(max_index, bool) or not isinstance(max_index, int) or not 0 <= max_index <= 7:
            raise HTTPException(status_code=422, detail="max_index must be an integer between 0 and 7")
        try:
            result = await run_in_threadpool(manager.scan_cameras, max_index=max_index)
            return {"ok": True, **result}
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.put("/api/device-settings")
    async def update_device_settings(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
            if not isinstance(payload, Mapping):
                raise ValueError("camera settings must be a JSON object")
            previous_config = manager.config
            previous_confirmed = manager.device_settings_confirmed
            previous_error = manager.device_settings_error
            candidate = apply_camera_settings(previous_config, payload)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        try:
            status_payload = await run_in_threadpool(
                manager.reconfigure,
                candidate,
                device_settings_confirmed=True,
                device_settings_error=None,
            )
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        issues = binding_issues(status_payload)
        if issues:
            await run_in_threadpool(
                manager.reconfigure,
                previous_config,
                device_settings_confirmed=previous_confirmed,
                device_settings_error=previous_error,
            )
            raise HTTPException(status_code=409, detail=f"新设置未通过检查，已恢复原设置：{'; '.join(issues)}")
        try:
            await run_in_threadpool(save_camera_settings, camera_settings_path, candidate)
        except OSError as error:
            await run_in_threadpool(
                manager.reconfigure,
                previous_config,
                device_settings_confirmed=previous_confirmed,
                device_settings_error=previous_error,
            )
            raise HTTPException(status_code=500, detail=f"相机设置保存失败，已恢复原设置: {error}") from error
        return {"ok": True, "settings": device_settings_payload(), "status": status_payload}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return manager.status()

    @app.post("/api/preflight")
    async def preflight() -> dict[str, Any]:
        return await run_in_threadpool(manager.preflight)

    @app.post("/api/restart")
    async def restart() -> dict[str, Any]:
        return await run_in_threadpool(manager.restart)

    @app.post("/api/shutdown")
    async def shutdown() -> dict[str, Any]:
        """Stop capture workers and request a clean local server shutdown."""
        await run_in_threadpool(manager.close)
        server = getattr(app.state, "server", None)
        if server is not None:
            server.should_exit = True
        return {"ok": True, "state": manager.status()["app_state"]}

    @app.post("/api/session/start")
    async def start_session(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
            metadata = SessionMetadata.from_mapping(payload)
            record = await run_in_threadpool(manager.start_session, metadata)
            return {"ok": True, "session": record.public_dict()}
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/session/stop")
    async def stop_session(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        reason = payload.get("reason") if isinstance(payload, Mapping) else None
        if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
            raise HTTPException(status_code=422, detail="reason must be at most 500 characters")
        record = await run_in_threadpool(manager.stop_session, reason or None)
        if record is None:
            raise HTTPException(status_code=409, detail="没有正在录制的会话")
        return {"ok": True, "session": record.public_dict()}

    @app.get("/api/session/current")
    def current_session() -> dict[str, Any]:
        return {"session": manager.status().get("session"), "last_session": manager.status().get("last_session")}

    @app.get("/api/sessions")
    def sessions() -> dict[str, Any]:
        return {"items": manager.sessions()}

    @app.get("/api/session/{session_id}/export")
    async def export_session(session_id: str):
        try:
            path = await run_in_threadpool(manager.export_session, session_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return FileResponse(path, media_type="application/zip", filename=path.name)

    @app.get("/api/session/{session_id}/export-info")
    async def export_info(session_id: str) -> dict[str, Any]:
        """Create the ZIP and return its absolute local path for the operator."""
        try:
            path = await run_in_threadpool(manager.export_session, session_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        try:
            size = path.stat().st_size
        except OSError as error:
            raise HTTPException(status_code=500, detail=f"导出文件不可读: {error}") from error
        return {
            "ok": True,
            "session_id": session_id,
            "filename": path.name,
            "path": str(path),
            "size_bytes": int(size),
            "download_url": f"/api/session/{session_id}/export",
        }

    @app.get("/api/session/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        try:
            return {"session": manager.get_session(session_id).public_dict()}
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/session/{session_id}/screening")
    async def save_screening(session_id: str, request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
            if not isinstance(payload, Mapping):
                raise ValueError("screening payload must be an object")
            result = await run_in_threadpool(manager.save_screening, session_id, payload)
            return {"ok": True, "result": result}
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/preview/{camera_name}.jpg")
    def preview(camera_name: str):
        image = manager.preview_jpeg(camera_name)
        if image is None:
            raise HTTPException(status_code=404, detail="preview unavailable")
        return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    if UI_ROOT.exists():
        app.mount("/", StaticFiles(directory=UI_ROOT, html=True), name="ui")

    return app

def get_app():
    """Return an app instance for ASGI tooling without allocating it at import time."""
    return create_app(autostart=False)
