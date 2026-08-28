"""Low-latency pupil preview and session recorder.

The high-speed camera workers deliberately do not run computer vision in their
capture loop.  This module consumes the newest shared-memory frame on a small,
bounded cadence and uses the same ``pupil_detectors.Detector2D`` implementation
used by the project's binocular calibration tools.  Dropped detector frames do
not affect the raw AVI or timestamp stream.
"""

from __future__ import annotations

from collections import deque
import csv
import math
from pathlib import Path
import threading
import time
from typing import Any, Mapping

import cv2
import numpy as np

from apps.multicamera_capture.shared_frame import FrameSnapshot, SharedFrameOwner


PUPIL_CSV_FIELDS = (
    "camera",
    "eye_id",
    "sequence",
    "capture_timestamp_ns",
    "detection_timestamp_ns",
    "latency_ms",
    "valid",
    "confidence",
    "center_x",
    "center_y",
    "radius",
    "diameter",
    "axis_width",
    "axis_height",
    "angle",
    "detector_fps",
    "quality_ok",
    "rejected_reason",
    "temporal_distance",
    "error",
)


# Detector2D is intentionally kept as the primary detector.  These limits are
# a small acceptance layer around its single best candidate: they reject an
# ellipse that is clipped by the image, implausibly elongated, or jumps from a
# tracked pupil in one detector tick.  They do not alter Detector2D's raw
# confidence value and they do not use the operator guide rectangle.
DEFAULT_CANDIDATE_MIN_Y_RATIO = 0.05
DEFAULT_CANDIDATE_MAX_Y_RATIO = 0.95
DEFAULT_CANDIDATE_MAX_AXIS_RATIO = 2.8
DEFAULT_CANDIDATE_MAX_JUMP_RATIO = 0.35
DEFAULT_CANDIDATE_MIN_DIAMETER_RATIO = 0.07
DEFAULT_CANDIDATE_STABILITY_FRAMES = 3
DEFAULT_TRACK_LOST_FRAMES = 8


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _ellipse_extents(axis_width: float, axis_height: float, angle: float) -> tuple[float, float]:
    """Return the axis-aligned half extents of a rotated ellipse."""
    half_width = max(0.0, float(axis_width)) / 2.0
    half_height = max(0.0, float(axis_height)) / 2.0
    radians = math.radians(float(angle))
    cosine = abs(math.cos(radians))
    sine = abs(math.sin(radians))
    return (
        math.sqrt((half_width * cosine) ** 2 + (half_height * sine) ** 2),
        math.sqrt((half_width * sine) ** 2 + (half_height * cosine) ** 2),
    )


