from __future__ import annotations

import multiprocessing as mp
from pathlib import Path
import queue
import threading
import time
from typing import Any, Mapping

import cv2

from apps.multicamera_capture.shared_frame import FrameSnapshot, SharedFrameOwner

from .camera_settings import probe_camera_indices
from .models import CameraConfig, ClinicalConfig, SessionMetadata, SessionRecord, SessionState, utc_now
from .pupil import PupilDetectorService
from .storage import SessionStore
from .worker import capture_worker, set_shared_path


class ClinicalCaptureManager:
    """Owns camera processes and the bounded clinical-session state machine."""

    def __init__(
        self,
        config: ClinicalConfig,
        *,
        device_settings_confirmed: bool = True,
        device_settings_error: str | None = None,
    ) -> None:
        self.config = config.validate()
        self.device_settings_confirmed = bool(device_settings_confirmed)
        self.device_settings_error = device_settings_error
        self.store = SessionStore(self.config.data_dir)
        self._context = mp.get_context("spawn")
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._processes: dict[str, Any] = {}
        self._owners: dict[str, SharedFrameOwner] = {}
        self._pupil_services: dict[str, PupilDetectorService] = {}
        self._status_queue: Any | None = None
        self._stop_event: Any | None = None
        self._record_event: Any | None = None
        self._record_directory_state: Any | None = None
        # Shared command generation prevents a worker that is still unwinding
        # an old recording from treating a newly-set event as that old command.
        self._record_generation_state: Any | None = None
        self._active_record_generation: int | None = None
        self._stopping_record_generation: int | None = None
        self._recording_expected: set[str] = set()
        self._recording_stop_pending: set[str] = set()
        self._recording_stop_baseline: dict[str, int] = {}
        self._state = SessionState.IDLE
        self._last_error: str | None = None
        self._started_monotonic = time.monotonic()
        self._session: SessionRecord | None = None
        self._last_session: SessionRecord | None = None
        self._fault_thread: threading.Thread | None = None
        self._fault_handling = False
        self._cameras: dict[str, dict[str, Any]] = {
            camera.name: self._new_camera_status(camera) for camera in self.config.cameras
        }
        self._init_runtime_resources()

    def _init_runtime_resources(self) -> None:
        self._status_queue = self._context.Queue(maxsize=2048)
        self._stop_event = self._context.Event()
        self._record_event = self._context.Event()
        self._record_directory_state = self._context.Array("c", 2048, lock=True)
        self._record_generation_state = self._context.Value("q", 0, lock=True)
        self._active_record_generation = None
        self._stopping_record_generation = None
        self._recording_expected = set()
        self._recording_stop_pending = set()
        self._recording_stop_baseline = {}
        self._owners = {
            camera.name: SharedFrameOwner(self._context, camera.frame_shape)
            for camera in self.config.cameras
        }
        self._pupil_services = {
            camera.name: PupilDetectorService(
                camera.name,
                0 if camera.name == "right_eye" else 1 if camera.name == "left_eye" else index,
                self._owners[camera.name],
                detector_fps=self.config.pupil_detector_fps,
                confidence_threshold=self.config.pupil_confidence_threshold,
                alignment_hold_seconds=self.config.pupil_alignment_hold_seconds,
                alignment_center_tolerance=self.config.pupil_alignment_center_tolerance,
                alignment_max_age_seconds=self.config.pupil_alignment_max_age_seconds,
                candidate_min_y_ratio=self.config.pupil_candidate_min_y_ratio,
                candidate_max_y_ratio=self.config.pupil_candidate_max_y_ratio,
                candidate_max_axis_ratio=self.config.pupil_candidate_max_axis_ratio,
                candidate_max_jump_ratio=self.config.pupil_candidate_max_jump_ratio,
                candidate_min_diameter_ratio=self.config.pupil_candidate_min_diameter_ratio,
                candidate_stability_frames=self.config.pupil_candidate_stability_frames,
            )
            for index, camera in enumerate(self.config.cameras)
        }
        self._processes = {}

    def _advance_record_generation(self) -> int:
        """Atomically invalidate the previous recording command.

        A generation is intentionally separate from the recording directory:
        clearing/setting a process-shared Event alone leaves a small window in
        which a worker can observe an old ``True`` value and open its writer
        after the manager has moved on to another session.
        """
        state = self._record_generation_state
        if state is None:
            return 0
        try:
            with state.get_lock():
                state.value = int(state.value) + 1
                return int(state.value)
        except (AttributeError, TypeError, ValueError, OSError):
            # Keep compatibility with lightweight test doubles and older
            # callers that do not provide a shared generation Value.
            try:
                state.value = int(getattr(state, "value", 0)) + 1
                return int(state.value)
            except (AttributeError, TypeError, ValueError, OSError):
                return 0

    @staticmethod
    def _payload_generation(payload: Mapping[str, Any]) -> int | None:
        """Parse an optional worker recording generation field."""
        if "recording_generation" not in payload:
            return None
        value = payload.get("recording_generation")
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return None

    def _recording_payload_matches_active_locked(self, payload: Mapping[str, Any]) -> bool:
        """Return whether a recording status belongs to the live command.

        Payloads from older workers omit the generation. They remain accepted
        only when this manager was created without a generation control (for
        example a lightweight legacy test double); a live manager always
        provides the control to its workers, so accepting a missing token
        would re-open the stale-ACK race this guard is meant to close.
        """
        generation = self._payload_generation(payload)
        if generation is None:
            return self._record_generation_state is None
        return (
            self._active_record_generation is not None
            and generation == self._active_record_generation
            and self._session is not None
            and self._state in {SessionState.STARTING, SessionState.RECORDING}
        )

    def _recording_payload_matches_stopping_locked(self, payload: Mapping[str, Any]) -> bool:
        """Return whether a stop acknowledgement belongs to the prior command."""
        generation = self._payload_generation(payload)
        if generation is None:
            return self._record_generation_state is None
        return self._stopping_record_generation is not None and generation == self._stopping_record_generation

    def _boot_trace(self, message: str) -> None:
        try:
            path = self.config.data_dir / "clinical_capture.log"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")
        except OSError:
            pass

    def _new_camera_status(self, camera: CameraConfig) -> dict[str, Any]:
        return {
            "name": camera.name,
            "required": camera.required,
            "index": camera.index,
            "frame_transform": camera.frame_transform,
            "requested": f"{camera.width}x{camera.height}@{camera.fps}",
            "target_fps": camera.fps,
            "minimum_fps": round(camera.fps * self.config.minimum_fps_ratio, 1),
            "state": "offline",
            "actual": None,
            "simulated": False,
            "capture_fps": 0.0,
            "frames_in_interval": 0,
            "recorded_frames": 0,
            "recording": False,
            "black_percent": 0.0,
            "read_p95_ms": 0.0,
            "mean": 0.0,
            "std": 0.0,
            "sequence": 0,
            "timestamp_ns": 0,
            "last_frame_age_ms": None,
            "last_frame_monotonic": None,
            "last_update_monotonic": None,
            "reconnect_count": 0,
            "reconnect_attempts_total": 0,
            "read_failures": 0,
            "reconnect_attempt": 0,
            "reconnect_attempts": 0,
            "last_reconnect_at": None,
            "transform_applied": False,
            "transform_verified": False,
            "recording_path": None,
            "recording_codec": None,
            "recording_generation": None,
            "recording_duration_s": 0.0,
            "error": None,
            "pupil": {
                "state": "starting",
                "valid": False,
                "confidence": 0.0,
                "x": None,
                "y": None,
                "radius": None,
                "latency_ms": None,
                "detector_fps": 0.0,
                "valid_rate": 0.0,
                "age_ms": None,
                "frame_width": 0,
                "frame_height": 0,
                "geometry_ok": False,
                "quality_ok": False,
                "quality_state": "searching",
                "rejected_reason": None,
                "temporal_stable": False,
                "temporal_distance": None,
                "center_ok": False,
                "direction": "search",
                "guidance": None,
                "alignment": {
                    "state": "starting",
                    "ready": False,
                    "progress": 0.0,
                    "hold_seconds": 0.0,
                    "required_hold_seconds": self.config.pupil_alignment_hold_seconds,
                    "quality_ok": False,
                    "rejected_reason": None,
                    "temporal_stable": False,
                    "center_ok": False,
                    "direction": "search",
                    "fresh": False,
                    "age_ms": None,
                    "confidence": 0.0,
                    "threshold": self.config.pupil_confidence_threshold,
                    "guidance": "正在等待瞳孔检测",
                },
                "error": None,
            },
        }

    @property
    def state(self) -> SessionState:
        with self._lock:
            return self._state

    def start(self) -> dict[str, Any]:
        """Start camera workers; hardware errors are reported in status, not raised."""
        self._boot_trace("manager.start entered")
        with self._lock:
            if self._state in {SessionState.STARTING, SessionState.READY, SessionState.RECORDING, SessionState.STOPPING}:
                return self.status()
            if self._stop_event is None or self._status_queue is None:
                self._init_runtime_resources()
            self._state = SessionState.STARTING
            self._last_error = None
            self._monitor_stop.clear()
            self._monitor_thread = threading.Thread(target=self._monitor_loop, name="clinical-capture-monitor", daemon=True)
            self._monitor_thread.start()
            for camera in self.config.cameras:
                self._boot_trace(f"starting worker {camera.name}")
                payload = camera.public_dict()
                process = self._context.Process(
                    name=f"clinical-capture-{camera.name}",
                    target=capture_worker,
                    args=(
                        payload,
                        self._owners[camera.name].state,
                        self._status_queue,
                        self._stop_event,
                        self._record_event,
                        self._record_directory_state,
                        self.config.simulate,
                        self.config.status_interval_seconds,
                        self.config.jpeg_quality,
                        self.config.camera_read_failure_threshold,
                        self.config.camera_reconnect_attempts,
                        self.config.camera_reconnect_delay_seconds,
                        self._record_generation_state,
                    ),
                )
                self._processes[camera.name] = process
                try:
                    process.start()
                    self._boot_trace(f"worker {camera.name} started")
                except Exception as error:
                    # ``multiprocessing.Process.start`` may fail before a
                    # child is spawned.  Do not leave that unstarted object in
                    # the shutdown list: ``join``/``is_alive`` assert on it.
                    if getattr(process, "pid", None) is None:
                        self._processes.pop(camera.name, None)
                        try:
                            process.close()
                        except (AssertionError, ValueError, OSError):
                            pass
                    self._cameras[camera.name]["state"] = "error"
                    self._cameras[camera.name]["error"] = str(error)
                    self._last_error = str(error)
            self._start_pupil_services()
            deadline = time.monotonic() + self.config.startup_timeout_seconds
            self._boot_trace("workers started; waiting for readiness")
        while time.monotonic() < deadline:
            with self._lock:
                required_ready = all(
                    self._cameras[camera.name]["state"] == "ready"
                    and float(self._cameras[camera.name].get("capture_fps") or 0.0) > 0.0
                    for camera in self.config.cameras
                    if camera.required
                )
                required_failed = any(
                    self._cameras[camera.name]["state"] == "error"
                    for camera in self.config.cameras
                    if camera.required
                )
                if required_ready:
                    self._boot_trace("manager.start ready")
                    self._state = SessionState.READY
                    return self.status()
                if required_failed:
                    self._state = SessionState.ERROR
                    return self.status()
            time.sleep(0.05)
        with self._lock:
            self._state = SessionState.ERROR
            self._last_error = self._last_error or "required camera startup timed out"
            return self.status()

    def restart(self) -> dict[str, Any]:
        status = self._restart_once()
        return self._restart_until_healthy(status)

    def _restart_once(self) -> dict[str, Any]:
        with self._lifecycle_lock:
            # A recording handshake uses STARTING until every worker confirms
            # its writer.  Restarting during that window must still finalize
            # the pending session and clear the shared record event; otherwise
            # the reinitialized workers can inherit an old writer command.
            if self._session is not None:
                self.stop_session(failure_reason="采集进程被重启，当前会话已安全结束")
            self._shutdown_workers()
            with self._lock:
                self._cameras = {camera.name: self._new_camera_status(camera) for camera in self.config.cameras}
                self._state = SessionState.IDLE
                self._last_error = None
                self._init_runtime_resources()
            return self.start()

    def _camera_restart_needed(self, status: Mapping[str, Any]) -> bool:
        preflight = status.get("preflight") if isinstance(status, Mapping) else None
        cameras = preflight.get("cameras") if isinstance(preflight, Mapping) else None
        if not isinstance(cameras, Mapping):
            return False
        for camera in cameras.values():
            if not isinstance(camera, Mapping) or not camera.get("required", True):
                continue
            if camera.get("state") not in {"ready", "recording"}:
                return True
            try:
                fps = float(camera.get("capture_fps") or 0.0)
                minimum = float(camera.get("minimum_fps") or 0.0)
            except (TypeError, ValueError):
                return True
            if fps <= 0.0 or (minimum > 0.0 and fps < minimum):
                return True
            try:
                age_ms = float(camera.get("last_frame_age_ms") or 0.0)
            except (TypeError, ValueError):
                age_ms = 0.0
            if age_ms > float(self.config.stale_frame_timeout_seconds) * 1000.0:
                return True
            if not self.config.simulate and not bool(camera.get("transform_verified")):
                return True
        return False

    def _restart_until_healthy(self, status: dict[str, Any]) -> dict[str, Any]:
        """Retry a bounded number of times while DirectShow releases handles."""
        time.sleep(1.25)
        status = self.status()
        for _attempt in range(2):
            if not self._camera_restart_needed(status):
                return status
            status = self._restart_once()
            time.sleep(1.25)
            status = self.status()
        return status

    def reconfigure(
        self,
        config: ClinicalConfig,
        *,
        device_settings_confirmed: bool,
        device_settings_error: str | None = None,
    ) -> dict[str, Any]:
        candidate = config.validate()
        with self._lifecycle_lock:
            with self._lock:
                if self._state in {SessionState.RECORDING, SessionState.STOPPING} or self._session is not None:
                    raise RuntimeError("正在采集或保存，不能修改相机设置")
                if candidate.data_dir != self.config.data_dir:
                    raise ValueError("camera reconfiguration cannot change the data directory")
                self._state = SessionState.STARTING
            self._shutdown_workers()
            with self._lock:
                self.config = candidate
                self.device_settings_confirmed = bool(device_settings_confirmed)
                self.device_settings_error = device_settings_error
                self._cameras = {camera.name: self._new_camera_status(camera) for camera in self.config.cameras}
                self._state = SessionState.IDLE
                self._last_error = None
                self._fault_handling = False
                self._init_runtime_resources()
            status = self.start()
        return self._restart_until_healthy(status)

    def scan_cameras(self, *, max_index: int = 3) -> dict[str, Any]:
        with self._lifecycle_lock:
            with self._lock:
                if self._state in {SessionState.RECORDING, SessionState.STOPPING} or self._session is not None:
                    raise RuntimeError("正在采集或保存，不能扫描相机")
                self._state = SessionState.STARTING
                reference = next(iter(self.config.cameras))
            self._shutdown_workers()
            candidates: list[dict[str, Any]] = []
            try:
                candidates = probe_camera_indices(
                    max_index=max_index,
                    width=reference.width,
                    height=reference.height,
                    fps=reference.fps,
                    jpeg_quality=self.config.jpeg_quality,
                )
            finally:
                with self._lock:
                    self._cameras = {camera.name: self._new_camera_status(camera) for camera in self.config.cameras}
                    self._state = SessionState.IDLE
                    self._last_error = None
                    self._init_runtime_resources()
                restarted = self.start()
            return {"cameras": candidates, "status": restarted}

    def _start_pupil_services(self) -> None:
        """Start latest-frame detectors without touching the high-rate workers."""
        for service in self._pupil_services.values():
            try:
                service.start()
            except Exception as error:  # detector status remains visible; capture can still be reviewed
                with self._lock:
                    item = self._cameras.get(service.camera_name)
                    if item is not None:
                        item["pupil"] = {
                            "state": "error",
                            "valid": False,
                            "confidence": 0.0,
                            "x": None,
                            "y": None,
                            "radius": None,
                            "latency_ms": None,
                            "detector_fps": 0.0,
                            "valid_rate": 0.0,
                            "age_ms": None,
                            "quality_ok": False,
                            "quality_state": "error",
                            "rejected_reason": "detector_error",
                            "temporal_stable": False,
                            "temporal_distance": None,
                            "direction": "search",
                            "guidance": "瞳孔算法不可用，请检查安装或查看日志",
                            "alignment": {
                                "state": "error",
                                "ready": False,
                                "progress": 0.0,
                                "hold_seconds": 0.0,
                                "required_hold_seconds": self.config.pupil_alignment_hold_seconds,
                                "quality_ok": False,
                                "rejected_reason": "detector_error",
                                "temporal_stable": False,
                                "center_ok": False,
                                "direction": "search",
                                "fresh": False,
                                "age_ms": None,
                                "confidence": 0.0,
                                "threshold": self.config.pupil_confidence_threshold,
                                "guidance": "瞳孔算法不可用，请检查安装或查看日志",
                            },
                            "error": f"瞳孔检测启动失败: {error}",
                        }

    def preflight(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            cameras: dict[str, dict[str, Any]] = {}
            blocking: list[str] = []
            warnings: list[str] = []
            if not self.config.simulate and not self.device_settings_confirmed:
                blocking.append("请先确认左右眼相机和画面方向")
            if self.device_settings_error:
                warnings.append(self.device_settings_error)
            for spec in self.config.cameras:
                item = dict(self._cameras[spec.name])
                # Only a status carrying a real frame may refresh the frame
                # watchdog.  Reconnect/error messages must not mask a stalled
                # stream by looking like fresh data.
                last_frame = item.get("last_frame_monotonic")
                age = None if last_frame is None else max(0.0, now - float(last_frame))
                item["last_frame_age_ms"] = None if age is None else round(age * 1000.0, 1)
                item.pop("last_frame_monotonic", None)
                item.pop("last_update_monotonic", None)
                service = self._pupil_services.get(spec.name)
                if service is not None:
                    try:
                        item["pupil"] = service.status()
                    except Exception as error:
                        item["pupil"] = {
                            "state": "error",
                            "valid": False,
                            "confidence": 0.0,
                            "x": None,
                            "y": None,
                            "radius": None,
                            "latency_ms": None,
                            "detector_fps": 0.0,
                            "valid_rate": 0.0,
                            "age_ms": None,
                            "quality_ok": False,
                            "quality_state": "error",
                            "rejected_reason": "status_error",
                            "temporal_stable": False,
                            "temporal_distance": None,
                            "direction": "search",
                            "alignment": {
                                "state": "error",
                                "ready": False,
                                "progress": 0.0,
                                "hold_seconds": 0.0,
                                "required_hold_seconds": self.config.pupil_alignment_hold_seconds,
                                "quality_ok": False,
                                "rejected_reason": "status_error",
                                "temporal_stable": False,
                                "center_ok": False,
                                "direction": "search",
                                "fresh": False,
                                "age_ms": None,
                                "confidence": 0.0,
                                "threshold": self.config.pupil_confidence_threshold,
                                "guidance": "瞳孔算法状态读取失败，请检查日志",
                            },
                            "error": str(error),
                        }
                cameras[spec.name] = item
                if item.get("recording") and self._session is None:
                    # A residual writer is unsafe even for an optional camera:
                    # starting another subject could append data to the prior
                    # session.  Keep this a hard gate so the UI agrees with the
                    # backend for every camera configuration.
                    blocking.append(f"{spec.name}: 上一会话仍在录制，请重启采集设备")
                if item["state"] == "error":
                    (blocking if spec.required else warnings).append(
                        f"{spec.name}: {item.get('error') or '采集进程错误'}"
                    )
                    continue
                if item["state"] == "reconnecting":
                    attempt = int(item.get("reconnect_attempt") or 0)
                    attempts = int(item.get("reconnect_attempts") or 0)
                    progress = f"（第 {attempt}/{attempts} 次）" if attempts else ""
                    (blocking if spec.required else warnings).append(f"{spec.name}: 正在重连{progress}")
                    continue
                if item["state"] not in {"ready", "recording"}:
                    (blocking if spec.required else warnings).append(f"{spec.name}: 尚未就绪")
                    continue
                if age is None or age > self.config.stale_frame_timeout_seconds:
                    (blocking if spec.required else warnings).append(f"{spec.name}: 帧流超时")
                fps = float(item.get("capture_fps") or 0.0)
                if fps <= 0.0 or fps < spec.fps * self.config.minimum_fps_ratio:
                    (blocking if spec.required else warnings).append(
                        f"{spec.name}: 帧率 {fps:.1f} 低于 {spec.fps * self.config.minimum_fps_ratio:.1f}"
                    )
                if not self.config.simulate and not bool(item.get("transform_verified")):
                    (blocking if spec.required else warnings).append(
                        f"{spec.name}: 画面方向尚未通过首帧验证"
                    )
                if float(item.get("black_percent") or 0.0) > self.config.max_black_percent:
                    (blocking if spec.required else warnings).append(
                        f"{spec.name}: 黑帧 {float(item['black_percent']):.1f}%"
                    )
            alignment_items: list[Mapping[str, Any]] = []
            for spec in self.config.cameras:
                if not spec.required:
                    continue
                item = cameras[spec.name]
                if item.get("state") not in {"ready", "recording"}:
                    continue
                pupil = item.get("pupil") if isinstance(item.get("pupil"), Mapping) else {}
                alignment = pupil.get("alignment") if isinstance(pupil.get("alignment"), Mapping) else {}
                alignment_items.append(alignment)
                if not self.config.simulate and not bool(alignment.get("ready")):
                    guidance = str(
                        alignment.get("guidance")
                        or pupil.get("guidance")
                        or "请根据实时预览调整相机，直到瞳孔置信度达标"
                    )
                    blocking.append(f"{spec.name}: {guidance}")

            alignment_required = not self.config.simulate
            alignment_ready = (
                True
                if not alignment_required
                else bool(alignment_items) and all(bool(item.get("ready")) for item in alignment_items)
            )
            alignment_progress = (
                1.0
                if not alignment_required
                else min((float(item.get("progress") or 0.0) for item in alignment_items), default=0.0)
            )
            alignment_guidance = (
                "模拟模式：跳过真实瞳孔对准门禁"
                if not alignment_required
                else next(
                    (
                        str(item.get("guidance"))
                        for item in alignment_items
                        if not bool(item.get("ready")) and item.get("guidance")
                    ),
                    "请根据双眼实时预览调整相机，等待瞳孔置信度达标",
                )
            )
            ready = not blocking and self._state in {SessionState.READY, SessionState.RECORDING}
            return {
                "ready": ready,
                "state": self._state.value,
                "checked_at": utc_now(),
                "blocking_reasons": blocking,
                "warnings": warnings,
                "cameras": cameras,
                "pupil_alignment": {
                    "required": alignment_required,
                    "ready": alignment_ready,
                    "progress": round(max(0.0, min(1.0, alignment_progress)), 3),
                    "hold_seconds": self.config.pupil_alignment_hold_seconds,
                    "confidence_threshold": self.config.pupil_confidence_threshold,
                    "candidate_filter": {
                        "min_y_ratio": self.config.pupil_candidate_min_y_ratio,
                        "max_y_ratio": self.config.pupil_candidate_max_y_ratio,
                        "max_axis_ratio": self.config.pupil_candidate_max_axis_ratio,
                        "max_jump_ratio": self.config.pupil_candidate_max_jump_ratio,
                        "min_diameter_ratio": self.config.pupil_candidate_min_diameter_ratio,
                        "stability_frames": self.config.pupil_candidate_stability_frames,
                    },
                    "operator_guide": {
                        "margin_x": self.config.preview_guide_margin_x,
                        "margin_y": self.config.preview_guide_margin_y,
                        "gating": False,
                    },
                    "guidance": alignment_guidance,
                },
            }

    def start_session(self, metadata: SessionMetadata | Mapping[str, Any]) -> SessionRecord:
        """Start a session while serializing against stop/restart operations."""
        with self._lifecycle_lock:
            return self._start_session_once(metadata)

    def _start_session_once(self, metadata: SessionMetadata | Mapping[str, Any]) -> SessionRecord:
        if not isinstance(metadata, SessionMetadata):
            metadata = SessionMetadata.from_mapping(metadata)
        metadata.validate()
        with self._lock:
            if self._state != SessionState.READY:
                raise RuntimeError("采集设备尚未就绪，不能开始会话")
            check = self.preflight()
            if not check["ready"]:
                raise RuntimeError("; ".join(check["blocking_reasons"]) or "采集预检未通过")
            residual_recording = [
                camera.name
                for camera in self.config.cameras
                if self._cameras[camera.name].get("recording")
            ]
            if residual_recording:
                raise RuntimeError(
                    "上一会话仍在录制："
                    + "、".join(residual_recording)
                    + "；请重启采集设备后再开始"
                )
            if self._record_directory_state is None:
                raise RuntimeError("recording directory control is unavailable")
            record = self.store.create(metadata, self.config.public_dict())
            record.state = "recording"
            record.started_at = utc_now()
            self.store.save_record(record)
            self._session = record
            # Keep the state out of READY while camera workers acknowledge the
            # recording command.  A timeout must be stoppable and must not make
            # the UI offer a second session over a still-active record event.
            self._state = SessionState.STARTING
            self._last_error = None
            # Advance before publishing the path/event so every worker sees a
            # fresh command generation, even when the previous writer is still
            # finishing its shutdown path.
            active_generation = self._advance_record_generation()
            self._active_record_generation = active_generation
            self._stopping_record_generation = None
            set_shared_path(self._record_directory_state, record.path)
            for camera in self.config.cameras:
                self._cameras[camera.name]["recording_path"] = None
                self._cameras[camera.name]["recording_codec"] = None
                self._cameras[camera.name]["recording_generation"] = None
                self._cameras[camera.name]["recorded_frames"] = 0
                self._cameras[camera.name]["recording_duration_s"] = 0.0
                service = self._pupil_services.get(camera.name)
                if service is not None:
                    try:
                        service.start_session(record.session_id, record.path)
                    except Exception as error:
                        # A detector/CSV failure must be visible to the operator,
                        # but it must not silently prevent raw acquisition.
                        self._cameras[camera.name]["pupil"] = {
                            "state": "error",
                            "valid": False,
                            "confidence": 0.0,
                            "x": None,
                            "y": None,
                            "radius": None,
                            "latency_ms": None,
                            "detector_fps": 0.0,
                            "valid_rate": 0.0,
                            "age_ms": None,
                            "quality_ok": False,
                            "quality_state": "error",
                            "rejected_reason": "csv_error",
                            "temporal_stable": False,
                            "temporal_distance": None,
                            "direction": "search",
                            "guidance": "瞳孔数据文件创建失败，请检查会话目录权限",
                            "alignment": {
                                "state": "error",
                                "ready": False,
                                "progress": 0.0,
                                "hold_seconds": 0.0,
                                "required_hold_seconds": self.config.pupil_alignment_hold_seconds,
                                "quality_ok": False,
                                "rejected_reason": "csv_error",
                                "temporal_stable": False,
                                "center_ok": False,
                                "direction": "search",
                                "fresh": False,
                                "age_ms": None,
                                "confidence": 0.0,
                                "threshold": self.config.pupil_confidence_threshold,
                                "guidance": "瞳孔数据文件创建失败，请检查会话目录权限",
                            },
                            "error": f"瞳孔数据文件创建失败: {error}",
                        }
            assert self._record_event is not None
            self._record_event.set()
            expected = {
                camera.name
                for camera in self.config.cameras
                if self._cameras[camera.name]["state"] == "ready"
            }
            self._recording_expected = set(expected)
            self._recording_stop_pending = set()
            self._recording_stop_baseline = {}
        if not self._wait_for_recording_ack(expected, timeout=self.config.recording_ack_timeout_seconds):
            self.stop_session(failure_reason="一个或多个采集进程未确认开始录制")
            raise RuntimeError("录制启动超时，已安全结束本次会话")
        with self._lock:
            # A stop request can complete on another API worker while the
            # recording acknowledgement wait is in progress.  Never resurrect
            # that finished session or return ``None`` as a successful record.
            if self._session is None or self._state != SessionState.STARTING:
                raise RuntimeError("会话在录制启动确认前已停止")
            self._session.state = "recording"
            self._state = SessionState.RECORDING
            return self._session

    def stop_session(self, failure_reason: str | None = None) -> SessionRecord | None:
        """Serialize stop/finalize so one session is persisted exactly once."""
        with self._lifecycle_lock:
            return self._stop_session_once(failure_reason)

    def _stop_session_once(self, failure_reason: str | None = None) -> SessionRecord | None:
        # Drain ACKs that were already queued before taking the stop snapshot.
        # The lifecycle lock prevents a concurrent start, while the generation
        # below handles a worker that is still unwinding an older command.
        self._drain_status()
        with self._lock:
            record = self._session
            if record is None:
                return None
            if self._state not in {
                SessionState.STARTING,
                # READY is included defensively for a session created by an
                # older caller before the pending-start state was introduced.
                # An active _session must always be cleanable, regardless of
                # the state marker left by that caller.
                SessionState.READY,
                SessionState.RECORDING,
                SessionState.STOPPING,
                SessionState.ERROR,
            }:
                return record
            prior_state = self._state
            self._state = SessionState.STOPPING
            self._stopping_record_generation = self._active_record_generation
            self._active_record_generation = None
            self._advance_record_generation()
            if self._record_event is not None:
                self._record_event.clear()
            expected = set(self._recording_expected)
            if prior_state != SessionState.STARTING:
                # A stop packet that was already queued before this call has
                # made the camera safe; do not wait forever for that packet a
                # second time. In a pending start, retain every commanded
                # worker so a late writer-open is still required to settle.
                expected = {
                    name for name in expected
                    if self._cameras.get(name, {}).get("recording")
                }
            if not expected:
                expected = {
                    camera.name
                    for camera in self.config.cameras
                    if self._cameras[camera.name].get("recording")
                }
            self._recording_stop_pending = set(expected)
            self._recording_stop_baseline = {
                name: int(self._cameras[name].get("sequence") or 0)
                for name in expected
                if name in self._cameras
            }
        stop_acknowledged = self._wait_for_recording_stop(
            expected,
            timeout=self.config.recording_ack_timeout_seconds,
        )
        if not stop_acknowledged and not failure_reason:
            # Do not return READY while a worker may still own an open writer.
            # The operator must restart the capture workers before another
            # subject can be recorded, which prevents sessions from being
            # appended to the previous AVI after a stop timeout.
            failure_reason = "一个或多个采集进程未确认停止录制"
        pupil_summaries: dict[str, dict[str, Any]] = {}
        for camera in self.config.cameras:
            service = self._pupil_services.get(camera.name)
            if service is not None:
                try:
                    pupil_summaries[camera.name] = service.stop_session()
                except Exception as error:
                    pupil_summaries[camera.name] = {
                        "samples": 0,
                        "valid_samples": 0,
                        "valid_rate": 0.0,
                        "detector_fps": 0.0,
                        "latency_p95_ms": 0.0,
                        "csv_path": None,
                        "error": str(error),
                    }
        # Do not carry one subject's stable pupil track or alignment hold into
        # the next session.  The summary above intentionally captured the
        # stop-time state before this reset for report provenance.
        for service in self._pupil_services.values():
            try:
                service.reset_alignment()
            except Exception:
                pass
        with self._lock:
            camera_files: dict[str, str] = {}
            camera_stats: dict[str, dict[str, Any]] = {}
            for camera in self.config.cameras:
                item = dict(self._cameras[camera.name])
                path = item.get("recording_path")
                if path:
                    camera_files[camera.name] = str(path)
                fps = float(item.get("capture_fps") or 0.0)
                black = float(item.get("black_percent") or 0.0)
                quality_ok = (
                    item.get("state") not in {"error", "offline"}
                    and fps > 0.0
                    and fps >= camera.fps * self.config.minimum_fps_ratio
                    and black <= self.config.max_black_percent
                    and int(item.get("recorded_frames") or 0) > 0
                    # A reconnect invalidates the transform marker until the
                    # first frame from the new handle is processed. Do not
                    # call a session clinically ready while that direction
                    # check is still pending on a real camera.
                    and (self.config.simulate or bool(item.get("transform_verified")))
                )
                camera_stats[camera.name] = {
                    "name": camera.name,
                    "required": camera.required,
                    "capture_fps": fps,
                    "frames_in_interval": int(item.get("frames_in_interval") or 0),
                    "recorded_frames": int(item.get("recorded_frames") or 0),
                    "recording_duration_s": max(0.0, float(item.get("recording_duration_s") or 0.0)),
                    "black_percent": black,
                    "read_p95_ms": float(item.get("read_p95_ms") or 0.0),
                    "actual": item.get("actual"),
                    "recording_codec": item.get("recording_codec"),
                    "frame_transform": item.get("frame_transform", camera.frame_transform),
                    "transform_applied": bool(item.get("transform_applied")),
                    "transform_verified": bool(item.get("transform_verified")),
                    "reconnect_count": int(item.get("reconnect_count") or 0),
                    "reconnect_attempts_total": int(item.get("reconnect_attempts_total") or 0),
                    "last_reconnect_at": item.get("last_reconnect_at"),
                    "simulated": bool(item.get("simulated")),
                    "quality_ok": quality_ok,
                    "error": item.get("error"),
                    "pupil": pupil_summaries.get(camera.name, {}),
                }
            required_failed = [
                value["name"] for value in camera_stats.values()
                if value["required"] and not value["quality_ok"]
            ]
            error = failure_reason or ("质量检查未通过: " + ", ".join(required_failed) if required_failed else None)
            final_state = "error" if error else "completed"
            try:
                finalized = self.store.finalize(
                    record,
                    state=final_state,
                    camera_files=camera_files,
                    camera_stats=camera_stats,
                    error=error,
                )
            except Exception as storage_error:
                # Disk-full/permission failures must not strand the state machine
                # in STOPPING.  Preserve the in-memory evidence and attempt one
                # last small metadata write; the response clearly reports error.
                error = f"会话文件保存失败: {storage_error}"
                record.state = "error"
                record.stopped_at = utc_now()
                record.error = error
                record.camera_files = dict(camera_files)
                record.camera_stats = camera_stats
                try:
                    self.store.save_record(record)
                except Exception:
                    pass
                finalized = record
            self._last_session = finalized
            self._session = None
            self._active_record_generation = None
            self._stopping_record_generation = None
            self._recording_expected = set()
            self._recording_stop_pending = set()
            self._recording_stop_baseline = {}
            # Stop ACKs are consumed while the manager is STOPPING, so their
            # camera entries intentionally do not transition to READY until
            # finalization is complete. This makes an immediate next-session
            # start deterministic without masking an unresolved recording
            # flag or camera error.
            for item in self._cameras.values():
                if not item.get("recording") and item.get("state") not in {"error", "offline"}:
                    item["state"] = "ready"
            # A successful session does not require a process restart before the
            # next subject.  Keep workers in READY while exposing the completed
            # record through last_session for review/export.
            self._state = SessionState.ERROR if error else SessionState.READY
            self._last_error = error
            self._fault_handling = False
            return finalized

    def status(self) -> dict[str, Any]:
        with self._lock:
            preflight = self.preflight()
            active = self._session.public_dict() if self._session is not None else None
            last = self._last_session.public_dict() if self._last_session is not None else None
            return {
                "app_state": self._state.value,
                "last_error": self._last_error,
                "simulate": self.config.simulate,
                "uptime_s": round(max(0.0, time.monotonic() - self._started_monotonic), 1),
                "data_dir": str(self.config.data_dir),
                "session": active,
                "last_session": last,
                "preflight": preflight,
                "device_settings": {
                    "confirmed": self.device_settings_confirmed,
                    "error": self.device_settings_error,
                },
            }

    def preview_jpeg(
        self,
        camera_name: str,
        *,
        guide_rect: tuple[float, float, float, float] | None = None,
    ) -> bytes | None:
        """Return the newest annotated frame as JPEG.

        ``guide_rect`` is an optional normalized operator rectangle. It is
        rendered only in the preview and never participates in pupil validity
        or the recording gate.
        """
        with self._lock:
            owner = self._owners.get(camera_name)
            pupil = self._pupil_services.get(camera_name)
        if owner is None:
            return None
        snapshot: FrameSnapshot | None = owner.read_latest()
        if snapshot is None:
            return None
        frame = snapshot.frame
        if pupil is not None:
            try:
                frame = pupil.overlay(frame, guide_rect=guide_rect)
            except Exception:
                if frame.ndim == 2:
                    frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        elif frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        ok, encoded = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), self.config.jpeg_quality],
        )
        return bytes(encoded) if ok else None

    def sessions(self) -> list[dict[str, Any]]:
        return [record.public_dict() for record in self.store.list_records()]

    def get_session(self, session_id: str) -> SessionRecord:
        return self.store.read_record(session_id)

    def export_session(self, session_id: str) -> Path:
        from .exporter import export_session

        return export_session(self.store, session_id)

    def save_screening(self, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        record = self.store.read_record(session_id)
        if record.state not in {"completed", "error", "cancelled"}:
            raise ValueError("screening can only be attached to a stopped session")
        screening = str(payload.get("screening", "none"))
        if screening not in {"none", "pathology", "emotion", "custom"}:
            raise ValueError("unsupported screening type")
        score = payload.get("score")
        if score not in (None, ""):
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= float(score) <= 100:
                raise ValueError("score must be between 0 and 100")
            score = float(score)
        result = {
            "screening": screening,
            "score": score,
            "label": str(payload.get("label", ""))[:200],
            "notes": str(payload.get("notes", ""))[:4000],
            "saved_at": utc_now(),
            "disclaimer": "仅供研究/筛查记录，不构成临床诊断或治疗建议。",
        }
        saved = self.store.save_screening(record, result)
        with self._lock:
            if self._last_session is not None and self._last_session.session_id == session_id:
                self._last_session.screening_result = dict(saved)
        return saved

    def close(self) -> None:
        with self._lifecycle_lock:
            with self._lock:
                active_session = self._session is not None
            if active_session:
                self.stop_session(failure_reason="应用关闭，录制已安全结束")
            self._shutdown_workers()
            with self._lock:
                self._state = SessionState.IDLE

    def _shutdown_workers(self) -> None:
        self._monitor_stop.set()
        if self._monitor_thread is not None and self._monitor_thread is not threading.current_thread():
            self._monitor_thread.join(timeout=2.0)
        self._monitor_thread = None
        with self._lock:
            # Invalidate any command a child may have cached before shutdown.
            self._active_record_generation = None
            self._stopping_record_generation = None
            self._advance_record_generation()
            if self._record_event is not None:
                self._record_event.clear()
            if self._stop_event is not None:
                self._stop_event.set()
            processes = list(self._processes.values())
        self._shutdown_pupil_services()
        for process in processes:
            # A Process whose ``start`` raised has no child handle.  Calling
            # join/is_alive on it raises AssertionError on Windows; discard it
            # quietly and continue shutting down the other cameras.
            try:
                if getattr(process, "pid", None) is None:
                    process.close()
                    continue
                process.join(timeout=self.config.shutdown_timeout_seconds)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2.0)
            except (AssertionError, ValueError, OSError):
                try:
                    process.close()
                except (AssertionError, ValueError, OSError):
                    pass
        with self._lock:
            for owner in self._owners.values():
                owner.close()
            self._owners = {}
            if self._status_queue is not None:
                self._status_queue.close()
                self._status_queue.join_thread()
            self._status_queue = None
            self._stop_event = None
            self._record_event = None
            self._record_directory_state = None
            self._record_generation_state = None
            self._recording_expected = set()
            self._recording_stop_pending = set()
            self._recording_stop_baseline = {}
            self._processes = {}
        # Some DirectShow drivers keep the USB handle briefly after process
        # exit. Reopening immediately can yield a healthy-looking 1 fps stream.
        time.sleep(1.0)

    def _shutdown_pupil_services(self) -> None:
        services = list(self._pupil_services.values())
        for service in services:
            try:
                service.close()
            except Exception:
                pass
        with self._lock:
            self._pupil_services = {}

    def _monitor_loop(self) -> None:
        while not self._monitor_stop.wait(0.08):
            self._drain_status()
            should_abort = False
            reason = ""
            with self._lock:
                if self._state == SessionState.RECORDING:
                    for camera in self.config.cameras:
                        item = self._cameras[camera.name]
                        process = self._processes.get(camera.name)
                        if camera.required and (item["state"] == "error" or (process is not None and process.exitcode not in (None, 0))):
                            should_abort = True
                            reason = f"{camera.name} 采集进程异常，录制已安全结束"
                            break
                        age = item.get("last_frame_monotonic")
                        if camera.required and (
                            age is None
                            or time.monotonic() - float(age) > self.config.stale_frame_timeout_seconds
                        ):
                            should_abort = True
                            reason = f"{camera.name} 帧流不可用或已超时，录制已安全结束"
                            break
            if should_abort:
                self._abort_recording_async(reason)

    def _drain_status(self) -> None:
        status_queue = self._status_queue
        if status_queue is None:
            return
        while True:
            try:
                payload = status_queue.get_nowait()
            except queue.Empty:
                return
            camera_name = payload.get("camera")
            if camera_name not in self._cameras:
                continue
            with self._lock:
                item = self._cameras[camera_name]
                kind = payload.get("kind")
                active_generation_match = self._recording_payload_matches_active_locked(payload)
                stopping_generation_match = self._recording_payload_matches_stopping_locked(payload)

                # New workers attach a generation to all recording-related
                # packets. Discard packets from an invalidated command before
                # they can refresh the frame watchdog or alter recording UI
                # state. Generation-less packets are accepted only for a
                # manager explicitly running without the generation control.
                payload_generation = self._payload_generation(payload)
                if kind == "recording_started":
                    if not active_generation_match:
                        continue
                if kind == "stats":
                    if payload_generation is not None and not (
                        active_generation_match or stopping_generation_match
                    ):
                        continue
                    if (
                        payload_generation is None
                        and bool(payload.get("recording"))
                        and self._record_generation_state is not None
                    ):
                        # A live manager cannot identify which generation a
                        # legacy recording packet belongs to; never let it
                        # resurrect a writer or refresh a stale heartbeat.
                        continue
                if kind == "recording_stopped":
                    if not stopping_generation_match and not active_generation_match:
                        continue
                if kind in {"error", "stopped"} and payload_generation is not None:
                    if not (active_generation_match or stopping_generation_match):
                        continue
                update_monotonic = time.monotonic()
                item["last_update_monotonic"] = update_monotonic
                if kind == "starting":
                    item["state"] = "starting"
                elif kind == "opened":
                    item.update({
                        "state": "ready",
                        "actual": payload.get("actual"),
                        "simulated": bool(payload.get("simulated")),
                        "error": None,
                        "frame_transform": payload.get("frame_transform", item.get("frame_transform")),
                        "transform_applied": bool(payload.get("transform_applied")),
                        "transform_verified": bool(payload.get("transform_verified")),
                        "reconnect_count": int(payload.get("reconnect_count") or 0),
                        "recording": False,
                        "recording_generation": None,
                    })
                elif kind == "transform_verified":
                    item.update({
                        "frame_transform": payload.get("frame_transform", item.get("frame_transform")),
                        "transform_applied": True,
                        "transform_verified": True,
                    })
                elif kind == "read_failure":
                    item["read_failures"] = int(payload.get("consecutive_failures") or 0)
                    item["reconnect_count"] = int(payload.get("reconnect_count") or item.get("reconnect_count") or 0)
                elif kind == "reconnecting":
                    item.update({
                        "state": "reconnecting",
                        "transform_applied": False,
                        "transform_verified": False,
                        "reconnect_attempt": int(payload.get("attempt") or 0),
                        "reconnect_attempts": int(payload.get("attempts") or 0),
                        "reconnect_count": int(payload.get("reconnect_count") or item.get("reconnect_count") or 0),
                    })
                elif kind == "reconnect_failed":
                    item.update({
                        "state": "reconnecting",
                        "reconnect_attempt": int(payload.get("attempt") or 0),
                        "reconnect_attempts": int(payload.get("attempts") or 0),
                        "error": str(payload.get("message") or "相机重连失败，正在继续重试"),
                    })
                elif kind == "reconnected":
                    attempts_used = int(payload.get("attempt") or 0)
                    item.update({
                        "state": "recording" if item.get("recording") else "ready",
                        "actual": payload.get("actual") or item.get("actual"),
                        "error": None,
                        "reconnect_count": int(payload.get("reconnect_count") or item.get("reconnect_count") or 0),
                        "reconnect_attempt": attempts_used,
                        # Stats will replace this with the worker's exact
                        # cumulative value; this immediate estimate keeps a
                        # stop taken right after recovery auditable.
                        "reconnect_attempts_total": int(item.get("reconnect_attempts_total") or 0) + attempts_used,
                        "last_reconnect_at": payload.get("last_reconnect_at") or utc_now(),
                        "frame_transform": payload.get("frame_transform", item.get("frame_transform")),
                        # The worker emits a fresh transform_verified event
                        # after the first frame from this handle.
                        "transform_applied": False,
                        "transform_verified": False,
                    })
                elif kind == "stats":
                    previous_sequence = int(item.get("sequence") or 0)
                    previous_timestamp = int(item.get("timestamp_ns") or 0)
                    incoming_sequence = int(payload.get("sequence") or 0)
                    incoming_timestamp = int(payload.get("timestamp_ns") or 0)
                    interval_frames = payload.get("frames_in_interval")
                    if interval_frames is None:
                        # Compatibility for older/test producers: require an
                        # advancing sequence/timestamp, except for the first
                        # legacy packet where neither field is available.
                        frame_advanced = (
                            incoming_sequence > previous_sequence
                            or incoming_timestamp > previous_timestamp
                            or (
                                "sequence" not in payload
                                and "timestamp_ns" not in payload
                                and item.get("last_frame_monotonic") is None
                            )
                        )
                    else:
                        frame_advanced = int(interval_frames or 0) > 0 and (
                            incoming_sequence > previous_sequence
                            or incoming_timestamp > previous_timestamp
                        )
                    stats_updates = {key: payload[key] for key in (
                        "capture_fps", "frames_in_interval", "recorded_frames", "recording", "black_percent", "read_p95_ms",
                        "mean", "std", "sequence", "timestamp_ns", "frame_transform", "transform_applied",
                        "transform_verified", "reconnect_count", "reconnect_attempts_total",
                        "last_reconnect_at", "recording_generation",
                    ) if key in payload}
                    # A worker clears these fields immediately after sending
                    # ``recording_stopped``. A status interval that happens to
                    # expire afterward therefore carries None/0; never erase
                    # the path, codec, or elapsed-time evidence already
                    # acknowledged for this session.
                    for key in ("recording_path", "recording_codec"):
                        value = payload.get(key)
                        if value:
                            stats_updates[key] = value
                    if "recording_duration_s" in payload:
                        try:
                            incoming_duration = max(0.0, float(payload.get("recording_duration_s") or 0.0))
                        except (TypeError, ValueError):
                            incoming_duration = None
                        if incoming_duration is not None:
                            try:
                                current_duration = max(0.0, float(item.get("recording_duration_s") or 0.0))
                            except (TypeError, ValueError):
                                current_duration = 0.0
                            stats_updates["recording_duration_s"] = max(current_duration, incoming_duration)
                    item.update(stats_updates)
                    if frame_advanced:
                        item["last_frame_monotonic"] = update_monotonic
                        item["read_failures"] = 0
                        if self._state != SessionState.STOPPING:
                            if item.get("recording"):
                                item["state"] = "recording"
                            elif item["state"] not in {"error", "offline"}:
                                item["state"] = "ready"
                elif kind == "recording_started":
                    item.update({
                        "recording": True,
                        "state": "recording",
                        "recording_path": payload.get("path"),
                        "recording_codec": payload.get("codec"),
                        "recording_generation": payload_generation,
                    })
                elif kind == "recording_stopped":
                    if (
                        payload_generation is None
                        and self._session is None
                        and self._state != SessionState.STOPPING
                    ):
                        # A legacy worker cannot identify which session it
                        # stopped after finalization; ignore that late packet.
                        continue
                    path = payload.get("path") or item.get("recording_path")
                    codec = payload.get("recording_codec") or payload.get("codec") or item.get("recording_codec")
                    item.update({
                        "recording": False,
                        "recording_path": path,
                        "recording_codec": codec,
                        "recorded_frames": int(payload.get("recorded_frames") or item.get("recorded_frames") or 0),
                        "recording_generation": None,
                    })
                    if "recording_duration_s" in payload:
                        try:
                            acknowledged_duration = max(0.0, float(payload.get("recording_duration_s") or 0.0))
                        except (TypeError, ValueError):
                            acknowledged_duration = None
                        if acknowledged_duration is not None:
                            try:
                                previous_duration = max(0.0, float(item.get("recording_duration_s") or 0.0))
                            except (TypeError, ValueError):
                                previous_duration = 0.0
                            item["recording_duration_s"] = max(previous_duration, acknowledged_duration)
                    close_error = str(payload.get("close_error") or "").strip()
                    if close_error:
                        item["state"] = "error"
                        item["error"] = close_error
                        self._last_error = close_error
                    if self._state == SessionState.STOPPING and stopping_generation_match:
                        self._recording_stop_pending.discard(camera_name)
                    if item.get("state") != "error" and self._state != SessionState.STOPPING:
                        item["state"] = "ready"
                elif kind == "error":
                    item.update({"state": "error", "error": str(payload.get("message") or "采集进程错误")})
                    self._last_error = item["error"]
                elif kind == "stopped":
                    # A terminal worker event cannot make an existing error
                    # look healthy.  For non-error states it does, however,
                    # clear a stale recording flag even if the preceding
                    # recording_stopped packet was dropped.
                    item["recording"] = False
                    item["recording_generation"] = None
                    if self._state == SessionState.STOPPING and stopping_generation_match:
                        self._recording_stop_pending.discard(camera_name)
                    if item["state"] != "error":
                        item["state"] = "offline"

    def _wait_for_recording_ack(self, expected: set[str], *, timeout: float) -> bool:
        if not expected:
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain_status()
            with self._lock:
                if self._session is None or self._state != SessionState.STARTING:
                    return False
                if all(self._cameras[name].get("recording") for name in expected):
                    return True
                if any(self._cameras[name].get("state") == "error" for name in expected):
                    return False
            time.sleep(0.03)
        return False

    def _wait_for_recording_stop(self, expected: set[str], *, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain_status()
            with self._lock:
                if self._recording_stop_pending:
                    if not (self._recording_stop_pending & set(expected)):
                        return True
                elif all(not self._cameras[name].get("recording") for name in expected):
                    # Compatibility for callers that invoke this helper
                    # directly without initializing the pending set.
                    return True
            time.sleep(0.03)
        return False

    def _abort_recording_async(self, reason: str) -> None:
        with self._lock:
            if self._fault_handling or self._session is None:
                return
            self._fault_handling = True
        self._fault_thread = threading.Thread(
            target=self.stop_session,
            kwargs={"failure_reason": reason},
            name="clinical-capture-fault-stop",
            daemon=True,
        )
        self._fault_thread.start()
