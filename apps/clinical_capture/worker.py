from __future__ import annotations

import csv
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import queue
import time
from typing import Any, Mapping

import cv2
import numpy as np

from apps.multicamera_capture.shared_frame import SharedFrameState, SharedFrameWriter

from .models import CameraConfig


BACKENDS = {
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
}
AUTO_EXPOSURE_VALUES = {"auto": 0.75, "manual": 0.25}


def capture_worker(
    camera_payload: Mapping[str, Any],
    frame_state: SharedFrameState,
    status_queue: Any,
    stop_event: Any,
    record_event: Any,
    record_directory_state: Any,
    simulate: bool,
    status_interval_seconds: float,
    record_quality: int = 95,
    read_failure_threshold: int = 3,
    reconnect_attempts: int = 3,
    reconnect_delay_seconds: float = 0.20,
) -> None:
    """Capture one camera in its own process.

    The worker owns the camera and the video encoder.  Only the newest frame is
    copied to shared memory for preview; every frame during recording is sent
    directly to the encoder, so a slow browser cannot create an unbounded queue.
    """
    spec = CameraConfig.from_mapping(camera_payload)
    capture: cv2.VideoCapture | None = None
    writer: cv2.VideoWriter | None = None
    metadata_handle: Any | None = None
    metadata_writer: csv.writer | None = None
    frame_writer: SharedFrameWriter | None = None
    failed = False
    recording = False
    recorded_frames = 0
    frame_sequence = 0
    recording_started_ns = 0
    recording_path: str | None = None
    recording_codec: str | None = None
    writer_is_color = True
    interval_started = time.perf_counter()
    interval_frames = 0
    quality_samples = 0
    black_frames = 0
    read_durations_ms: list[float] = []
    last_mean = 0.0
    last_std = 0.0
    consecutive_read_failures = 0
    reconnect_count = 0
    reconnect_attempts_total = 0
    last_reconnect_at: str | None = None
    transform_verified = False

    read_failure_threshold = max(1, min(20, int(read_failure_threshold)))
    reconnect_attempts = max(1, min(20, int(reconnect_attempts)))
    reconnect_delay_seconds = max(0.05, min(5.0, float(reconnect_delay_seconds)))

    try:
        cv2.setNumThreads(1)
        _send(status_queue, {"kind": "starting", "camera": spec.name, "index": spec.index})
        if not simulate:
            capture = _open_capture(spec)
            actual_mode = _actual_mode(capture)
        else:
            actual_mode = f"simulation {spec.width}x{spec.height}@{spec.fps}"
        frame_writer = SharedFrameWriter(frame_state)
        _send(
            status_queue,
            {
                "kind": "opened",
                "camera": spec.name,
                "index": spec.index,
                "actual": actual_mode,
                "simulated": simulate,
                "frame_transform": spec.frame_transform,
                "transform_applied": False,
                "transform_verified": False,
                "reconnect_count": reconnect_count,
            },
        )
        # Camera opening can take several seconds on DirectShow.  Start the FPS
        # window after it opens so the first health sample reflects frame rate,
        # not device-enumeration time.
        interval_started = time.perf_counter()

        while not stop_event.is_set():
            read_started_ns = time.perf_counter_ns()
            if simulate:
                frame = _simulation_frame(spec, frame_sequence, read_started_ns)
                # Keep the simulated stream close to the configured rate.
                target_period = 1.0 / float(spec.fps)
            else:
                success, frame = capture.read() if capture is not None else (False, None)
                if not success or frame is None:
                    consecutive_read_failures += 1
                    _send(
                        status_queue,
                        {
                            "kind": "read_failure",
                            "camera": spec.name,
                            "consecutive_failures": consecutive_read_failures,
                            "reconnect_count": reconnect_count,
                        },
                    )
                    if consecutive_read_failures < read_failure_threshold:
                        if stop_event.wait(reconnect_delay_seconds):
                            break
                        continue
                    capture, actual_mode, used_attempts = _reconnect_capture(
                        spec,
                        capture,
                        status_queue,
                        stop_event,
                        attempts=reconnect_attempts,
                        delay_seconds=reconnect_delay_seconds,
                        reconnect_count=reconnect_count + 1,
                    )
                    reconnect_count += 1
                    reconnect_attempts_total += used_attempts
                    last_reconnect_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                    consecutive_read_failures = 0
                    continue
                consecutive_read_failures = 0
                target_period = 0.0
            read_finished_ns = time.perf_counter_ns()
            normalized = normalize_frame(frame, spec)
            if not transform_verified:
                # The transform is applied to the frame before it enters both
                # shared memory and the recorder.  This marker is an auditable
                # runtime fact, while the raw pixels remain untouched by UI.
                transform_verified = True
                _send(
                    status_queue,
                    {
                        "kind": "transform_verified",
                        "camera": spec.name,
                        "frame_transform": spec.frame_transform,
                        "sequence": frame_sequence + 1,
                    },
                )
            capture_timestamp_ns = (read_started_ns + read_finished_ns) // 2
            frame_sequence = frame_writer.publish(normalized, capture_timestamp_ns)
            interval_frames += 1
            read_durations_ms.append((read_finished_ns - read_started_ns) / 1_000_000.0)

            if interval_frames == 1 or interval_frames % max(1, spec.fps // 30) == 0:
                gray = normalized if normalized.ndim == 2 else cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
                last_mean = float(gray.mean())
                last_std = float(gray.std())
                quality_samples += 1
                black_frames += int(last_mean <= 1.2 and last_std < 1.0)

            should_record = bool(record_event.is_set())
            if should_record and not recording:
                session_directory = _read_shared_path(record_directory_state)
                if not session_directory:
                    raise RuntimeError("recording directory was not configured")
                (
                    writer,
                    metadata_handle,
                    metadata_writer,
                    recording_path,
                    writer_is_color,
                    recording_codec,
                ) = _open_recording(spec, Path(session_directory), quality=record_quality)
                recording = True
                recorded_frames = 0
                recording_started_ns = capture_timestamp_ns
                _send(
                    status_queue,
                    {
                        "kind": "recording_started",
                        "camera": spec.name,
                        "path": recording_path,
                        "codec": recording_codec,
                    },
                )
            elif not should_record and recording:
                _close_recording(writer, metadata_handle)
                writer = None
                metadata_handle = None
                metadata_writer = None
                recording = False
                _send(
                    status_queue,
                    {
                        "kind": "recording_stopped",
                        "camera": spec.name,
                        "path": recording_path,
                        "recorded_frames": recorded_frames,
                    },
                )

            if recording and writer is not None and metadata_writer is not None:
                if writer_is_color:
                    video_frame = normalized if normalized.ndim == 3 else cv2.cvtColor(normalized, cv2.COLOR_GRAY2BGR)
                else:
                    video_frame = normalized if normalized.ndim == 2 else cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
                writer.write(video_frame)
                recorded_frames += 1
                metadata_writer.writerow(
                    (
                        recorded_frames - 1,
                        frame_sequence,
                        capture_timestamp_ns,
                        datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    )
                )
                if metadata_handle is not None and recorded_frames % 64 == 0:
                    metadata_handle.flush()

            now = time.perf_counter()
            elapsed = now - interval_started
            if elapsed >= status_interval_seconds:
                _send(
                    status_queue,
                    {
                        "kind": "stats",
                        "camera": spec.name,
                        "sequence": frame_sequence,
                        "capture_fps": interval_frames / elapsed,
                        "recorded_frames": recorded_frames,
                        "recording": recording,
                        "black_percent": 100.0 * black_frames / max(quality_samples, 1),
                        "read_p95_ms": float(np.percentile(read_durations_ms, 95)) if read_durations_ms else 0.0,
                        "mean": last_mean,
                        "std": last_std,
                        "timestamp_ns": capture_timestamp_ns,
                        "recording_path": recording_path,
                        "recording_codec": recording_codec,
                        "frame_transform": spec.frame_transform,
                        "transform_applied": True,
                        "transform_verified": transform_verified,
                        "reconnect_count": reconnect_count,
                        "reconnect_attempts_total": reconnect_attempts_total,
                        "last_reconnect_at": last_reconnect_at,
                        "recording_duration_s": (
                            max(0.0, (capture_timestamp_ns - recording_started_ns) / 1_000_000_000.0)
                            if recording and recording_started_ns
                            else 0.0
                        ),
                    },
                )
                interval_started = now
                interval_frames = 0
                quality_samples = 0
                black_frames = 0
                read_durations_ms.clear()

            if target_period > 0:
                elapsed_read = (time.perf_counter_ns() - read_started_ns) / 1_000_000_000.0
                remaining = target_period - elapsed_read
                if remaining > 0:
                    time.sleep(min(remaining, 0.02))
    except KeyboardInterrupt:
        # Console Ctrl+C is delivered to child processes on Windows as well;
        # treat it as the same bounded shutdown path used by the service.
        failed = False
    except Exception as error:
        failed = True
        _send(status_queue, {"kind": "error", "camera": spec.name, "index": spec.index, "message": str(error)})
    finally:
        if recording:
            _close_recording(writer, metadata_handle)
            _send(
                status_queue,
                {
                    "kind": "recording_stopped",
                    "camera": spec.name,
                    "path": recording_path,
                    "recorded_frames": recorded_frames,
                    "forced": True,
                },
            )
        if capture is not None:
            capture.release()
        if frame_writer is not None:
            frame_writer.close()
        _send(status_queue, {"kind": "stopped", "camera": spec.name, "index": spec.index})
    if failed:
        raise SystemExit(1)


def normalize_frame(frame: np.ndarray, spec: CameraConfig) -> np.ndarray:
    if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8:
        raise RuntimeError("camera returned an unsupported frame type")
    if spec.grayscale:
        if frame.ndim == 2:
            converted = frame
        elif frame.ndim == 3 and frame.shape[2] == 1:
            converted = frame[:, :, 0]
        elif frame.ndim == 3 and frame.shape[2] == 3:
            converted = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        elif frame.ndim == 3 and frame.shape[2] == 4:
            converted = cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
        else:
            raise RuntimeError(f"unsupported grayscale frame shape {frame.shape}")
    else:
        if frame.ndim == 2:
            converted = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        elif frame.ndim == 3 and frame.shape[2] == 1:
            converted = cv2.cvtColor(frame[:, :, 0], cv2.COLOR_GRAY2BGR)
        elif frame.ndim == 3 and frame.shape[2] == 3:
            converted = frame
        elif frame.ndim == 3 and frame.shape[2] == 4:
            converted = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        else:
            raise RuntimeError(f"unsupported color frame shape {frame.shape}")
    if converted.shape != spec.frame_shape:
        raise RuntimeError(f"camera returned shape {converted.shape}, expected {spec.frame_shape}")
    if spec.frame_transform != "none":
        converted = cv2.flip(
            converted,
            {"flip_horizontal": 1, "flip_vertical": 0, "rotate_180": -1}[spec.frame_transform],
        )
    return np.ascontiguousarray(converted)


def _open_capture(spec: CameraConfig) -> cv2.VideoCapture:
    backend = 0 if spec.backend == "any" else BACKENDS[spec.backend]
    capture = cv2.VideoCapture(spec.index, backend)
    capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*spec.fourcc))
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, spec.width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, spec.height)
    capture.set(cv2.CAP_PROP_FPS, spec.fps)
    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if spec.auto_exposure is not None:
        capture.set(cv2.CAP_PROP_AUTO_EXPOSURE, AUTO_EXPOSURE_VALUES[spec.auto_exposure])
    if spec.exposure is not None:
        capture.set(cv2.CAP_PROP_EXPOSURE, spec.exposure)
    if spec.gain is not None:
        capture.set(cv2.CAP_PROP_GAIN, spec.gain)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError("could not open camera")
    return capture


