"""Build review artifacts and a verifiable ZIP export for one session.

Raw acquisition files are never modified. Annotated videos are derived during
export by aligning each recorded frame with the most recent pupil sample. This
keeps computer-vision work outside the high-rate camera processes.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Mapping

import cv2

from .storage import SessionStore


_EXPORT_LOCK = threading.RLock()
_ANNOTATION_MAX_AGE_NS = 250_000_000

_SESSION_GUIDE = """

Export file guide
-----------------
*.avi: raw camera frames after the configured orientation transform; these are
the source recordings and never contain a computer-vision overlay.
*_annotated.avi: review copies with the pupil outline, center, confidence,
latency, eye role, and frame number burned in.
*_pupil_preview.jpg: a representative annotated still image for quick review
without opening the video editor.
*_frames.csv: one row per written video frame with capture sequence and
monotonic timestamp.
*_pupil.csv: pupil validity, geometry, confidence, and detector latency for
each detector sample.
pupil_report.json: structured per-eye detection summary, confidence statistics,
alignment-gate settings, and links to the review artifacts.
clinical_report.json: clinician-facing de-identified session summary with
camera quality, pupil quality, screening note, and artifact links.
quality.json: per-camera frame-rate, black-frame, and recording quality checks.
config.json / session.json: camera mapping, orientation, capture mode, and
de-identified session metadata.
analysis_ready.csv: one-row-per-camera summary for analysis import.
export_manifest.json: inventory with purpose, byte count, and SHA-256 for every
session file; the manifest itself is excluded from its own hash.