def pupil_candidate_quality(
    *,
    frame_width: int,
    frame_height: int,
    center_x: float | None,
    center_y: float | None,
    axis_width: float | None,
    axis_height: float | None,
    diameter: float | None,
    angle: float = 0.0,
    previous: Mapping[str, Any] | None = None,
    min_y_ratio: float = DEFAULT_CANDIDATE_MIN_Y_RATIO,
    max_y_ratio: float = DEFAULT_CANDIDATE_MAX_Y_RATIO,
    max_axis_ratio: float = DEFAULT_CANDIDATE_MAX_AXIS_RATIO,
    max_jump_ratio: float = DEFAULT_CANDIDATE_MAX_JUMP_RATIO,
    min_diameter_ratio: float = DEFAULT_CANDIDATE_MIN_DIAMETER_RATIO,
) -> dict[str, Any]:
    """Classify one Detector2D candidate without changing its confidence.

    Detector2D returns one best ellipse.  A high score alone is not enough to
    distinguish a pupil from an eyelid/eyebrow edge, especially during camera
    adjustment.  This helper checks full ellipse exposure, conservative shape
    limits, and (when a track exists) frame-to-frame displacement.  The raw
    candidate is still retained for CSV audit; callers decide whether the
    candidate is usable for guidance and the recording gate.
    """
    result: dict[str, Any] = {
        "ok": False,
        "reason": "missing_geometry",
        "axis_ratio": None,
        "temporal_distance": None,
        "size_ratio": None,
        "ellipse_exposed": False,
    }
    try:
        width = int(frame_width)
        height = int(frame_height)
    except (TypeError, ValueError, OverflowError):
        return result | {"reason": "invalid_frame"}
    if width <= 0 or height <= 0:
        return result | {"reason": "invalid_frame"}
    if center_x is None or center_y is None or axis_width is None or axis_height is None:
        return result
    try:
        finite_values = [
            float(center_x),
            float(center_y),
            float(axis_width),
            float(axis_height),
            float(angle),
        ]
        candidate_diameter = 0.0 if diameter is None else float(diameter)
    except (TypeError, ValueError, OverflowError):
        return result
    if not all(math.isfinite(value) for value in (*finite_values, candidate_diameter)):
        return result
    cx, cy, aw, ah, candidate_angle = finite_values
    if aw <= 0.0 or ah <= 0.0:
        return result
    if not (0.0 <= cx < width and 0.0 <= cy < height):
        return result | {"reason": "center_outside_frame"}

    major = max(aw, ah)
    minor = min(aw, ah)
    axis_ratio = major / max(minor, 1e-6)
    result["axis_ratio"] = round(axis_ratio, 4)
    if axis_ratio > max(1.01, float(max_axis_ratio)):
        return result | {"reason": "ellipse_too_elongated"}

    # Very small high-confidence blobs are a frequent eyelash/specular
    # reflection false positive.  Scale the floor with the frame's shorter
    # dimension so the same policy works for 320x240 and higher resolutions.
    # The value is intentionally configurable because lens distance and sensor
    # crop differ between sites.
    minimum_diameter = max(1.0, min(float(width), float(height)) * float(min_diameter_ratio))
    if major < minimum_diameter:
        return result | {"reason": "pupil_too_small"}

    # A Detector2D ellipse that extends outside the frame is usually a partial
    # lid/brow contour.  Requiring full exposure matches the clinical intent:
    # the confidence gate should be based on an actually visible pupil.
    extent_x, extent_y = _ellipse_extents(aw, ah, candidate_angle)
    exposed = (
        cx - extent_x >= 0.0
        and cy - extent_y >= 0.0
        and cx + extent_x <= float(width - 1)
        and cy + extent_y <= float(height - 1)
    )
    result["ellipse_exposed"] = exposed
    if not exposed:
        return result | {"reason": "pupil_not_fully_exposed"}

    # Keep the sanity band deliberately wider than the operator guide.  The
    # guide is visual-only; this band only rejects detections at the extreme
    # image border where an eyebrow/eyelid contour is much more likely.
    y_ratio = cy / float(height)
    if y_ratio < float(min_y_ratio) or y_ratio > float(max_y_ratio):
        return result | {"reason": "candidate_outside_eye_band"}

    # A long, mostly horizontal contour in the upper part of an eye image is a
    # common eyebrow/upper-lid false positive.  Round or vertically elongated
    # pupils remain eligible, including when the subject looks upward.
    if (
        y_ratio < 0.24
        and aw >= ah * 1.8
        and axis_ratio >= 1.8
    ):
        return result | {"reason": "upper_lid_or_brow_candidate"}

    if candidate_diameter > 0.0:
        diameter_difference = abs(candidate_diameter - major)
        if diameter_difference > max(4.0, major * 0.60):
            return result | {"reason": "diameter_mismatch"}

    if isinstance(previous, Mapping):
        previous_x = _finite_float(previous.get("x"), float("nan"))
        previous_y = _finite_float(previous.get("y"), float("nan"))
        if math.isfinite(previous_x) and math.isfinite(previous_y):
            distance = math.hypot((cx - previous_x) / float(width), (cy - previous_y) / float(height))
            result["temporal_distance"] = round(distance, 5)
            if distance > max(0.05, float(max_jump_ratio)):
                return result | {"reason": "temporal_jump"}
        previous_diameter = _finite_float(previous.get("diameter"), 0.0)
        if previous_diameter <= 0.0:
            previous_diameter = max(_finite_float(previous.get("axis_width")), _finite_float(previous.get("axis_height")))
        if previous_diameter > 0.0 and candidate_diameter > 0.0:
            size_ratio = max(candidate_diameter, previous_diameter) / max(min(candidate_diameter, previous_diameter), 1e-6)
            result["size_ratio"] = round(size_ratio, 4)
            if size_ratio > 2.5:
                return result | {"reason": "pupil_size_jump"}

    result["ok"] = True
    result["reason"] = None
    return result


def _rejection_guidance(camera_name: str, reason: str) -> str:
    """Return non-directional guidance for a rejected detector candidate."""
    eye_label = {"right_eye": "右眼", "left_eye": "左眼"}.get(camera_name, camera_name)
    if reason in {"upper_lid_or_brow_candidate", "ellipse_too_elongated", "pupil_too_small"}:
        return f"{eye_label}检测到疑似眼睑/眉毛/睫毛或反光小轮廓，正在重新寻找瞳孔；请保持眼睛睁开并调整焦距/红外照明"
    if reason in {"temporal_jump", "pupil_size_jump"}:
        return f"{eye_label}检测位置变化过快，正在重新确认瞳孔；请保持相机稳定，不要大幅移动"
    if reason == "pupil_not_fully_exposed":
        return f"{eye_label}瞳孔未完整暴露，请调整相机距离、角度或焦距"
    if reason == "candidate_outside_eye_band":
        return f"{eye_label}候选位于画面边缘，正在重新寻找瞳孔；请把眼眶移入中央范围"
    if reason == "diameter_mismatch":
        return f"{eye_label}瞳孔轮廓不稳定，正在重新寻找；请检查焦距和红外照明"
    return f"{eye_label}未锁定可靠瞳孔，正在重新寻找；请保持相机稳定"


def _empty_detection(
    *,
    state: str = "starting",
    error: str | None = None,
    alignment_hold_seconds: float = 1.5,
) -> dict[str, Any]:
    return {
        "state": state,
        "valid": False,
        "x": None,
        "y": None,
        "radius": None,
        "diameter": None,
        "axis_width": None,
        "axis_height": None,
        "angle": None,
        "confidence": 0.0,
        "latency_ms": None,
        "detector_fps": 0.0,
        "valid_rate": 0.0,
        "sequence": 0,
        "capture_timestamp_ns": 0,
        "age_ms": None,
        "frame_width": 0,
        "frame_height": 0,
        "geometry_ok": False,
        "quality_ok": False,
        "quality_state": "searching",
        "rejected_reason": error,
        "temporal_stable": False,
        "temporal_distance": None,
        "center_ok": False,
        "direction": "search",
        "guidance": None,
        "alignment": {
            "state": state,
            "ready": False,
            "progress": 0.0,
            "hold_seconds": 0.0,
            "required_hold_seconds": float(alignment_hold_seconds),
            "center_ok": False,
            "quality_ok": False,
            "rejected_reason": error,
            "temporal_stable": False,
            "direction": "search",
            "guidance": None,
        },
        "error": error,
    }