def _reconnect_capture(
    spec: CameraConfig,
    capture: cv2.VideoCapture | None,
    status_queue: Any,
    stop_event: Any,
    *,
    attempts: int,
    delay_seconds: float,
    reconnect_count: int,
) -> tuple[cv2.VideoCapture, str, int]:
    """Reopen a DirectShow handle after a bounded run of failed reads.

    Some UVC drivers report a transient empty frame when the USB controller
    recovers.  Releasing the old handle before opening a new one prevents the
    common ``isOpened() == True``/one-fps zombie state.  The worker remains the
    sole owner of the device, so successful recovery does not require a full
    application restart and does not break the active AVI session.
    """
    if capture is not None:
        capture.release()
    errors: list[str] = []
    for attempt in range(1, max(1, int(attempts)) + 1):
        _send(
            status_queue,
            {
                "kind": "reconnecting",
                "camera": spec.name,
                "attempt": attempt,
                "attempts": max(1, int(attempts)),
                "reconnect_count": reconnect_count,
            },
        )
        if stop_event.wait(max(0.05, float(delay_seconds))):
            raise RuntimeError("camera reconnect cancelled during shutdown")
        candidate: cv2.VideoCapture | None = None
        try:
            candidate = _open_capture(spec)
            actual = _actual_mode(candidate)
            _send(
                status_queue,
                {
                    "kind": "reconnected",
                    "camera": spec.name,
                    "actual": actual,
                    "attempt": attempt,
                    "reconnect_count": reconnect_count,
                    "frame_transform": spec.frame_transform,
                },
            )
            return candidate, actual, attempt
        except Exception as error:
            if candidate is not None:
                candidate.release()
            errors.append(str(error))
            _send(
                status_queue,
                {
                    "kind": "reconnect_failed",
                    "camera": spec.name,
                    "attempt": attempt,
                    "attempts": max(1, int(attempts)),
                    "message": str(error),
                    "reconnect_count": reconnect_count,
                },
            )
    detail = "; ".join(errors[-3:]) or "unknown camera open error"
    raise RuntimeError(f"camera read failed; reconnect exhausted after {max(1, int(attempts))} attempts: {detail}")