This package is for research/screening review and is not a diagnostic report.
The high-speed mode preserves the camera's native pixels; software upscaling
cannot create missing detail.
"""

_KNOWN_FILES = {
    "README.txt": (
        "documentation",
        "This file explains the session folder and its research-only status.",
    ),
    "session.json": (
        "session_metadata",
        "De-identified session metadata and final state.",
    ),
    "config.json": (
        "capture_config",
        "Camera mapping, requested mode, transforms, and quality settings used for this session.",
    ),
    "quality.json": (
        "quality_report",
        "Per-camera frame-rate, black-frame, and recording quality summary.",
    ),
    "analysis_ready.csv": (
        "analysis_table",
        "One-row-per-camera summary intended for analysis import.",
    ),
    "pupil_report.json": (
        "pupil_report",
        "Structured per-eye pupil detection summary and alignment-gate provenance; not a diagnostic report.",
    ),
    "clinical_report.json": (
        "clinical_report",
        "De-identified clinical acquisition summary with camera quality, pupil quality, and artifact links; not a diagnostic report.",
    ),
    "screening.json": (
        "screening_note",
        "Optional research/screening note; not a diagnostic report.",
    ),
}


def export_session(store: SessionStore, session_id: str) -> Path:
    """Generate derived review files, provenance, and the final ZIP archive."""
    with _EXPORT_LOCK:
        root = store.path_for(session_id)
        if not root.exists():
            raise ValueError(f"session does not exist: {session_id}")

        warnings: list[str] = []
        for eye_name in ("right_eye", "left_eye"):
            source = root / f"{eye_name}.avi"
            if not source.exists():
                continue
            try:
                if annotate_video(root, eye_name) is None:
                    warnings.append(
                        f"{eye_name} annotated video could not be generated; "
                        "review the raw AVI and CSV files."
                    )
            except Exception as error:  # derived output must not hide raw data
                warnings.append(f"{eye_name} annotation failed: {error}")

        _write_session_readme(root)
        try:
            build_pupil_report(root, session_id)
        except Exception as error:  # report generation must not hide raw data
            warnings.append(f"瞳孔报告生成失败: {error}")
        try:
            build_clinical_report(root, session_id)
        except Exception as error:  # report generation must not hide raw data
            warnings.append(f"临床报告生成失败: {error}")
        build_manifest(root, session_id, warnings)
        return store.export_zip(session_id)


def annotate_video(root: Path, eye_name: str) -> Path | None:
    """Create a timestamp-aligned annotated AVI without changing the raw AVI."""
    source = root / f"{eye_name}.avi"
    target = root / f"{eye_name}_annotated.avi"
    frames_path = root / f"{eye_name}_frames.csv"
    pupil_path = root / f"{eye_name}_pupil.csv"
    preview_target = root / f"{eye_name}_pupil_preview.jpg"
    if not source.exists():
        return None

    inputs = (source, frames_path, pupil_path, root / "config.json")
    newest_input = max(
        (path.stat().st_mtime_ns for path in inputs if path.exists()),
        default=0,
    )
    if (
        target.exists()
        and preview_target.exists()
        and target.stat().st_mtime_ns >= newest_input
        and preview_target.stat().st_mtime_ns >= newest_input
        and _playable_video(target)
    ):
        return target

    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        capture.release()
        return None

    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    if width <= 0 or height <= 0:
        capture.release()
        return None
    if not math.isfinite(fps) or fps <= 0:
        fps = 30.0

    frame_rows = _read_rows(frames_path)
    detections = _detection_index(_read_rows(pupil_path))
    temporary = target.with_name(
        f"{target.stem}.tmp.{os.getpid()}.{threading.get_ident()}{target.suffix}"
    )
    for stale in root.glob(f"{target.stem}.tmp.*{target.suffix}"):
        try:
            stale.unlink()
        except OSError:
            pass

    writer = cv2.VideoWriter(
        str(temporary),
        cv2.VideoWriter_fourcc(*"MJPG"),
        fps,
        (width, height),
        True,
    )
    if hasattr(cv2, "VIDEOWRITER_PROP_QUALITY"):
        try:
            writer.set(int(cv2.VIDEOWRITER_PROP_QUALITY), 95)
        except Exception:
            pass
    if not writer.isOpened():
        capture.release()
        writer.release()
        temporary.unlink(missing_ok=True)
        return None

    frame_count = 0
    detection_position = 0
    current: dict[str, Any] | None = None
    preview_frame: Any | None = None
    preview_valid = False
    completed = False
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            metadata = frame_rows[frame_count] if frame_count < len(frame_rows) else {}
            sequence = _integer(metadata, "capture_sequence", frame_count)
            timestamp = _integer(metadata, "timestamp_ns", 0)

            while detection_position < len(detections):
                candidate = detections[detection_position]
                if sequence and candidate["sequence"] and candidate["sequence"] > sequence:
                    break
                if not sequence and timestamp and candidate["timestamp"] and candidate["timestamp"] > timestamp:
                    break
                current = candidate
                detection_position += 1

            valid = False
            if current and current.get("valid"):
                current_timestamp = int(current.get("timestamp") or 0)
                valid = (
                    0 <= timestamp - current_timestamp <= _ANNOTATION_MAX_AGE_NS
                    if timestamp and current_timestamp
                    else True
                )
            annotated = _draw_annotation(
                frame,
                current,
                valid=valid,
                frame_index=frame_count,
                eye_name=eye_name,
            )
            if preview_frame is None or (valid and not preview_valid):
                preview_frame = annotated.copy()
                preview_valid = valid
            writer.write(annotated)
            frame_count += 1
        completed = frame_count > 0
    finally:
        capture.release()
        writer.release()
        if not completed:
            temporary.unlink(missing_ok=True)

    if not completed or not _playable_video(temporary, expected_frames=frame_count):
        temporary.unlink(missing_ok=True)
        return None
    os.replace(temporary, target)
    if preview_frame is not None:
        _write_preview_image(preview_target, preview_frame)
    return target


def _write_preview_image(target: Path, frame: Any) -> None:
    """Write one representative annotated frame atomically for quick review."""
    temporary = target.with_name(
        f"{target.stem}.tmp.{os.getpid()}.{threading.get_ident()}{target.suffix}"
    )
    try:
        ok, encoded = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), 95],
        )
        if not ok:
            raise RuntimeError("OpenCV could not encode the pupil preview image")
        temporary.write_bytes(bytes(encoded))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _playable_video(path: Path, *, expected_frames: int | None = None) -> bool:
    try:
        probe = cv2.VideoCapture(str(path))
        reported_frames = float(probe.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        playable = probe.isOpened() and bool(probe.grab())
        probe.release()
    except (OSError, cv2.error):
        return False
    if expected_frames and reported_frames > 0:
        playable = playable and reported_frames >= max(1.0, expected_frames * 0.99)
    return bool(playable)


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error):
        return []


def _detection_index(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    detections = [
        {
            "sequence": _integer(row, "sequence", 0),
            "timestamp": _integer(row, "capture_timestamp_ns", 0),
            "valid": _truthy(row.get("valid")),
            "x": _number(row, "center_x"),
            "y": _number(row, "center_y"),
            "axis_width": _number(row, "axis_width"),
            "axis_height": _number(row, "axis_height"),
            "angle": _number(row, "angle", 0.0),
            "confidence": _number(row, "confidence", 0.0) or 0.0,
            "latency_ms": _number(row, "latency_ms", 0.0) or 0.0,
        }
        for row in rows
    ]
    return sorted(detections, key=lambda item: (item["sequence"], item["timestamp"]))


def _number(row: Mapping[str, Any], key: str, default: float | None = None) -> float | None:
    try:
        value = float(row.get(key, ""))
    except (AttributeError, TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _integer(row: Mapping[str, Any], key: str, default: int = 0) -> int:
    value = _number(row, key)
    return int(value) if value is not None else default


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _draw_annotation(
    frame: Any,
    detection: Mapping[str, Any] | None,
    *,
    valid: bool,
    frame_index: int,
    eye_name: str,
) -> Any:
    if frame is None:
        return frame
    if frame.ndim == 2:
        preview = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    elif frame.ndim == 3 and frame.shape[2] == 1:
        preview = cv2.cvtColor(frame[:, :, 0], cv2.COLOR_GRAY2BGR)
    elif frame.ndim == 3 and frame.shape[2] == 4:
        preview = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    else:
        preview = frame.copy()

    height, width = preview.shape[:2]
    color = (70, 255, 80) if valid else (80, 170, 255)
    if valid and detection:
        x, y = detection.get("x"), detection.get("y")
        if x is not None and y is not None:
            center = (
                max(0, min(width - 1, int(round(float(x))))),
                max(0, min(height - 1, int(round(float(y))))),
            )
            axes = (
                max(1, int(round(float(detection.get("axis_width") or 2.0) / 2.0))),
                max(1, int(round(float(detection.get("axis_height") or 2.0) / 2.0))),
            )
            cv2.ellipse(
                preview,
                center,
                axes,
                float(detection.get("angle") or 0.0),
                0,
                360,
                (0, 220, 255),
                2,
                cv2.LINE_AA,
            )
            cv2.circle(preview, center, 3, color, -1, cv2.LINE_AA)
            cv2.line(preview, (center[0] - 7, center[1]), (center[0] + 7, center[1]), color, 1, cv2.LINE_AA)
            cv2.line(preview, (center[0], center[1] - 7), (center[0], center[1] + 7), color, 1, cv2.LINE_AA)
        pupil_label = "PUPIL* {:.2f}  {:.1f}ms".format(
            float(detection.get("confidence") or 0.0),
            float(detection.get("latency_ms") or 0.0),
        )
    else:
        pupil_label = "PUPIL --"

    sequence = int(detection.get("sequence") or 0) if detection else 0
    eye_label = "{}  seq{}".format(eye_name, sequence)
    footer = f"{eye_name} | annotated | frame {frame_index} | {width}x{height}"
    overlay = preview.copy()
    cv2.rectangle(overlay, (0, 0), (width, 29), (0, 0, 0), -1)
    cv2.rectangle(overlay, (0, max(0, height - 20)), (width, height), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.52, preview, 0.48, 0, preview)
    cv2.putText(preview, pupil_label, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.48, color, 1, cv2.LINE_AA)
    label_size = cv2.getTextSize(eye_label, cv2.FONT_HERSHEY_SIMPLEX, 0.38, 1)[0]
    cv2.putText(
        preview,
        eye_label,
        (max(8, width - label_size[0] - 8), 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.38,
        (210, 230, 230),
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        preview,
        footer,
        (8, max(16, height - 6)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.38,
        (210, 230, 230),
        1,
        cv2.LINE_AA,
    )
    return preview


def _write_session_readme(root: Path) -> None:
    target = root / "README.txt"
    try:
        existing = target.read_text(encoding="utf-8") if target.exists() else "Clinical Capture session\n=========================\n"
        if "Export file guide" not in existing:
            target.write_text(existing.rstrip() + _SESSION_GUIDE, encoding="utf-8")
    except (OSError, UnicodeError):
        pass


def build_pupil_report(root: Path, session_id: str) -> dict[str, Any]:
    """Write a compact, machine-readable pupil review report for the session."""
    config: dict[str, Any] = {}
    session: dict[str, Any] = {}
    try:
        config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    try:
        session = json.loads((root / "session.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass

    camera_stats = session.get("camera_stats") if isinstance(session, Mapping) else {}
    if not isinstance(camera_stats, Mapping):
        camera_stats = {}
    threshold_value = _number(config, "pupil_confidence_threshold", 0.6)
    threshold = 0.6 if threshold_value is None else threshold_value
    eyes: dict[str, dict[str, Any]] = {}
    for eye_name in ("right_eye", "left_eye"):
        rows = _read_rows(root / f"{eye_name}_pupil.csv")
        confidences = [
            value
            for value in (_number(row, "confidence") for row in rows)
            if value is not None
        ]
        valid_count = sum(1 for row in rows if _truthy(row.get("valid")))
        timestamps = [
            value
            for value in (_integer(row, "capture_timestamp_ns", 0) for row in rows)
            if value > 0
        ]
        detector_summary = camera_stats.get(eye_name, {})
        if not isinstance(detector_summary, Mapping):
            detector_summary = {}
        pupil_summary = detector_summary.get("pupil", {})
        if not isinstance(pupil_summary, Mapping):
            pupil_summary = {}
        eyes[eye_name] = {
            "pupil_csv": f"{eye_name}_pupil.csv" if rows or (root / f"{eye_name}_pupil.csv").exists() else None,
            "annotated_video": f"{eye_name}_annotated.avi" if (root / f"{eye_name}_annotated.avi").exists() else None,
            "annotated_preview": f"{eye_name}_pupil_preview.jpg" if (root / f"{eye_name}_pupil_preview.jpg").exists() else None,
            "samples": len(rows),
            "valid_samples": valid_count,
            "valid_rate_percent": round(100.0 * valid_count / max(1, len(rows)), 2),
            "confidence_threshold": round(float(threshold), 4),
            "confidence_min": round(min(confidences), 4) if confidences else None,
            "confidence_mean": round(sum(confidences) / len(confidences), 4) if confidences else None,
            "confidence_max": round(max(confidences), 4) if confidences else None,
            "capture_start_timestamp_ns": min(timestamps) if timestamps else None,
            "capture_end_timestamp_ns": max(timestamps) if timestamps else None,
            "detector_fps": pupil_summary.get("detector_fps"),
            "latency_p95_ms": pupil_summary.get("latency_p95_ms"),
            "alignment_at_start": pupil_summary.get("alignment_at_start", {}),
            "alignment_at_stop": pupil_summary.get("alignment_at_stop", {}),
        }

    report = {
        "schema_version": 1,
        "session_id": session_id,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "purpose": "Pupil detection and camera-alignment review for research/screening; not a diagnostic report.",
        "alignment_gate": {
            "required": not bool(config.get("simulate", False)),
            "hold_seconds": config.get("pupil_alignment_hold_seconds", 1.5),
            "center_tolerance": config.get("pupil_alignment_center_tolerance", 0.30),
            "max_detection_age_seconds": config.get("pupil_alignment_max_age_seconds", 0.75),
            "confidence_threshold": threshold,
        },
        "eyes": eyes,
        "artifacts": {
            "raw_videos": [
                f"{eye_name}.avi"
                for eye_name in ("right_eye", "left_eye")
                if (root / f"{eye_name}.avi").exists()
            ],
            "detection_csv": [
                f"{eye_name}_pupil.csv"
                for eye_name in ("right_eye", "left_eye")
                if (root / f"{eye_name}_pupil.csv").exists()
            ],
        },
        "disclaimer": "仅供研究/筛查记录和设备对准复核，不构成临床诊断或治疗建议。",
    }
    target = root / "pupil_report.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return report


def build_clinical_report(root: Path, session_id: str) -> dict[str, Any]:
    """Write the clinician-facing summary without adding diagnostic claims."""
    session: dict[str, Any] = {}
    quality: dict[str, Any] = {}
    pupil_report: dict[str, Any] = {}
    for name, target in (
        ("session.json", "session"),
        ("quality.json", "quality"),
        ("pupil_report.json", "pupil_report"),
    ):
        try:
            value = json.loads((root / name).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            value = {}
        if target == "session":
            session = value if isinstance(value, dict) else {}
        elif target == "quality":
            quality = value if isinstance(value, dict) else {}
        else:
            pupil_report = value if isinstance(value, dict) else {}

    metadata = session.get("metadata") if isinstance(session.get("metadata"), Mapping) else {}
    cameras = quality.get("cameras") if isinstance(quality.get("cameras"), Mapping) else {}
    report = {
        "schema_version": 1,
        "session_id": session_id,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": session.get("state", "unknown"),
        "metadata": {
            "subject_code": metadata.get("subject_code"),
            "visit_id": metadata.get("visit_id"),
            "operator": metadata.get("operator"),
            "protocol": metadata.get("protocol"),
            "age": metadata.get("age"),
            "sex": metadata.get("sex"),
            "consent_confirmed": metadata.get("consent_confirmed"),
        },
        "quality": {
            "overall": quality.get("overall", {}),
            "cameras": cameras,
        },
        "pupil": {
            "alignment_gate": pupil_report.get("alignment_gate", {}),
            "eyes": pupil_report.get("eyes", {}),
        },
        "screening": session.get("screening_result"),
        "artifacts": {
            "raw_videos": [
                f"{eye_name}.avi"
                for eye_name in ("right_eye", "left_eye")
                if (root / f"{eye_name}.avi").exists()
            ],
            "annotated_videos": [
                f"{eye_name}_annotated.avi"
                for eye_name in ("right_eye", "left_eye")
                if (root / f"{eye_name}_annotated.avi").exists()
            ],
            "annotated_images": [
                f"{eye_name}_pupil_preview.jpg"
                for eye_name in ("right_eye", "left_eye")
                if (root / f"{eye_name}_pupil_preview.jpg").exists()
            ],
            "pupil_report": "pupil_report.json" if (root / "pupil_report.json").exists() else None,
            "clinical_report": "clinical_report.json",
            "quality_report": "quality.json" if (root / "quality.json").exists() else None,
        },
        "disclaimer": "仅供临床研究/筛查采集质量复核，不构成临床诊断、治疗或风险分层结论。",
    }
    target = root / "clinical_report.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return report


def build_manifest(root: Path, session_id: str, warnings: list[str] | None = None) -> dict[str, Any]:
    """Write a manifest with provenance and hashes for every session artifact."""
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.name == "export_manifest.json":
            continue
        if ".tmp." in path.name or path.name.endswith(".tmp"):
            continue
        relative = path.relative_to(root).as_posix()
        if relative in _KNOWN_FILES:
            kind, description = _KNOWN_FILES[relative]
        elif relative.endswith("_annotated.avi"):
            kind, description = (
                "annotated_video",
                "Playback copy with timestamp-aligned pupil outline, center, confidence, latency, and frame number. The matching .avi remains the raw source.",
            )
        elif relative.endswith(".avi"):
            kind, description = (
                "raw_video",
                "Original camera frames after the configured orientation transform; no computer-vision overlay is burned in.",
            )
        elif relative.endswith("_frames.csv"):
            kind, description = (
                "frame_timestamps",
                "Per-written-frame capture sequence and monotonic timestamp used to align annotations.",
            )
        elif relative.endswith("_pupil.csv"):
            kind, description = (
                "pupil_detection",
                "Detector output for the newest frame at each detector sample, including validity, geometry, confidence, and latency.",
            )
        elif relative.endswith("_pupil_preview.jpg"):
            kind, description = (
                "annotated_image",
                "Representative still image with timestamp-aligned pupil outline, center, confidence, and eye role.",
            )
        else:
            kind, description = (
                "other",
                "Additional session artifact; see this manifest and the file header/content.",
            )
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        files.append(
            {
                "path": relative,
                "kind": kind,
                "description": description,
                "size_bytes": size,
                "sha256": _sha256(path),
            }
        )

    camera_mapping: dict[str, Any] = {}
    try:
        config = json.loads((root / "config.json").read_text(encoding="utf-8"))
        for camera in config.get("cameras", []):
            camera_mapping[str(camera.get("name"))] = {
                "physical_index": camera.get("index"),
                "frame_transform": camera.get("frame_transform", "none"),
                "requested": "{}x{}@{}".format(
                    camera.get("width"), camera.get("height"), camera.get("fps")
                ),
                "fourcc": camera.get("fourcc"),
                "record_fourcc": camera.get("record_fourcc"),
            }
    except (OSError, ValueError, TypeError, AttributeError):
        pass

    annotated_videos = {}
    for eye_name in ("right_eye", "left_eye"):
        source = root / f"{eye_name}.avi"
        target = root / f"{eye_name}_annotated.avi"
        annotated_videos[eye_name] = {
            "source": source.name if source.exists() else None,
            "file": target.name if target.exists() else None,
            "available": target.exists(),
        }

    manifest = {
        "schema_version": 1,
        "session_id": session_id,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "purpose": "Clinical Capture export inventory and provenance. This package is for research/screening review and is not a diagnostic report.",
        "camera_mapping": camera_mapping,
        "annotated_videos": annotated_videos,
        "pupil_report": {
            "file": "pupil_report.json" if (root / "pupil_report.json").exists() else None,
            "available": (root / "pupil_report.json").exists(),
        },
        "clinical_report": {
            "file": "clinical_report.json" if (root / "clinical_report.json").exists() else None,
            "available": (root / "clinical_report.json").exists(),
        },
        "warnings": list(warnings or []),
        "manifest_self_excluded": True,
        "files": files,
    }
    target = root / "export_manifest.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return manifest


def _sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()
