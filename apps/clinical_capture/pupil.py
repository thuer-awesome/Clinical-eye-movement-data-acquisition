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
    "error",
)


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _empty_detection(*, state: str = "starting", error: str | None = None) -> dict[str, Any]:
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
        "error": error,
    }


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
    ) -> None:
        self.camera_name = str(camera_name)
        self.eye_id = int(eye_id)
        self.owner = owner
        self.detector_fps = max(1, min(60, int(detector_fps)))
        self.confidence_threshold = max(0.0, min(1.0, float(confidence_threshold)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._detector: Any | None = None
        self._latest: dict[str, Any] = _empty_detection()
        self._last_valid: dict[str, Any] | None = None
        self._last_valid_monotonic: float | None = None
        self._last_sequence = 0
        self._last_update_monotonic: float | None = None
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

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._latest = _empty_detection(state="starting")
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
        return output

    def stop_session(self) -> dict[str, Any]:
        with self._lock:
            self._close_csv_locked()
            summary = self._session_summary_locked()
            self._session_id = None
            self._session_path = None
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
            return value

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return self._session_summary_locked()

    def overlay(self, frame: np.ndarray) -> np.ndarray:
        """Return a BGR preview with the most recent valid pupil annotation."""
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
        if valid and age_ms <= 250.0:
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
                self._latest = _empty_detection(state="error", error=f"瞳孔算法不可用: {error}")
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
        valid = bool(geometry_ok and confidence >= self.confidence_threshold)
        center_x = _finite_float(center[0]) if geometry_ok else None
        center_y = _finite_float(center[1]) if geometry_ok else None
        axis_width = _finite_float(axes[0]) if geometry_ok else None
        axis_height = _finite_float(axes[1]) if geometry_ok else None
        diameter = _finite_float(raw.get("diameter") if raw else 0.0) if geometry_ok else None
        if geometry_ok and diameter is not None and diameter <= 0.0:
            diameter = max(axis_width or 0.0, axis_height or 0.0)
        radius = (diameter / 2.0) if diameter is not None and diameter > 0.0 else None

        with self._lock:
            self._recent_detection_times.append(time.monotonic())
            self._recent_valid.append(valid)
            self._recent_latency_ms.append(latency_ms)
            self._last_update_monotonic = time.monotonic()
            self._latest = {
                "state": "ready",
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
                "error": error,
            }
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
        }