def _open_recording(
    spec: CameraConfig,
    session_directory: Path,
    *,
    quality: int = 95,
) -> tuple[cv2.VideoWriter, Any, csv.writer, str, bool, str]:
    session_directory.mkdir(parents=True, exist_ok=True)
    base = session_directory / f"{spec.name}.avi"
    # Keep a three-channel MJPG AVI for broad Windows/player compatibility.
    # OpenCV's built-in MJPEG backend accepts an explicit quality value, unlike
    # the default FFmpeg MJPG writer whose implicit quality visibly softens eye
    # edges during a second compression pass.
    record_fourcc = getattr(spec, "record_fourcc", "MJPG") or "MJPG"
    quality = max(30, min(100, int(quality)))
    writer_is_color = True
    selected_codec = f"{record_fourcc} q{quality}"
    writer = cv2.VideoWriter()
    opened = False
    if record_fourcc.upper() == "MJPG" and hasattr(cv2, "CAP_OPENCV_MJPEG"):
        try:
            opened = bool(
                writer.open(
                    str(base),
                    int(cv2.CAP_OPENCV_MJPEG),
                    cv2.VideoWriter_fourcc(*record_fourcc),
                    float(spec.fps),
                    (spec.width, spec.height),
                    [int(cv2.VIDEOWRITER_PROP_QUALITY), quality],
                )
            )
        except (TypeError, cv2.error):
            opened = False
    if not opened:
        writer.release()
        writer = cv2.VideoWriter(
            str(base),
            cv2.VideoWriter_fourcc(*record_fourcc),
            float(spec.fps),
            (spec.width, spec.height),
            writer_is_color,
        )
        opened = writer.isOpened()
    if not writer.isOpened():
        writer.release()
        # Preserve the stable *.avi contract if a custom configured codec is
        # unavailable on the target machine.
        writer_is_color = True
        selected_codec = f"{spec.fourcc} default"
        writer = cv2.VideoWriter(
            str(base),
            cv2.VideoWriter_fourcc(*spec.fourcc),
            float(spec.fps),
            (spec.width, spec.height),
            True,
        )
    if not writer.isOpened():
        writer.release()
        raise RuntimeError(f"could not open video writer for {spec.name}")
    metadata_path = session_directory / f"{spec.name}_frames.csv"
    handle = metadata_path.open("w", encoding="utf-8", newline="")
    csv_writer = csv.writer(handle)
    csv_writer.writerow(("frame_index", "capture_sequence", "timestamp_ns", "wall_time"))
    handle.flush()
    return writer, handle, csv_writer, base.name, writer_is_color, selected_codec


