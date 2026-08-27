from __future__ import annotations

import base64
from dataclasses import replace
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
from typing import Any, Mapping

import cv2

from .models import ClinicalConfig


CAMERA_ROLES = ("right_eye", "left_eye")
FRAME_TRANSFORMS = ("none", "rotate_180", "flip_horizontal", "flip_vertical")
MAX_CAMERA_INDEX = 15
SETTINGS_VERSION = 1


def settings_path_for(data_dir: str | Path) -> Path:
    return Path(data_dir).resolve().parent / "camera-settings.json"


def settings_document(config: ClinicalConfig) -> dict[str, Any]:
    by_name = {camera.name: camera for camera in config.cameras}
    return {
        "version": SETTINGS_VERSION,
        "confirmed": True,
        "cameras": {
            role: {
                "index": by_name[role].index,
                "frame_transform": by_name[role].frame_transform,
            }
            for role in CAMERA_ROLES
        },
    }


def apply_camera_settings(config: ClinicalConfig, raw: Mapping[str, Any]) -> ClinicalConfig:
    if not isinstance(raw, Mapping):
        raise ValueError("camera settings must be a JSON object")
    extra = set(raw) - {"version", "confirmed", "cameras"}
    if extra:
        raise ValueError(f"unknown camera settings fields: {sorted(extra)}")
    version = raw.get("version", SETTINGS_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version != SETTINGS_VERSION:
        raise ValueError(f"camera settings version must be {SETTINGS_VERSION}")
    if raw.get("confirmed") is not True:
        raise ValueError("camera settings must be explicitly confirmed")
    cameras = raw.get("cameras")
    if not isinstance(cameras, Mapping):
        raise ValueError("camera settings cameras must be an object")
    if set(cameras) != set(CAMERA_ROLES):
        raise ValueError("camera settings must contain right_eye and left_eye")

    parsed: dict[str, tuple[int, str]] = {}
    for role in CAMERA_ROLES:
        item = cameras.get(role)
        if not isinstance(item, Mapping):
            raise ValueError(f"{role} settings must be an object")
        item_extra = set(item) - {"index", "frame_transform"}
        if item_extra:
            raise ValueError(f"unknown {role} settings fields: {sorted(item_extra)}")
        index = item.get("index")
        transform = item.get("frame_transform")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= MAX_CAMERA_INDEX:
            raise ValueError(f"{role} index must be an integer between 0 and {MAX_CAMERA_INDEX}")
        if not isinstance(transform, str) or transform not in FRAME_TRANSFORMS:
            raise ValueError(f"{role} frame_transform is unsupported")
        parsed[role] = (index, transform)
    if parsed["right_eye"][0] == parsed["left_eye"][0]:
        raise ValueError("right_eye and left_eye must use different camera indices")

    configured_names = {camera.name for camera in config.cameras}
    if not set(CAMERA_ROLES).issubset(configured_names):
        raise ValueError("clinical configuration must contain right_eye and left_eye")
    updated = tuple(
        replace(camera, index=parsed[camera.name][0], frame_transform=parsed[camera.name][1])
        if camera.name in parsed
        else camera
        for camera in config.cameras
    )
    return replace(config, cameras=updated).validate()


def load_camera_settings(config: ClinicalConfig, path: str | Path) -> tuple[ClinicalConfig, bool, str | None]:
    settings_path = Path(path)
    if not settings_path.exists():
        return config, False, None
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("camera settings must be a JSON object")
        return apply_camera_settings(config, payload), True, None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        return config, False, f"本机相机设置无效，已使用默认配置: {error}"


def save_camera_settings(path: str | Path, config: ClinicalConfig) -> None:
    settings_path = Path(path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings_path.with_name(f".{settings_path.name}.tmp")
    encoded = json.dumps(settings_document(config), ensure_ascii=False, indent=2) + "\n"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(settings_path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _probe_camera_once(
    index: int,
    width: int,
    height: int,
    fps: int,
    jpeg_quality: int,
) -> dict[str, Any] | None:
    capture: cv2.VideoCapture | None = None
    try:
        cv2.setNumThreads(1)
        capture = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        capture.set(cv2.CAP_PROP_FPS, fps)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not capture.isOpened():
            return None
        frame = None
        for _ in range(3):
            success, candidate = capture.read()
            if success and candidate is not None:
                frame = candidate
        if frame is None:
            return None
        actual_height, actual_width = frame.shape[:2]
        preview = frame
        if actual_width > 480:
            preview_height = max(1, round(actual_height * 480 / actual_width))
            preview = cv2.resize(frame, (480, preview_height), interpolation=cv2.INTER_AREA)
        encoded_ok, encoded = cv2.imencode(
            ".jpg",
            preview,
            [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)],
        )
        return {
            "index": index,
            "width": int(actual_width),
            "height": int(actual_height),
            "fps": round(float(capture.get(cv2.CAP_PROP_FPS) or 0.0), 1),
            "preview_data_url": (
                "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")
                if encoded_ok
                else None
            ),
        }
    except Exception:
        return None
    finally:
        if capture is not None:
            capture.release()


def _probe_camera_child(
    result_queue: Any,
    index: int,
    width: int,
    height: int,
    fps: int,
    jpeg_quality: int,
) -> None:
    result_queue.put(_probe_camera_once(index, width, height, fps, jpeg_quality))


def probe_camera_indices(
    *,
    max_index: int = 3,
    width: int = 320,
    height: int = 240,
    fps: int = 240,
    jpeg_quality: int = 95,
    timeout_seconds: float = 4.0,
) -> list[dict[str, Any]]:
    upper = min(MAX_CAMERA_INDEX, max(0, int(max_index)))
    results: list[dict[str, Any]] = []
    context = mp.get_context("spawn")
    for index in range(upper + 1):
        result_queue = context.Queue(maxsize=1)
        process = context.Process(
            name=f"clinical-camera-probe-{index}",
            target=_probe_camera_child,
            args=(result_queue, index, width, height, fps, jpeg_quality),
        )
        started = False
        try:
            process.start()
            started = True
            try:
                result = result_queue.get(timeout=max(0.5, float(timeout_seconds)))
            except queue.Empty:
                result = None
            if isinstance(result, dict):
                results.append(result)
        except Exception:
            pass
        finally:
            if started:
                process.join(timeout=0.5)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=1.0)
            result_queue.close()
            result_queue.join_thread()
    return results
