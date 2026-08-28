from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import csv
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Iterable, Mapping
import zipfile

from .models import SessionMetadata, SessionRecord, SAFE_ID, utc_now


SESSION_ID_PATTERN = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}\Z")
MAX_SCREENING_BYTES = 128 * 1024


def _fsync_replace(path: Path, payload: bytes) -> None:
    """Write a file atomically so a power loss cannot leave half a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


class SessionStore:
    """Persistent session store with path validation and atomic metadata writes."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _validate_session_id(self, session_id: str) -> str:
        if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError("invalid session id")
        return session_id

    def path_for(self, session_id: str) -> Path:
        session_id = self._validate_session_id(session_id)
        path = (self.root / session_id).resolve()
        if path.parent != self.root:
            raise ValueError("session path escapes data directory")
        return path

    def create(self, metadata: SessionMetadata, config: Mapping[str, Any]) -> SessionRecord:
        metadata.validate()
        # UTC timestamp plus random suffix keeps IDs sortable without exposing patient data.
        now = datetime.now(timezone.utc)
        session_id = f"{now:%Y%m%dT%H%M%SZ}-{os.urandom(4).hex()}"
        path = self.path_for(session_id)
        path.mkdir(mode=0o700)
        record = SessionRecord(
            session_id=session_id,
            path=path,
            metadata=metadata,
            state="starting",
            created_at=utc_now(),
        )
        _fsync_replace(path / "session.json", _json_bytes(record.public_dict()))
        _fsync_replace(path / "config.json", _json_bytes(dict(config)))
        _fsync_replace(
            path / "README.txt",
            (
                "Clinical Capture session\n"
                "=========================\n"
                "This folder contains research/clinical acquisition data.\n"
                "It is not a diagnostic report. Review quality metrics before analysis.\n"
            ).encode("utf-8"),
        )
        return record

    def save_record(self, record: SessionRecord) -> None:
        _fsync_replace(record.path / "session.json", _json_bytes(record.public_dict()))

    def save_quality(self, record: SessionRecord, quality: Mapping[str, Any]) -> None:
        _fsync_replace(record.path / "quality.json", _json_bytes(dict(quality)))

    def save_screening(self, record: SessionRecord, result: Mapping[str, Any]) -> dict[str, Any]:
        encoded = _json_bytes(dict(result))
        if len(encoded) > MAX_SCREENING_BYTES:
            raise ValueError("screening result is too large")
        _fsync_replace(record.path / "screening.json", encoded)
        record.screening_result = dict(result)
        self.save_record(record)
        return record.screening_result

    def finalize(
        self,
        record: SessionRecord,
        *,
        state: str,
        camera_files: Mapping[str, str],
        camera_stats: Mapping[str, Mapping[str, Any]],
        error: str | None = None,
    ) -> SessionRecord:
        if state not in {"completed", "error", "cancelled"}:
            raise ValueError("invalid final session state")
        record.state = state
        record.stopped_at = utc_now()
        record.error = error
        record.camera_files = dict(camera_files)
        record.camera_stats = {key: dict(value) for key, value in camera_stats.items()}
        quality = {
            "generated_at": utc_now(),
            "session_id": record.session_id,
            "state": state,
            "error": error,
            "cameras": record.camera_stats,
            "overall": self._overall_quality(record.camera_stats),
        }
        self.write_analysis_csv(
            record,
            [
                {
                    "session_id": record.session_id,
                    "camera": name,
                    "required": bool(value.get("required", True)),
                    "capture_fps": value.get("capture_fps", 0.0),
                    "recorded_frames": value.get("recorded_frames", 0),
                    "black_percent": value.get("black_percent", 0.0),
                    "frame_transform": value.get("frame_transform", "none"),
                    "transform_verified": bool(value.get("transform_verified", False)),
                    "reconnect_count": int(value.get("reconnect_count", 0) or 0),
                    "reconnect_attempts_total": int(value.get("reconnect_attempts_total", 0) or 0),
                    "quality_ok": bool(value.get("quality_ok", False)),
                    "pupil_samples": (value.get("pupil") or {}).get("samples", 0),
                    "pupil_valid_samples": (value.get("pupil") or {}).get("valid_samples", 0),
                    "pupil_valid_rate": (value.get("pupil") or {}).get("valid_rate", 0.0),
                    "pupil_detector_fps": (value.get("pupil") or {}).get("detector_fps", 0.0),
                    "pupil_latency_p95_ms": (value.get("pupil") or {}).get("latency_p95_ms", 0.0),
                }
                for name, value in record.camera_stats.items()
            ],
        )
        self.save_quality(record, quality)
        self.save_record(record)
        return record

    @staticmethod
    def _overall_quality(stats: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        required = [value for value in stats.values() if value.get("required", True)]
        if not required:
            return {"ready": False, "reason": "no required camera statistics"}
        failed = [value.get("name", "camera") for value in required if not value.get("quality_ok", False)]
        return {
            "ready": not failed,
            "failed_cameras": failed,
            "recorded_frames": sum(int(value.get("recorded_frames", 0)) for value in stats.values()),
        }

    def read_record(self, session_id: str) -> SessionRecord:
        path = self.path_for(session_id)
        try:
            payload = json.loads((path / "session.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"could not read session {session_id}: {error}") from error
        metadata = SessionMetadata.from_mapping(payload.get("metadata", {}))
        return SessionRecord(
            session_id=session_id,
            path=path,
            metadata=metadata,
            state=str(payload.get("state", "error")),
            created_at=str(payload.get("created_at", "")),
            started_at=payload.get("started_at"),
            stopped_at=payload.get("stopped_at"),
            error=payload.get("error"),
            camera_files=dict(payload.get("camera_files") or {}),
            camera_stats={key: dict(value) for key, value in (payload.get("camera_stats") or {}).items()},
            screening_result=payload.get("screening_result"),
        )

    def list_records(self) -> list[SessionRecord]:
        records: list[SessionRecord] = []
        for child in self.root.iterdir():
            if not child.is_dir() or not SESSION_ID_PATTERN.fullmatch(child.name):
                continue
            try:
                records.append(self.read_record(child.name))
            except ValueError:
                # A partially copied/corrupt session is shown by the audit endpoint, not as a valid record.
                continue
        records.sort(key=lambda record: record.created_at, reverse=True)
        return records

    def export_zip(self, session_id: str, destination: str | Path | None = None) -> Path:
        record = self.read_record(session_id)
        target = Path(destination) if destination is not None else self.root / f"{session_id}.zip"
        target = target.expanduser().resolve()
        if target.parent == record.path or target == record.path:
            raise ValueError("export destination cannot be inside the session")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for file in sorted(record.path.rglob("*")):
                    if not file.is_file():
                        continue
                    archive.write(file, arcname=file.relative_to(record.path).as_posix())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def write_analysis_csv(self, record: SessionRecord, rows: Iterable[Mapping[str, Any]]) -> Path:
        path = record.path / "analysis_ready.csv"
        row_list = [dict(row) for row in rows]
        fieldnames = sorted({key for row in row_list for key in row})
        if not fieldnames:
            fieldnames = ["session_id", "camera", "frame_index", "timestamp_ns"]
        fd, temporary_name = tempfile.mkstemp(prefix=".analysis.", suffix=".tmp", dir=record.path)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(row_list)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path