def _alignment_guidance(
    camera_name: str,
    *,
    width: int,
    height: int,
    center_x: float | None,
    center_y: float | None,
    confidence: float,
    threshold: float,
    geometry_ok: bool,
    error: str | None,
    center_tolerance: float,
    rejection_reason: str | None = None,
) -> str:
    """Return a short operator instruction for the live alignment step."""
    eye_label = {"right_eye": "右眼", "left_eye": "左眼"}.get(camera_name, camera_name)
    if error:
        return f"{eye_label}瞳孔算法异常，请检查安装或查看日志"
    if rejection_reason:
        return _rejection_guidance(camera_name, rejection_reason)
    if not geometry_ok or width <= 0 or height <= 0 or center_x is None or center_y is None:
        return f"请将{eye_label}整只瞳孔移入画面，调整相机位置或焦距"

    dx = (float(center_x) / float(width)) - 0.5
    dy = (float(center_y) / float(height)) - 0.5
    directions: list[str] = []
    if dx < -center_tolerance:
        directions.append("左")
    elif dx > center_tolerance:
        directions.append("右")
    if dy < -center_tolerance:
        directions.append("上")
    elif dy > center_tolerance:
        directions.append("下")
    if directions:
        direction = "、".join(directions)
        return f"{eye_label}瞳孔偏{direction}，请将相机向{direction}微调"
    if confidence < threshold:
        return (
            f"{eye_label}已看到瞳孔，请微调焦距/曝光并保持稳定 "
            f"（置信度 {confidence:.2f}，目标 ≥ {threshold:.2f}）"
        )
    return f"{eye_label}瞳孔已居中，请保持不动，正在确认稳定性"


def _alignment_direction(
    *,
    width: int,
    height: int,
    center_x: float | None,
    center_y: float | None,
    geometry_ok: bool,
    center_tolerance: float,
) -> str:
    """Return the observed pupil offset as a stable, UI-friendly token.

    The token describes where the pupil appears in the image (for example
    ``left_down``), not a confidence result.  The Qt UI uses it to draw an
    arrow while the backend continues to use only the detector geometry and
    confidence for validity.
    """
    if not geometry_ok or width <= 0 or height <= 0 or center_x is None or center_y is None:
        return "search"
    dx = (float(center_x) / float(width)) - 0.5
    dy = (float(center_y) / float(height)) - 0.5
    horizontal = "left" if dx < -center_tolerance else "right" if dx > center_tolerance else ""
    vertical = "up" if dy < -center_tolerance else "down" if dy > center_tolerance else ""
    if horizontal and vertical:
        return f"{horizontal}_{vertical}"
    return horizontal or vertical or "center"


def guide_rect_pixels(
    width: int,
    height: int,
    *,
    margin_x: float = 0.16,
    margin_y: float = 0.20,
) -> tuple[int, int, int, int]:
    """Return a large, centered operator guide rectangle in pixels.

    This rectangle is deliberately separate from ``center_ok`` and is never
    consulted by the detector or recording gate.
    """
    width = max(1, int(width))
    height = max(1, int(height))
    margin_x = max(0.0, min(0.44, float(margin_x)))
    margin_y = max(0.0, min(0.44, float(margin_y)))
    left = int(round(width * margin_x))
    top = int(round(height * margin_y))
    right = max(left + 1, int(round(width * (1.0 - margin_x))) - 1)
    bottom = max(top + 1, int(round(height * (1.0 - margin_y))) - 1)
    return left, top, right, bottom