def _close_recording(writer: cv2.VideoWriter | None, metadata_handle: Any | None) -> None:
    if writer is not None:
        writer.release()
    if metadata_handle is not None:
        metadata_handle.flush()
        metadata_handle.close()


def _simulation_frame(spec: CameraConfig, sequence: int, timestamp_ns: int) -> np.ndarray:
    """Generate a deterministic visible test pattern when no camera is attached."""
    height, width = spec.height, spec.width
    value = (sequence * 3 + (timestamp_ns // 1_000_000) // 17) % 255
    if spec.grayscale:
        frame = np.full((height, width), value, dtype=np.uint8)
        cv2.circle(frame, (width // 2, height // 2), max(4, min(width, height) // 5), 220, 2)
        cv2.putText(frame, spec.name[:8], (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.45, 255, 1, cv2.LINE_AA)
        return frame
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :, 0] = value
    frame[:, :, 1] = (value * 2) % 255
    frame[:, :, 2] = (value * 3) % 255
    cv2.putText(frame, spec.name[:8], (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
    return frame


def _actual_mode(capture: cv2.VideoCapture) -> str:
    code = int(capture.get(cv2.CAP_PROP_FOURCC))
    fourcc = "".join(chr((code >> (8 * index)) & 0xFF) if 32 <= ((code >> (8 * index)) & 0xFF) <= 126 else "?" for index in range(4))
    return (
        f"{int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))}@"
        f"{capture.get(cv2.CAP_PROP_FPS):.1f} FOURCC={fourcc!r}"
    )


def _send(status_queue: Any, payload: dict[str, Any]) -> None:
    try:
        status_queue.put_nowait(payload)
    except queue.Full:
        # Status is advisory; frame capture and disk writing must never wait on the UI.
        pass


def set_shared_path(state: Any, path: str | Path) -> None:
    """Store a UTF-8 path in a bounded multiprocessing byte array."""
    raw = os.fsencode(str(path))
    capacity = len(state)
    if len(raw) >= capacity:
        raise ValueError(f"recording path is too long (max {capacity - 1} bytes)")
    with state.get_lock():
        state[:] = b"\x00" * capacity
        state[: len(raw)] = raw


def _read_shared_path(state: Any) -> str:
    with state.get_lock():
        raw = bytes(state[:])
    return os.fsdecode(raw.split(b"\x00", 1)[0])
