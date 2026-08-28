from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping


SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
SAFE_OPERATOR = re.compile(r"[^\r\n\t]{1,120}\Z")
ALLOWED_SEX = {"unknown", "female", "male", "other"}
ALLOWED_SCREENINGS = {"none", "pathology", "emotion", "custom"}


class SessionState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    READY = "ready"
    RECORDING = "recording"
    STOPPING = "stopping"
    COMPLETED = "completed"
    ERROR = "error"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _optional_int(value: Any, name: str, *, minimum: int, maximum: int) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer or null")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class CameraConfig:
    """A bounded camera configuration accepted by the acquisition worker."""

    name: str
    index: int
    width: int
    height: int
    fps: int
    required: bool = True
    fourcc: str = "MJPG"
    # Camera transport and recording codec are separate so recording quality
    # can be controlled without changing the validated high-speed mode.
    record_fourcc: str = "MJPG"
    backend: str = "dshow"
    grayscale: bool = True
    auto_exposure: str | None = None
    exposure: float | None = None
    gain: float | None = None
    frame_transform: str = "none"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CameraConfig":
        if not isinstance(raw, Mapping):
            raise ValueError("each camera must be a JSON object")
        allowed = {
            "name", "index", "width", "height", "fps", "required", "fourcc",
            "record_fourcc", "backend", "grayscale", "auto_exposure", "exposure", "gain",
            "frame_transform",
        }
        extra = set(raw) - allowed
        if extra:
            raise ValueError(f"unknown camera fields: {sorted(extra)}")
        try:
            item = cls(**dict(raw))
        except TypeError as error:
            raise ValueError(f"invalid camera fields: {error}") from error
        return item.validate()

    @property
    def frame_shape(self) -> tuple[int, ...]:
        return (self.height, self.width) if self.grayscale else (self.height, self.width, 3)

    def validate(self) -> "CameraConfig":
        if not isinstance(self.name, str) or not SAFE_ID.fullmatch(self.name):
            raise ValueError("camera name must contain only letters, numbers, '.', '_' or '-'")
        for name in ("width", "height", "fps"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"camera {self.name}: {name} must be a positive integer")
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise ValueError(f"camera {self.name}: index must be nonnegative")
        if not isinstance(self.required, bool):
            raise ValueError(f"camera {self.name}: required must be boolean")
        for field_name in ("fourcc", "record_fourcc"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or len(value) != 4 or not value.isascii():
                raise ValueError(f"camera {self.name}: {field_name} must be four ASCII characters")
        if self.backend not in {"dshow", "msmf", "any"}:
            raise ValueError(f"camera {self.name}: backend must be dshow, msmf or any")
        if not isinstance(self.grayscale, bool):
            raise ValueError(f"camera {self.name}: grayscale must be boolean")
        if self.auto_exposure not in (None, "auto", "manual"):
            raise ValueError(f"camera {self.name}: auto_exposure must be auto, manual or null")
        if self.frame_transform not in {"none", "flip_horizontal", "flip_vertical", "rotate_180"}:
            raise ValueError(f"camera {self.name}: unsupported frame_transform")
        for name in ("exposure", "gain"):
            value = getattr(self, name)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise ValueError(f"camera {self.name}: {name} must be finite or null")
        return self

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        # The UI does not need the complete driver tuning values in its status response.
        return value


@dataclass(frozen=True, slots=True)
class ClinicalConfig:
    cameras: tuple[CameraConfig, ...]
    data_dir: Path
    preview_fps: int = 6
    pupil_detector_fps: int = 60
    pupil_confidence_threshold: float = 0.6
    # Recording is blocked until every required eye has a stable, centered
    # pupil detection.  The hold avoids starting while the operator is still
    # moving the camera or the patient is blinking.
    pupil_alignment_hold_seconds: float = 1.5
    pupil_alignment_center_tolerance: float = 0.30
    pupil_alignment_max_age_seconds: float = 0.75
    # Detector2D remains the source of the raw confidence.  These conservative
    # candidate filters reject clipped/elongated contours and one-frame jumps
    # before a sample can guide alignment or enter the valid stream.
    pupil_candidate_min_y_ratio: float = 0.05
    pupil_candidate_max_y_ratio: float = 0.95
    pupil_candidate_max_axis_ratio: float = 2.8
    pupil_candidate_max_jump_ratio: float = 0.35
    pupil_candidate_min_diameter_ratio: float = 0.07
    pupil_candidate_stability_frames: int = 3
    # The centered guide approximates the camera's frontal eye-orbit range. It
    # is a visual aid only; pupil validity and the recording gate never use
    # these margins.
    preview_guide_margin_x: float = 0.16
    preview_guide_margin_y: float = 0.20
    status_interval_seconds: float = 1.0
    startup_timeout_seconds: float = 12.0
    recording_ack_timeout_seconds: float = 8.0
    shutdown_timeout_seconds: float = 5.0
    stale_frame_timeout_seconds: float = 2.5
    # A short USB/DirectShow hiccup must not terminate a clinical session.
    # Workers retry failed reads, then reopen the handle with bounded backoff.
    camera_read_failure_threshold: int = 3
    camera_reconnect_attempts: int = 3
    camera_reconnect_delay_seconds: float = 0.20
    # Two 240-fps cameras share one USB transport on the field workstation;
    # measured stable throughput is about 162-167 fps per eye.  Keep a
    # conservative, explicit floor that accepts that validated operating point
    # while still rejecting a materially stalled stream.
    minimum_fps_ratio: float = 0.65
    max_black_percent: float = 20.0
    jpeg_quality: int = 95
    simulate: bool = False

    @classmethod
    def defaults(cls, *, data_dir: str | Path | None = None, simulate: bool = False) -> "ClinicalConfig":
        root = Path(data_dir) if data_dir is not None else Path("data") / "clinical_sessions"
        return cls(
            cameras=(
                CameraConfig(name="right_eye", index=1, width=320, height=240, fps=240, frame_transform="rotate_180"),
                CameraConfig(name="left_eye", index=0, width=320, height=240, fps=240),
            ),
            data_dir=root,
            simulate=simulate,
        ).validate()

    @classmethod
    def from_json(
        cls,
        path: str | Path,
        *,
        data_dir: str | Path | None = None,
        simulate: bool | None = None,
    ) -> "ClinicalConfig":
        config_path = Path(path)
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"could not load clinical config {config_path}: {error}") from error
        if not isinstance(payload, Mapping):
            raise ValueError("clinical configuration must be a JSON object")
        allowed = {
            "cameras", "data_dir", "preview_fps", "pupil_detector_fps",
            "pupil_confidence_threshold", "pupil_alignment_hold_seconds",
            "pupil_alignment_center_tolerance", "pupil_alignment_max_age_seconds",
            "pupil_candidate_min_y_ratio", "pupil_candidate_max_y_ratio",
            "pupil_candidate_max_axis_ratio", "pupil_candidate_max_jump_ratio",
            "pupil_candidate_min_diameter_ratio",
            "pupil_candidate_stability_frames",
            "preview_guide_margin_x", "preview_guide_margin_y",
            "status_interval_seconds",
            "startup_timeout_seconds", "recording_ack_timeout_seconds",
            "shutdown_timeout_seconds", "stale_frame_timeout_seconds",
            "camera_read_failure_threshold", "camera_reconnect_attempts",
            "camera_reconnect_delay_seconds",
            "minimum_fps_ratio", "max_black_percent", "jpeg_quality", "simulate",
        }
        extra = set(payload) - allowed
        if extra:
            raise ValueError(f"unknown clinical configuration fields: {sorted(extra)}")
        raw_cameras = payload.get("cameras")
        if not isinstance(raw_cameras, list):
            raise ValueError("cameras must be a JSON array")
        root = data_dir if data_dir is not None else payload.get("data_dir", Path("data") / "clinical_sessions")
        raw_simulation = payload.get("simulate", False)
        if not isinstance(raw_simulation, bool):
            raise ValueError("simulate must be boolean")
        if simulate is not None and not isinstance(simulate, bool):
            raise ValueError("simulate override must be boolean")
        selected_simulation = raw_simulation if simulate is None else simulate
        values = {
            "cameras": tuple(CameraConfig.from_mapping(item) for item in raw_cameras),
            "data_dir": Path(root),
            **{key: payload[key] for key in allowed if key in payload and key not in {"cameras", "data_dir", "simulate"}},
            "simulate": selected_simulation,
        }
        return cls(**values).validate()

    def validate(self) -> "ClinicalConfig":
        if not self.cameras:
            raise ValueError("at least one camera must be configured")
        names = [camera.name for camera in self.cameras]
        indices = [camera.index for camera in self.cameras]
        if len(names) != len(set(names)):
            raise ValueError("camera names must be unique")
        if len(indices) != len(set(indices)):
            raise ValueError("camera indices must be unique")
        if not any(camera.required for camera in self.cameras):
            raise ValueError("at least one camera must be required")
        for name in ("preview_fps",):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 60:
                raise ValueError(f"{name} must be an integer between 1 and 60")
        if (
            isinstance(self.pupil_detector_fps, bool)
            or not isinstance(self.pupil_detector_fps, int)
            or not 1 <= self.pupil_detector_fps <= 60
        ):
            raise ValueError("pupil_detector_fps must be an integer between 1 and 60")
        if (
            isinstance(self.pupil_confidence_threshold, bool)
            or not isinstance(self.pupil_confidence_threshold, (int, float))
            or not math.isfinite(float(self.pupil_confidence_threshold))
            or not 0 <= float(self.pupil_confidence_threshold) <= 1
        ):
            raise ValueError("pupil_confidence_threshold must be between 0 and 1")
        for name in ("pupil_alignment_hold_seconds", "pupil_alignment_max_age_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")
        for name in ("preview_guide_margin_x", "preview_guide_margin_y"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) < 0.45
            ):
                raise ValueError(f"{name} must be between 0 and 0.45")
        if (
            isinstance(self.pupil_alignment_center_tolerance, bool)
            or not isinstance(self.pupil_alignment_center_tolerance, (int, float))
            or not math.isfinite(float(self.pupil_alignment_center_tolerance))
            or not 0.05 <= float(self.pupil_alignment_center_tolerance) <= 0.49
        ):
            raise ValueError("pupil_alignment_center_tolerance must be between 0.05 and 0.49")
        for name in ("pupil_candidate_min_y_ratio", "pupil_candidate_max_y_ratio"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError(f"{name} must be between 0 and 1")
        if self.pupil_candidate_max_y_ratio <= self.pupil_candidate_min_y_ratio:
            raise ValueError("pupil_candidate_max_y_ratio must exceed pupil_candidate_min_y_ratio")
        if (
            isinstance(self.pupil_candidate_max_axis_ratio, bool)
            or not isinstance(self.pupil_candidate_max_axis_ratio, (int, float))
            or not math.isfinite(float(self.pupil_candidate_max_axis_ratio))
            or not 1.05 <= float(self.pupil_candidate_max_axis_ratio) <= 10.0
        ):
            raise ValueError("pupil_candidate_max_axis_ratio must be between 1.05 and 10")
        if (
            isinstance(self.pupil_candidate_max_jump_ratio, bool)
            or not isinstance(self.pupil_candidate_max_jump_ratio, (int, float))
            or not math.isfinite(float(self.pupil_candidate_max_jump_ratio))
            or not 0.05 <= float(self.pupil_candidate_max_jump_ratio) <= 1.0
        ):
            raise ValueError("pupil_candidate_max_jump_ratio must be between 0.05 and 1")
        if (
            isinstance(self.pupil_candidate_min_diameter_ratio, bool)
            or not isinstance(self.pupil_candidate_min_diameter_ratio, (int, float))
            or not math.isfinite(float(self.pupil_candidate_min_diameter_ratio))
            or not 0.0 <= float(self.pupil_candidate_min_diameter_ratio) <= 0.5
        ):
            raise ValueError("pupil_candidate_min_diameter_ratio must be between 0 and 0.5")
        if (
            isinstance(self.pupil_candidate_stability_frames, bool)
            or not isinstance(self.pupil_candidate_stability_frames, int)
            or not 1 <= self.pupil_candidate_stability_frames <= 10
        ):
            raise ValueError("pupil_candidate_stability_frames must be an integer between 1 and 10")
        for name in (
            "status_interval_seconds", "startup_timeout_seconds", "recording_ack_timeout_seconds",
            "shutdown_timeout_seconds", "stale_frame_timeout_seconds",
            "camera_reconnect_delay_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        for name in ("camera_read_failure_threshold", "camera_reconnect_attempts"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
                raise ValueError(f"{name} must be an integer between 1 and 20")
        if not isinstance(self.minimum_fps_ratio, (int, float)) or isinstance(self.minimum_fps_ratio, bool) or not 0 < float(self.minimum_fps_ratio) <= 1:
            raise ValueError("minimum_fps_ratio must be in (0, 1]")
        if not isinstance(self.max_black_percent, (int, float)) or isinstance(self.max_black_percent, bool) or not 0 <= float(self.max_black_percent) <= 100:
            raise ValueError("max_black_percent must be between 0 and 100")
        if isinstance(self.jpeg_quality, bool) or not isinstance(self.jpeg_quality, int) or not 30 <= self.jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be between 30 and 100")
        if not isinstance(self.data_dir, Path):
            raise ValueError("data_dir must be a pathlib.Path")
        if not isinstance(self.simulate, bool):
            raise ValueError("simulate must be boolean")
        for camera in self.cameras:
            camera.validate()
        return self

    def public_dict(self) -> dict[str, Any]:
        return {
            "cameras": [camera.public_dict() for camera in self.cameras],
            "preview_fps": self.preview_fps,
            "pupil_detector_fps": self.pupil_detector_fps,
            "pupil_confidence_threshold": self.pupil_confidence_threshold,
            "pupil_alignment_hold_seconds": self.pupil_alignment_hold_seconds,
            "pupil_alignment_center_tolerance": self.pupil_alignment_center_tolerance,
            "pupil_alignment_max_age_seconds": self.pupil_alignment_max_age_seconds,
            "pupil_candidate_min_y_ratio": self.pupil_candidate_min_y_ratio,
            "pupil_candidate_max_y_ratio": self.pupil_candidate_max_y_ratio,
            "pupil_candidate_max_axis_ratio": self.pupil_candidate_max_axis_ratio,
            "pupil_candidate_max_jump_ratio": self.pupil_candidate_max_jump_ratio,
            "pupil_candidate_min_diameter_ratio": self.pupil_candidate_min_diameter_ratio,
            "pupil_candidate_stability_frames": self.pupil_candidate_stability_frames,
            "preview_guide_margin_x": self.preview_guide_margin_x,
            "preview_guide_margin_y": self.preview_guide_margin_y,
            "status_interval_seconds": self.status_interval_seconds,
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "recording_ack_timeout_seconds": self.recording_ack_timeout_seconds,
            "stale_frame_timeout_seconds": self.stale_frame_timeout_seconds,
            "camera_read_failure_threshold": self.camera_read_failure_threshold,
            "camera_reconnect_attempts": self.camera_reconnect_attempts,
            "camera_reconnect_delay_seconds": self.camera_reconnect_delay_seconds,
            "minimum_fps_ratio": self.minimum_fps_ratio,
            "max_black_percent": self.max_black_percent,
            "jpeg_quality": self.jpeg_quality,
            "simulate": self.simulate,
        }


@dataclass(frozen=True, slots=True)
class SessionMetadata:
    subject_code: str
    visit_id: str
    operator: str
    protocol: str = "双眼高速采集"
    age: int | None = None
    sex: str = "unknown"
    screening: str = "none"
    notes: str = ""
    consent_confirmed: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SessionMetadata":
        if not isinstance(raw, Mapping):
            raise ValueError("session metadata must be a JSON object")
        item = cls(
            subject_code=str(raw.get("subject_code", "")).strip(),
            visit_id=str(raw.get("visit_id", "")).strip(),
            operator=str(raw.get("operator", "")).strip(),
            protocol=str(raw.get("protocol", "双眼高速采集")).strip() or "双眼高速采集",
            age=raw.get("age"),
            sex=str(raw.get("sex", "unknown")),
            screening=str(raw.get("screening", "none")),
            notes=str(raw.get("notes", "")),
            consent_confirmed=raw.get("consent_confirmed", False),
        )
        return item.validate()

    def validate(self) -> "SessionMetadata":
        for name in ("subject_code", "visit_id"):
            value = getattr(self, name)
            if not SAFE_ID.fullmatch(value):
                raise ValueError(f"{name} must use 1-64 letters, numbers, '.', '_' or '-'")
        if not SAFE_OPERATOR.fullmatch(self.operator):
            raise ValueError("operator must contain 1-120 printable characters")
        if not SAFE_OPERATOR.fullmatch(self.protocol):
            raise ValueError("protocol contains unsupported control characters")
        age = _optional_int(self.age, "age", minimum=0, maximum=130)
        if self.sex not in ALLOWED_SEX:
            raise ValueError(f"sex must be one of {sorted(ALLOWED_SEX)}")
        if self.screening not in ALLOWED_SCREENINGS:
            raise ValueError(f"screening must be one of {sorted(ALLOWED_SCREENINGS)}")
        if not isinstance(self.notes, str) or len(self.notes) > 4000 or any(ord(ch) < 32 and ch not in "\n\r\t" for ch in self.notes):
            raise ValueError("notes must be at most 4000 characters")
        if not isinstance(self.consent_confirmed, bool) or not self.consent_confirmed:
            raise ValueError("consent_confirmed must be true before recording")
        object.__setattr__(self, "age", age)
        return self

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SessionRecord:
    session_id: str
    path: Path
    metadata: SessionMetadata
    state: str
    created_at: str
    started_at: str | None = None
    stopped_at: str | None = None
    error: str | None = None
    camera_files: dict[str, str] = field(default_factory=dict)
    camera_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    screening_result: dict[str, Any] | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "error": self.error,
            "metadata": self.metadata.public_dict(),
            "camera_files": dict(self.camera_files),
            "camera_stats": self.camera_stats,
            "screening_result": self.screening_result,
        }