class PupilDetectorService:
    """One independent, latest-frame-only detector for one eye camera."""

    def __init__(
        self,
        camera_name: str,
        eye_id: int,
        owner: SharedFrameOwner,
        *,
        detector_fps: int = 30,
        confidence_threshold: float = 0.6,
        alignment_hold_seconds: float = 1.5,
        alignment_center_tolerance: float = 0.30,
        alignment_max_age_seconds: float = 0.75,
        candidate_min_y_ratio: float = DEFAULT_CANDIDATE_MIN_Y_RATIO,
        candidate_max_y_ratio: float = DEFAULT_CANDIDATE_MAX_Y_RATIO,
        candidate_max_axis_ratio: float = DEFAULT_CANDIDATE_MAX_AXIS_RATIO,
        candidate_max_jump_ratio: float = DEFAULT_CANDIDATE_MAX_JUMP_RATIO,
        candidate_min_diameter_ratio: float = DEFAULT_CANDIDATE_MIN_DIAMETER_RATIO,
        candidate_stability_frames: int = DEFAULT_CANDIDATE_STABILITY_FRAMES,
    ) -> None:
        self.camera_name = str(camera_name)
        self.eye_id = int(eye_id)
        self.owner = owner
        self.detector_fps = max(1, min(60, int(detector_fps)))
        self.confidence_threshold = max(0.0, min(1.0, float(confidence_threshold)))
        self.alignment_hold_seconds = max(0.1, float(alignment_hold_seconds))
        self.alignment_center_tolerance = max(0.05, min(0.49, float(alignment_center_tolerance)))
        self.alignment_max_age_seconds = max(0.1, float(alignment_max_age_seconds))
        self.candidate_min_y_ratio = max(0.0, min(0.45, float(candidate_min_y_ratio)))
        self.candidate_max_y_ratio = max(self.candidate_min_y_ratio + 0.05, min(1.0, float(candidate_max_y_ratio)))
        self.candidate_max_axis_ratio = max(1.05, min(10.0, float(candidate_max_axis_ratio)))
        self.candidate_max_jump_ratio = max(0.05, min(1.0, float(candidate_max_jump_ratio)))
        self.candidate_min_diameter_ratio = max(0.0, min(0.5, float(candidate_min_diameter_ratio)))
        self.candidate_stability_frames = max(1, min(10, int(candidate_stability_frames)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._detector: Any | None = None
        self._latest: dict[str, Any] = _empty_detection(
            alignment_hold_seconds=self.alignment_hold_seconds,
        )
        self._last_valid: dict[str, Any] | None = None
        self._last_valid_monotonic: float | None = None
        self._last_sequence = 0
        self._last_update_monotonic: float | None = None
        self._alignment_since_monotonic: float | None = None
        self._track: dict[str, float] | None = None
        self._pending_track: dict[str, float] | None = None
        self._pending_track_count = 0
        self._track_misses = 0
        self._recent_detection_times: deque[float] = deque(maxlen=120)
        self._recent_valid: deque[bool] = deque(maxlen=120)
        self._recent_latency_ms: deque[float] = deque(maxlen=120)
        self._session_id: str | None = None
        self._session_path: Path | None = None
        self._csv_handle: Any | None = None
        self._csv_writer: csv.DictWriter | None = None
        self._csv_rows_since_flush = 0
        self._session_samples = 0
        self._session_valid = 0
        self._session_latencies: deque[float] = deque(maxlen=20_000)
        self._session_alignment_start: dict[str, Any] | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._latest = _empty_detection(
                state="starting",
                alignment_hold_seconds=self.alignment_hold_seconds,
            )
            self._last_sequence = 0
            self._last_update_monotonic = None
            self._last_valid = None
            self._last_valid_monotonic = None
            self._alignment_since_monotonic = None
            self._track = None
            self._pending_track = None
            self._pending_track_count = 0
            self._track_misses = 0
            self._thread = threading.Thread(
                target=self._run,
                name=f"clinical-pupil-{self.camera_name}",
                daemon=True,
            )
            self._thread.start()

    def start_session(self, session_id: str, session_path: str | Path) -> Path:
        """Create the per-eye CSV before recording starts.

        The detector may still be warming up; a header-only CSV is useful
        evidence that the detector was enabled for the session.
        """
        path = Path(session_path).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        output = path / f"{self.camera_name}_pupil.csv"
        with self._lock:
            self._close_csv_locked()
            handle = output.open("w", encoding="utf-8-sig", newline="")
            writer = csv.DictWriter(handle, fieldnames=PUPIL_CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            handle.flush()
            self._csv_handle = handle
            self._csv_writer = writer
            self._csv_rows_since_flush = 0
            self._session_id = str(session_id)
            self._session_path = path
            self._session_samples = 0
            self._session_valid = 0
            self._session_latencies.clear()
            self._session_alignment_start = self._alignment_status_locked()
        return output

    def stop_session(self) -> dict[str, Any]:
        with self._lock:
            self._close_csv_locked()
            summary = self._session_summary_locked()
            self._session_id = None
            self._session_path = None
            self._session_alignment_start = None
            return summary

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            self._close_csv_locked()
            self._thread = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            value = dict(self._latest)
            if self._last_update_monotonic is not None:
                value["age_ms"] = round(max(0.0, time.monotonic() - self._last_update_monotonic) * 1000.0, 1)
            value["detector_fps"] = self._rate_locked()
            value["valid_rate"] = self._valid_rate_locked()
            value["session_samples"] = self._session_samples
            value["session_valid"] = self._session_valid
            value["alignment"] = self._alignment_status_locked()
            return value

    def summary(self) -> dict[str, Any]:
        with self._lock:
            summary = self._session_summary_locked()
            summary["alignment"] = self._alignment_status_locked()
            summary["confidence_threshold"] = self.confidence_threshold
            return summary

    def reset_alignment(self, *, reason: str = "session_reset") -> None:
        """Forget the previous subject's track and alignment hold.

        A completed session must never lend its ``ready`` state to the next
        subject.  The latest raw detector geometry is retained for preview,
        but it is made ineligible until a fresh candidate is observed.
        """
        with self._lock:
            self._track = None
            self._pending_track = None
            self._pending_track_count = 0
            self._track_misses = 0
            self._alignment_since_monotonic = None
            self._last_valid = None
            self._last_valid_monotonic = None
            if self._latest:
                self._latest["valid"] = False
                self._latest["quality_ok"] = False
                self._latest["quality_state"] = "searching"
                self._latest["rejected_reason"] = str(reason or "session_reset")
                self._latest["temporal_stable"] = False
                self._latest["temporal_distance"] = None
                self._latest["state"] = "ready"
                self._latest["center_ok"] = False
                self._latest["alignment_eligible"] = False
                self._latest["direction"] = "search"
                self._latest["guidance"] = "请重新确认双眼瞳孔后再开始采集"
                self._latest["alignment"] = self._alignment_status_locked()

    def overlay(
        self,
        frame: np.ndarray,
        *,
        guide_rect: tuple[float, float, float, float] | None = None,
    ) -> np.ndarray:
        """Return a BGR preview with pupil annotation and an optional guide.

        ``guide_rect`` is normalized ``(left, top, right, bottom)``.  It is
        intentionally rendered after the image conversion and has no effect
        on detector validity or alignment state.
        """
        if frame.ndim == 2:
            preview = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        elif frame.ndim == 3 and frame.shape[2] == 1:
            preview = cv2.cvtColor(frame[:, :, 0], cv2.COLOR_GRAY2BGR)
        elif frame.ndim == 3 and frame.shape[2] == 4:
            preview = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        else:
            preview = frame.copy()

        with self._lock:
            pupil = dict(self._latest)
            if self._last_update_monotonic is not None:
                pupil["age_ms"] = max(0.0, time.monotonic() - self._last_update_monotonic) * 1000.0
            held = False
            if not pupil.get("valid") and self._last_valid is not None and self._last_valid_monotonic is not None:
                valid_age_ms = max(0.0, time.monotonic() - self._last_valid_monotonic) * 1000.0
                if valid_age_ms <= 100.0:
                    pupil = dict(self._last_valid)
                    pupil["age_ms"] = valid_age_ms
                    held = True
        valid = bool(pupil.get("valid"))
        age_ms = _finite_float(pupil.get("age_ms"), 9999.0)
        quality_ok = bool(pupil.get("quality_ok"))
        if valid and quality_ok and age_ms <= 250.0:
            center = (int(round(_finite_float(pupil.get("x")))), int(round(_finite_float(pupil.get("y")))))
            axis_width = max(1, int(round(_finite_float(pupil.get("axis_width"), 2.0) / 2.0)))
            axis_height = max(1, int(round(_finite_float(pupil.get("axis_height"), 2.0) / 2.0)))
            angle = _finite_float(pupil.get("angle"))
            cv2.ellipse(preview, center, (axis_width, axis_height), angle, 0, 360, (0, 220, 255), 2, cv2.LINE_AA)
            cv2.circle(preview, center, 3, (70, 255, 80), -1, cv2.LINE_AA)
            cv2.line(preview, (center[0] - 7, center[1]), (center[0] + 7, center[1]), (70, 255, 80), 1, cv2.LINE_AA)
            cv2.line(preview, (center[0], center[1] - 7), (center[0], center[1] + 7), (70, 255, 80), 1, cv2.LINE_AA)
            label = f"PUPIL{'*' if held else ''} {float(pupil.get('confidence') or 0.0):.2f}  {float(pupil.get('latency_ms') or 0.0):.1f}ms"
            color = (70, 255, 80)
        else:
            label = "PUPIL --"
            color = (80, 100, 255)
        cv2.putText(preview, label, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.48, color, 1, cv2.LINE_AA)
        if guide_rect is not None:
            try:
                left_n, top_n, right_n, bottom_n = (float(value) for value in guide_rect)
                height, width = preview.shape[:2]
                left = max(0, min(width - 1, int(round(left_n * width))))
                top = max(0, min(height - 1, int(round(top_n * height))))
                right = max(left + 1, min(width - 1, int(round(right_n * width))))
                bottom = max(top + 1, min(height - 1, int(round(bottom_n * height))))
                cv2.rectangle(preview, (left, top), (right, bottom), (255, 190, 50), 1, cv2.LINE_AA)
            except (TypeError, ValueError, OverflowError):
                # A preview aid must never make the detector/preview fail.
                pass
        return preview

    def _run(self) -> None:
        try:
            # This is the detector used by apps/video-eye-detect and the
            # binocular calibration runtime, rather than a second heuristic.
            from pupil_detectors import Detector2D

            cv2.setNumThreads(1)
            detector = Detector2D()
            with self._lock:
                self._detector = detector
                self._latest["state"] = "ready"
                self._latest["error"] = None
        except Exception as error:  # pragma: no cover - depends on native wheel
            with self._lock:
                self._latest = _empty_detection(
                    state="error",
                    error=f"瞳孔算法不可用: {error}",
                    alignment_hold_seconds=self.alignment_hold_seconds,
                )
            return

        period = 1.0 / float(self.detector_fps)
        next_run = time.perf_counter()
        while not self._stop.is_set():
            now = time.perf_counter()
            if now < next_run:
                self._stop.wait(min(next_run - now, 0.02))
                continue
            next_run = max(next_run + period, now + period)
            snapshot = self.owner.read_latest()
            if snapshot is None or snapshot.sequence == self._last_sequence:
                continue
            self._last_sequence = snapshot.sequence
            self._detect_snapshot(snapshot, detector)

    def _track_miss_locked(self) -> None:
        """Age the candidate track without allowing stale geometry to guide."""
        self._track_misses += 1
        self._pending_track = None
        self._pending_track_count = 0
        if self._track_misses >= DEFAULT_TRACK_LOST_FRAMES:
            self._track = None
            self._track_misses = 0

    def _track_candidate_locked(
        self,
        *,
        frame_width: int,
        frame_height: int,
        center_x: float,
        center_y: float,
        axis_width: float,
        axis_height: float,
        diameter: float,
        temporal_distance: float | None,
        size_ratio: float | None,
    ) -> tuple[bool, bool, str | None]:
        """Update the short pupil track and debounce large candidate jumps.

        Detector2D can return a convincing ellipse on a lid or eyebrow for a
        single frame.  A candidate that jumps a large fraction of the frame is
        therefore held out of guidance until the new location is observed for
        a few consecutive detector samples.  Normal pupil motion updates the
        track immediately.
        """
        candidate = {
            "x": float(center_x),
            "y": float(center_y),
            "axis_width": float(axis_width),
            "axis_height": float(axis_height),
            "diameter": float(diameter),
        }
        self._track_misses = 0
        if self._track is None:
            self._track = candidate
            self._pending_track = None
            self._pending_track_count = 0
            return True, True, None

        is_jump = (
            temporal_distance is not None and temporal_distance > self.candidate_max_jump_ratio
        ) or (size_ratio is not None and size_ratio > 2.5)
        if not is_jump:
            self._track = candidate
            self._pending_track = None
            self._pending_track_count = 0
            return True, True, None

        # Keep a separate pending location.  If the same new location persists
        # long enough, accept it as a genuine eye movement; otherwise keep the
        # previous track and suppress directional guidance.
        pending = self._pending_track
        close_to_pending = False
        if pending is not None:
            pending_distance = math.hypot(
                (candidate["x"] - pending["x"]) / max(1.0, float(frame_width)),
                (candidate["y"] - pending["y"]) / max(1.0, float(frame_height)),
            )
            # A five-pixel tolerance at the supported 320x240 mode, scaled to
            # larger or smaller frames, is enough to identify the same contour.
            pixel_tolerance = 5.0 / max(1.0, min(float(frame_width), float(frame_height)))
            close_to_pending = pending_distance <= max(0.01, pixel_tolerance)
        if close_to_pending:
            self._pending_track_count += 1
        else:
            self._pending_track = candidate
            self._pending_track_count = 1
        if self._pending_track_count >= self.candidate_stability_frames:
            self._track = candidate
            self._pending_track = None
            self._pending_track_count = 0
            return True, True, None
        return False, False, "temporal_jump"

    def _detect_snapshot(self, snapshot: FrameSnapshot, detector: Any) -> None:
        frame = snapshot.frame
        if frame.ndim == 2:
            gray = frame
        elif frame.ndim == 3 and frame.shape[2] == 1:
            gray = frame[:, :, 0]
        elif frame.ndim == 3 and frame.shape[2] == 3:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        elif frame.ndim == 3 and frame.shape[2] == 4:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
        else:
            return

        started_ns = time.perf_counter_ns()
        error: str | None = None
        raw: Mapping[str, Any] | None = None
        try:
            candidate = detector.detect(gray)
            if isinstance(candidate, Mapping):
                raw = candidate
            else:
                error = "Detector2D returned a non-object result"
        except Exception as caught:  # keep the acquisition loop alive on one bad frame
            error = str(caught)
        finished_ns = time.perf_counter_ns()
        latency_ms = max(0.0, (finished_ns - int(snapshot.timestamp_ns)) / 1_000_000.0)

        confidence = _finite_float(raw.get("confidence") if raw else 0.0)
        ellipse = raw.get("ellipse") if raw else None
        center = ellipse.get("center") if isinstance(ellipse, Mapping) else None
        axes = ellipse.get("axes") if isinstance(ellipse, Mapping) else None
        angle = _finite_float(ellipse.get("angle") if isinstance(ellipse, Mapping) else 0.0)
        geometry_ok = (
            isinstance(center, (tuple, list))
            and len(center) == 2
            and isinstance(axes, (tuple, list))
            and len(axes) == 2
            and all(math.isfinite(_finite_float(value, float("nan"))) for value in (*center, *axes))
            and _finite_float(axes[0]) > 0.0
            and _finite_float(axes[1]) > 0.0
            and 0.0 <= _finite_float(center[0]) < gray.shape[1]
            and 0.0 <= _finite_float(center[1]) < gray.shape[0]
        )
        center_x = _finite_float(center[0]) if geometry_ok else None
        center_y = _finite_float(center[1]) if geometry_ok else None
        axis_width = _finite_float(axes[0]) if geometry_ok else None
        axis_height = _finite_float(axes[1]) if geometry_ok else None
        diameter = _finite_float(raw.get("diameter") if raw else 0.0) if geometry_ok else None
        if geometry_ok and diameter is not None and diameter <= 0.0:
            diameter = max(axis_width or 0.0, axis_height or 0.0)
        radius = (diameter / 2.0) if diameter is not None and diameter > 0.0 else None
        frame_height, frame_width = gray.shape[:2]

        # Evaluate the candidate against the lightweight acceptance layer and
        # update the short track while holding the service lock.  The raw
        # Detector2D geometry remains in the status/CSV even when rejected.
        quality: dict[str, Any]
        temporal_stable = False
        rejection_reason: str | None = None
        with self._lock:
            previous_track = dict(self._track) if self._track is not None else None
            quality = pupil_candidate_quality(
                frame_width=frame_width,
                frame_height=frame_height,
                center_x=center_x,
                center_y=center_y,
                axis_width=axis_width,
                axis_height=axis_height,
                diameter=diameter,
                angle=angle,
                previous=previous_track,
                min_y_ratio=self.candidate_min_y_ratio,
                max_y_ratio=self.candidate_max_y_ratio,
                max_axis_ratio=self.candidate_max_axis_ratio,
                max_jump_ratio=self.candidate_max_jump_ratio,
                min_diameter_ratio=self.candidate_min_diameter_ratio,
            )
            rejection_reason = quality.get("reason")
            trackable_jump = quality.get("reason") in {"temporal_jump", "pupil_size_jump"}
            if geometry_ok and (quality.get("ok") or trackable_jump):
                accepted, temporal_stable, track_reason = self._track_candidate_locked(
                    frame_width=frame_width,
                    frame_height=frame_height,
                    center_x=float(center_x),
                    center_y=float(center_y),
                    axis_width=float(axis_width),
                    axis_height=float(axis_height),
                    diameter=float(diameter or 0.0),
                    temporal_distance=quality.get("temporal_distance"),
                    size_ratio=quality.get("size_ratio"),
                )
                if not accepted:
                    quality["ok"] = False
                    quality["reason"] = track_reason or "temporal_jump"
                    rejection_reason = quality["reason"]
                else:
                    quality["ok"] = True
                    quality["reason"] = None
                    rejection_reason = None
            elif geometry_ok:
                # Shape/exposure failures are not allowed to seed the track;
                # after a few such frames the old location is forgotten.
                self._track_miss_locked()
            else:
                self._track_miss_locked()

        quality_ok = bool(geometry_ok and quality.get("ok"))
        if error:
            rejection_reason = "detector_error"
        elif not geometry_ok and not rejection_reason:
            rejection_reason = "missing_geometry"
        valid = bool(quality_ok and confidence >= self.confidence_threshold)
        center_ok = bool(
            quality_ok
            and center_x is not None
            and center_y is not None
            and abs((center_x / max(1.0, float(frame_width))) - 0.5) <= self.alignment_center_tolerance
            and abs((center_y / max(1.0, float(frame_height))) - 0.5) <= self.alignment_center_tolerance
        )
        direction = _alignment_direction(
            width=frame_width,
            height=frame_height,
            center_x=center_x,
            center_y=center_y,
            geometry_ok=quality_ok,
            center_tolerance=self.alignment_center_tolerance,
        )
        guidance = _alignment_guidance(
            self.camera_name,
            width=frame_width,
            height=frame_height,
            center_x=center_x,
            center_y=center_y,
            confidence=confidence,
            threshold=self.confidence_threshold,
            geometry_ok=quality_ok,
            error=error,
            center_tolerance=self.alignment_center_tolerance,
            rejection_reason=rejection_reason,
        )
        alignment_eligible = bool(valid and center_ok and temporal_stable)
        update_monotonic = time.monotonic()

        with self._lock:
            if alignment_eligible:
                if self._alignment_since_monotonic is None:
                    self._alignment_since_monotonic = update_monotonic
            else:
                self._alignment_since_monotonic = None
            self._recent_detection_times.append(time.monotonic())
            self._recent_valid.append(valid)
            self._recent_latency_ms.append(latency_ms)
            self._last_update_monotonic = update_monotonic
            self._latest = {
                "state": "error" if error else "ready",
                "valid": valid,
                "x": center_x,
                "y": center_y,
                "radius": radius,
                "diameter": diameter,
                "axis_width": axis_width,
                "axis_height": axis_height,
                "angle": angle if geometry_ok else None,
                "confidence": round(confidence, 4),
                "latency_ms": round(latency_ms, 3),
                "detector_fps": self._rate_locked(),
                "valid_rate": self._valid_rate_locked(),
                "sequence": int(snapshot.sequence),
                "capture_timestamp_ns": int(snapshot.timestamp_ns),
                "age_ms": 0.0,
                "frame_width": frame_width,
                "frame_height": frame_height,
                "geometry_ok": geometry_ok,
                "quality_ok": quality_ok,
                "quality_state": (
                    "error"
                    if error
                    else "accepted" if quality_ok else "rejected"
                ),
                "rejected_reason": rejection_reason,
                "temporal_stable": temporal_stable,
                "temporal_distance": quality.get("temporal_distance"),
                "center_ok": center_ok,
                "alignment_eligible": alignment_eligible,
                "direction": direction,
                "guidance": guidance,
                "error": error,
            }
            self._latest["alignment"] = self._alignment_status_locked(now=update_monotonic)
            if valid:
                self._last_valid = dict(self._latest)
                self._last_valid_monotonic = self._last_update_monotonic
            self._write_csv_locked(
                {
                    "camera": self.camera_name,
                    "eye_id": self.eye_id,
                    "sequence": int(snapshot.sequence),
                    "capture_timestamp_ns": int(snapshot.timestamp_ns),
                    "detection_timestamp_ns": int(finished_ns),
                    "latency_ms": round(latency_ms, 3),
                    "valid": int(valid),
                    "confidence": round(confidence, 6),
                    "center_x": center_x,
                    "center_y": center_y,
                    "radius": radius,
                    "diameter": diameter,
                    "axis_width": axis_width,
                    "axis_height": axis_height,
                    "angle": angle if geometry_ok else None,
                    "detector_fps": self._rate_locked(),
                    "quality_ok": int(quality_ok),
                    "rejected_reason": rejection_reason or "",
                    "temporal_distance": quality.get("temporal_distance"),
                    "error": error or "",
                },
                valid=valid,
                latency_ms=latency_ms,
            )

    def _write_csv_locked(self, row: Mapping[str, Any], *, valid: bool, latency_ms: float) -> None:
        if self._csv_writer is None or self._csv_handle is None:
            return
        self._csv_writer.writerow(dict(row))
        self._csv_rows_since_flush += 1
        self._session_samples += 1
        self._session_valid += int(valid)
        self._session_latencies.append(float(latency_ms))
        if self._csv_rows_since_flush >= 30:
            self._csv_handle.flush()
            self._csv_rows_since_flush = 0

    def _close_csv_locked(self) -> None:
        if self._csv_handle is not None:
            try:
                self._csv_handle.flush()
                self._csv_handle.close()
            except OSError:
                pass
        self._csv_handle = None
        self._csv_writer = None
        self._csv_rows_since_flush = 0

    def _alignment_status_locked(self, *, now: float | None = None) -> dict[str, Any]:
        """Compute the live, time-based alignment gate for this eye."""
        now = time.monotonic() if now is None else float(now)
        latest = self._latest
        last_update = self._last_update_monotonic
        age_ms = (
            max(0.0, now - last_update) * 1000.0
            if last_update is not None
            else None
        )
        fresh = age_ms is not None and age_ms <= self.alignment_max_age_seconds * 1000.0
        eligible = bool(latest.get("alignment_eligible") and fresh)
        if not eligible:
            self._alignment_since_monotonic = None
        hold_seconds = (
            max(0.0, now - self._alignment_since_monotonic)
            if eligible and self._alignment_since_monotonic is not None
            else 0.0
        )
        ready = bool(eligible and hold_seconds >= self.alignment_hold_seconds)
        progress = min(1.0, hold_seconds / self.alignment_hold_seconds) if eligible else 0.0
        latest_state = str(latest.get("state") or "starting")
        if latest_state == "error":
            state = "error"
        elif ready:
            state = "ready"
        elif eligible:
            state = "holding"
        elif latest.get("geometry_ok") and latest.get("quality_ok"):
            state = "adjust"
        elif latest.get("geometry_ok"):
            state = "searching"
        else:
            state = "searching" if latest_state == "ready" else latest_state

        guidance = str(latest.get("guidance") or "正在等待瞳孔检测")
        if ready:
            eye_label = {"right_eye": "右眼", "left_eye": "左眼"}.get(self.camera_name, self.camera_name)
            guidance = f"{eye_label}对准完成，请保持不动，可以开始采集"
        elif eligible:
            remaining = max(0.0, self.alignment_hold_seconds - hold_seconds)
            guidance = f"瞳孔置信度已达标，请保持不动 {remaining:.1f} 秒"
        elif not fresh and latest_state == "ready":
            guidance = "检测帧已过期，请保持相机稳定并重新对准"
        elif latest_state == "error" and latest.get("error"):
            guidance = str(latest.get("error"))
        direction = str(latest.get("direction") or "search")
        if ready:
            direction = "ready"
        elif eligible:
            direction = "holding"
        return {
            "state": state,
            "ready": ready,
            "progress": round(progress, 3),
            "hold_seconds": round(min(hold_seconds, self.alignment_hold_seconds), 3),
            "required_hold_seconds": round(self.alignment_hold_seconds, 3),
            "quality_ok": bool(latest.get("quality_ok")),
            "rejected_reason": latest.get("rejected_reason"),
            "temporal_stable": bool(latest.get("temporal_stable")),
            "center_ok": bool(latest.get("center_ok")),
            "direction": direction,
            "fresh": fresh,
            "age_ms": None if age_ms is None else round(age_ms, 1),
            "confidence": round(_finite_float(latest.get("confidence")), 4),
            "threshold": round(self.confidence_threshold, 4),
            "guidance": guidance,
        }

    def _rate_locked(self) -> float:
        now = time.monotonic()
        while self._recent_detection_times and now - self._recent_detection_times[0] > 2.0:
            self._recent_detection_times.popleft()
            if self._recent_valid:
                self._recent_valid.popleft()
            if self._recent_latency_ms:
                self._recent_latency_ms.popleft()
        if len(self._recent_detection_times) < 2:
            return float(len(self._recent_detection_times))
        span = max(0.001, self._recent_detection_times[-1] - self._recent_detection_times[0])
        return round((len(self._recent_detection_times) - 1) / span, 1)

    def _valid_rate_locked(self) -> float:
        if not self._recent_valid:
            return 0.0
        return round(100.0 * sum(self._recent_valid) / len(self._recent_valid), 1)

    def _session_summary_locked(self) -> dict[str, Any]:
        latency = list(self._session_latencies)
        p95 = 0.0
        if latency:
            p95 = round(float(np.percentile(np.asarray(latency, dtype=np.float64), 95)), 3)
        return {
            "samples": self._session_samples,
            "valid_samples": self._session_valid,
            "valid_rate": round(100.0 * self._session_valid / max(1, self._session_samples), 1),
            "detector_fps": self._rate_locked(),
            "latency_p95_ms": p95,
            "csv_path": str(self._session_path / f"{self.camera_name}_pupil.csv") if self._session_path else None,
            "alignment_at_start": dict(self._session_alignment_start or {}),
            "alignment_at_stop": self._alignment_status_locked(),
            "confidence_threshold": self.confidence_threshold,
        }
