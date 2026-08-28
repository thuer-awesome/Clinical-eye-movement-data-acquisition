"""PySide6 version 2 workstation UI.

The browser application remains available through :mod:`server`.  This module
adds a native, local-only operator surface that talks to the same
``ClinicalCaptureManager``.  Camera workers, Detector2D and the exporter are
therefore shared instead of being reimplemented in the UI.

The centered eye-orbit rectangle drawn over each preview is an operator aid.
It is not used by ``PupilDetectorService`` and cannot make a low-confidence
detection valid.
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import os
from pathlib import Path
import sys
import traceback
from typing import Any, Callable, Mapping

import cv2
import numpy as np

from .camera_settings import (
    FRAME_TRANSFORMS,
    MAX_CAMERA_INDEX,
    apply_camera_settings,
    load_camera_settings,
    save_camera_settings,
    settings_path_for,
)
from .manager import ClinicalCaptureManager
from .models import ClinicalConfig, SessionMetadata
from .server import DEFAULT_CONFIG_PATH, load_config


QT_AVAILABLE = False
QT_IMPORT_ERROR = ""


def guide_rect_normalized(config: ClinicalConfig) -> tuple[float, float, float, float]:
    """Return the centered eye-orbit guide rectangle used by V2 previews.

    The values are normalized ``(left, top, right, bottom)`` coordinates and
    are deliberately independent from ``pupil_alignment_center_tolerance``.
    """
    margin_x = max(0.0, min(0.44, float(config.preview_guide_margin_x)))
    margin_y = max(0.0, min(0.44, float(config.preview_guide_margin_y)))
    return margin_x, margin_y, 1.0 - margin_x, 1.0 - margin_y


def confidence_color_rgb(confidence: Any, threshold: Any = 0.6) -> tuple[int, int, int]:
    """Map real detector confidence to a low-to-high RGB display cue.

    The threshold places the green endpoint at the configured recording
    threshold. This helper is display-only and cannot change ``valid`` or the
    recording gate.
    """
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        value = 0.0
    try:
        target = float(threshold)
    except (TypeError, ValueError):
        target = 0.6
    value = max(0.0, min(1.0, value))
    target = max(0.01, min(1.0, target))
    progress = max(0.0, min(1.0, value / target))
    low = (239, 91, 91)
    middle = (244, 193, 70)
    high = (82, 205, 142)
    if progress <= 0.5:
        amount = progress * 2.0
        start, end = low, middle
    else:
        amount = (progress - 0.5) * 2.0
        start, end = middle, high
    return tuple(round(start[index] + (end[index] - start[index]) * amount) for index in range(3))


def direction_label(direction: Any) -> str:
    """Translate a structured detector direction to a compact Chinese label."""
    labels = {
        "left": "左",
        "right": "右",
        "up": "上",
        "down": "下",
        "left_up": "左上",
        "left_down": "左下",
        "right_up": "右上",
        "right_down": "右下",
        "center": "居中",
        "holding": "保持不动",
        "ready": "已完成",
        "search": "寻找瞳孔",
    }
    return labels.get(str(direction), "调整相机")


def camera_label(name: Any) -> str:
    return {"right_eye": "右眼", "left_eye": "左眼"}.get(str(name), str(name))


def state_label(value: Any) -> str:
    return {
        "idle": "待机",
        "starting": "启动中",
        "ready": "可采集",
        "recording": "采集中",
        "stopping": "保存中",
        "completed": "已完成",
        "error": "需处理",
    }.get(str(value), str(value or "未知"))


def camera_state_label(value: Any) -> str:
    return {
        "offline": "离线",
        "starting": "启动中",
        "reconnecting": "正在重连",
        "ready": "就绪",
        "recording": "录制中",
        "error": "故障",
    }.get(str(value), str(value or "未知"))


def recording_gate_status(
    status_payload: Mapping[str, Any] | None,
    *,
    fields_ready: bool | None = None,
    session_fields: Mapping[str, Any] | None = None,
    busy: bool = False,
) -> dict[str, Any]:
    """Explain whether the operator may start a recording.

    This is deliberately a display-side projection of the existing start
    conditions.  The manager remains the authoritative gate; this helper only
    turns its status payload into a stable label and actionable reasons for the
    operator.  Keeping it free of Qt types also makes the decision testable on
    headless CI runners.
    """
    status = status_payload if isinstance(status_payload, Mapping) else {}
    reasons: list[str] = []
    app_state = str(status.get("app_state") or "starting")
    simulate = bool(status.get("simulate"))
    status_error = str(status.get("status_error") or "").strip()
    if status_error:
        reasons.append(f"本地服务状态读取失败：{status_error}")

    if busy:
        reasons.append("正在处理上一项操作，请稍候")

    if app_state in {"recording", "stopping"}:
        reasons.append("当前会话正在录制或保存，请先停止当前会话")
    else:
        if app_state != "ready":
            reasons.append(f"采集设备状态为“{state_label(app_state)}”，尚未就绪")

        preflight = status.get("preflight")
        if not isinstance(preflight, Mapping):
            reasons.append("尚未收到设备预检结果")
        elif not bool(preflight.get("ready")):
            raw_reasons = preflight.get("blocking_reasons")
            added_preflight_reason = False
            if isinstance(raw_reasons, (list, tuple)):
                for reason in raw_reasons:
                    normalized = str(reason).strip()
                    if normalized:
                        reasons.append(normalized)
                        added_preflight_reason = True
            if not added_preflight_reason:
                reasons.append("设备预检尚未通过")

        settings = status.get("device_settings")
        settings_confirmed = isinstance(settings, Mapping) and bool(settings.get("confirmed"))
        if (
            not status_error
            and not simulate
            and not settings_confirmed
            and not any("映射" in reason or "画面方向" in reason for reason in reasons)
        ):
            reasons.append("左右眼相机映射和画面方向尚未确认")

        preflight = status.get("preflight")
        alignment = preflight.get("pupil_alignment") if isinstance(preflight, Mapping) else None
        if (
            not status_error
            and isinstance(alignment, Mapping)
            and alignment.get("required")
            and not alignment.get("ready")
            and not any("瞳孔" in reason or "对准" in reason for reason in reasons)
        ):
            # Manager preflight normally includes one reason per eye.  Add a
            # concise aggregate only when that detail is unavailable.
            guidance = str(alignment.get("guidance") or "请根据左右眼预览调整相机，等待瞳孔置信度稳定达标")
            reasons.append(f"双眼瞳孔对准未完成：{guidance}")

    is_recording = app_state in {"recording", "stopping"}
    if fields_ready is None:
        if isinstance(session_fields, Mapping):
            fields_ready = bool(
                str(session_fields.get("subject_code") or "").strip()
                and str(session_fields.get("visit_id") or "").strip()
                and str(session_fields.get("operator") or "").strip()
                and bool(session_fields.get("consent_confirmed"))
            )
        else:
            fields_ready = False
    if not fields_ready and not is_recording:
        if isinstance(session_fields, Mapping):
            missing: list[str] = []
            for key, label in (
                ("subject_code", "受试者编号"),
                ("visit_id", "访视编号"),
                ("operator", "操作员"),
            ):
                if not str(session_fields.get(key) or "").strip():
                    missing.append(label)
            if not bool(session_fields.get("consent_confirmed")):
                missing.append("知情同意确认")
            reasons.append(
                "会话信息未完成：" + "、".join(missing)
                if missing
                else "会话信息未完成：请检查必填字段和知情同意确认"
            )
        else:
            reasons.append("会话信息未完成：填写受试者编号、访视编号、操作员并确认知情同意")

    # Preserve the order (device, alignment, metadata) while avoiding repeated
    # messages when a backend payload already contains an aggregate reason.
    unique_reasons: list[str] = []
    for reason in reasons:
        if reason and reason not in unique_reasons:
            unique_reasons.append(reason)
    can_record = not unique_reasons
    return {
        "can_record": can_record,
        "ready": can_record,
        "label": "可以录制" if can_record else "暂不可录制",
        "reasons": unique_reasons,
        "blocking_reasons": unique_reasons,
    }


def recording_gate_reasons(
    status_payload: Mapping[str, Any] | None,
    *,
    fields_ready: bool | None = None,
    session_fields: Mapping[str, Any] | None = None,
    busy: bool = False,
) -> list[str]:
    """Return only the actionable reasons from :func:`recording_gate_status`."""
    return recording_gate_status(
        status_payload,
        fields_ready=fields_ready,
        session_fields=session_fields,
        busy=busy,
    )["reasons"]


def recording_reason_label(reason: Any) -> str:
    """Translate an internal gate reason into operator-facing wording."""
    text = str(reason or "").strip()
    for role in ("right_eye", "left_eye"):
        prefix = f"{role}:"
        if text.startswith(prefix):
            return f"{camera_label(role)}：{text[len(prefix):].lstrip()}"
    return text


def status_failure_payload(
    error: Any,
    *,
    previous_status: Mapping[str, Any] | None = None,
    simulate: bool = False,
    camera_names: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a fail-closed status when the manager cannot be queried.

    The payload intentionally looks like a normal manager status so the
    existing renderer and recording gate can consume it without a second UI
    error path.  A known active recording state is retained so the operator
    can still stop and save it; starting a new session remains impossible.
    """
    previous = previous_status if isinstance(previous_status, Mapping) else {}
    if "simulate" in previous:
        simulate = bool(previous.get("simulate"))
    detail = str(error).strip() or error.__class__.__name__
    reason = f"本地服务状态读取失败：{detail}"
    previous_state = str(previous.get("app_state") or "")
    active = previous_state in {"recording", "stopping"} or bool(previous.get("session"))
    app_state = previous_state if active else "error"

    old_preflight = previous.get("preflight")
    old_cameras = old_preflight.get("cameras") if isinstance(old_preflight, Mapping) else {}
    names = list(camera_names)
    if isinstance(old_cameras, Mapping):
        for name in old_cameras:
            if str(name) not in names:
                names.append(str(name))
    cameras: dict[str, dict[str, Any]] = {}
    for name in names:
        old = old_cameras.get(name) if isinstance(old_cameras, Mapping) else None
        item = dict(old) if isinstance(old, Mapping) else {}
        item.update(
            {
                "name": name,
                "state": "error",
                "error": reason,
                "capture_fps": 0.0,
                "last_frame_age_ms": None,
            }
        )
        old_pupil = item.get("pupil") if isinstance(item.get("pupil"), Mapping) else {}
        old_alignment = old_pupil.get("alignment") if isinstance(old_pupil.get("alignment"), Mapping) else {}
        try:
            threshold = float(old_alignment.get("threshold") or old_pupil.get("threshold") or 0.6)
        except (TypeError, ValueError):
            threshold = 0.6
        item["pupil"] = {
            "state": "error",
            "valid": False,
            "confidence": 0.0,
            "latency_ms": None,
            "age_ms": None,
            "guidance": reason,
            "alignment": {
                "state": "error",
                "ready": False,
                "progress": 0.0,
                "fresh": False,
                "confidence": 0.0,
                "threshold": threshold,
                "guidance": reason,
            },
        }
        cameras[name] = item

    return {
        "app_state": app_state,
        "status_error": detail,
        "last_error": reason,
        "simulate": bool(simulate),
        "uptime_s": previous.get("uptime_s", 0.0),
        "data_dir": previous.get("data_dir"),
        "session": previous.get("session") if active else None,
        "last_session": previous.get("last_session"),
        "preflight": {
            "ready": False,
            "state": "error",
            "blocking_reasons": [reason],
            "warnings": [],
            "cameras": cameras,
            "pupil_alignment": {
                "required": not bool(simulate),
                "ready": False,
                "progress": 0.0,
                "guidance": reason,
                "confidence_threshold": 0.6,
            },
        },
        "device_settings": {"confirmed": False, "error": reason},
    }


def _binding_issues(status_payload: Mapping[str, Any], *, stale_timeout_ms: float = 2500.0) -> list[str]:
    """Return hardware binding issues after a settings restart."""
    preflight = status_payload.get("preflight")
    cameras = preflight.get("cameras") if isinstance(preflight, Mapping) else None
    if not isinstance(cameras, Mapping):
        return ["未收到双眼相机状态"]
    issues: list[str] = []
    for role in ("right_eye", "left_eye"):
        item = cameras.get(role)
        if not isinstance(item, Mapping):
            issues.append(f"{camera_label(role)}：未收到状态")
            continue
        if item.get("state") not in {"ready", "recording"}:
            issues.append(f"{camera_label(role)}：{item.get('error') or '相机未就绪'}")
            continue
        fps = float(item.get("capture_fps") or 0.0)
        minimum = float(item.get("minimum_fps") or 0.0)
        if fps <= 0 or (minimum > 0 and fps < minimum):
            issues.append(f"{camera_label(role)}：帧率 {fps:.1f} 低于 {minimum:.1f}")
        age_ms = item.get("last_frame_age_ms")
        if isinstance(age_ms, (int, float)) and float(age_ms) > max(0.0, float(stale_timeout_ms)):
            issues.append(f"{camera_label(role)}：帧流超时")
    return issues


try:  # Importing this module remains possible on a headless CI runner.
    from PySide6.QtCore import (
        QPointF,
        QRectF,
        QSize,
        Qt,
        QTimer,
        QObject,
        QRunnable,
        QThreadPool,
        Signal,
        Slot,
    )
    from PySide6.QtGui import (
        QColor,
        QFont,
        QImage,
        QPainter,
        QPen,
        QPixmap,
        QPolygonF,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDoubleSpinBox,
        QFileDialog,
        QFormLayout,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSpinBox,
        QSplitter,
        QStyle,
        QTableWidget,
        QTableWidgetItem,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )

    QT_AVAILABLE = True
except ImportError as error:  # pragma: no cover - exercised when the optional UI extra is absent
    # Keep the original exception available to the launcher.  A frozen build
    # otherwise only reports the generic "PySide6 is required" message, which
    # makes a missing Qt DLL or plugin unnecessarily difficult to diagnose.
    QT_IMPORT_ERROR = "".join(traceback.format_exception(error)).strip()


if QT_AVAILABLE:

    class TaskSignals(QObject):
        succeeded = Signal(object)
        failed = Signal(str)
        completed = Signal()


    class CallableTask(QRunnable):
        """Run one manager operation outside the Qt GUI thread."""

        def __init__(self, function: Callable[[], Any]) -> None:
            super().__init__()
            self.function = function
            self.signals = TaskSignals()
            self.setAutoDelete(True)

        @Slot()
        def run(self) -> None:
            try:
                self.signals.succeeded.emit(self.function())
            except Exception as error:  # surface a concise message to the operator
                detail = str(error).strip() or error.__class__.__name__
                self.signals.failed.emit(detail)
            finally:
                self.signals.completed.emit()


    class PreviewWidget(QWidget):
        """Aspect-ratio-stable preview with a non-gating eye-orbit guide box."""

        def __init__(self, eye_name: str, guide_rect: tuple[float, float, float, float], parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.eye_name = eye_name
            self._guide_rect = guide_rect
            self._image: QImage | None = None
            self._alignment: dict[str, Any] = {}
            self._pupil: dict[str, Any] = {}
            self.setMinimumSize(360, 270)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.setToolTip("眼眶正对范围仅用于帮助调整镜头；录制门禁仍只使用真实瞳孔检测置信度和几何。")

        def sizeHint(self) -> QSize:
            return QSize(640, 480)

        def minimumSizeHint(self) -> QSize:
            return QSize(320, 240)

        def set_frame_bytes(self, payload: bytes | None) -> None:
            if not payload:
                return
            try:
                encoded = np.frombuffer(payload, dtype=np.uint8)
                frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
                if frame is None:
                    return
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = QImage(
                    rgb.data,
                    int(rgb.shape[1]),
                    int(rgb.shape[0]),
                    int(rgb.strides[0]),
                    QImage.Format.Format_RGB888,
                ).copy()
            except (cv2.error, ValueError, TypeError):
                return
            self._image = image
            self.update()

        def set_status(self, pupil: Mapping[str, Any] | None, alignment: Mapping[str, Any] | None) -> None:
            self._pupil = dict(pupil or {})
            self._alignment = dict(alignment or {})
            self.update()

        def _image_rect(self) -> QRectF:
            target = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
            if self._image is None or self._image.isNull():
                return target
            image_ratio = self._image.width() / max(1.0, float(self._image.height()))
            target_ratio = target.width() / max(1.0, target.height())
            if image_ratio > target_ratio:
                height = target.width() / image_ratio
                return QRectF(target.left(), target.top() + (target.height() - height) / 2.0, target.width(), height)
            width = target.height() * image_ratio
            return QRectF(target.left() + (target.width() - width) / 2.0, target.top(), width, target.height())

        def _draw_arrow(self, painter: QPainter, rect: QRectF, direction: str) -> None:
            vectors = {
                "left": (-1.0, 0.0), "right": (1.0, 0.0), "up": (0.0, -1.0), "down": (0.0, 1.0),
                "left_up": (-0.72, -0.72), "left_down": (-0.72, 0.72),
                "right_up": (0.72, -0.72), "right_down": (0.72, 0.72),
            }
            vector = vectors.get(direction)
            if vector is None:
                return
            center = rect.center()
            length = min(rect.width(), rect.height()) * 0.18
            end = QPointF(center.x() + vector[0] * length, center.y() + vector[1] * length)
            painter.setPen(QPen(QColor("#ffca6b"), 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(center, end)
            # A small triangular head keeps the cue visible without covering
            # the pupil ellipse in the middle of the preview.
            import math
            angle = math.atan2(vector[1], vector[0])
            head = length * 0.16
            left = QPointF(end.x() - head * math.cos(angle - 0.55), end.y() - head * math.sin(angle - 0.55))
            right = QPointF(end.x() - head * math.cos(angle + 0.55), end.y() - head * math.sin(angle + 0.55))
            painter.setBrush(QColor("#ffca6b"))
            painter.drawPolygon(QPolygonF([end, left, right]))

        def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt API name
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.fillRect(self.rect(), QColor("#071014"))
            image_rect = self._image_rect()
            if self._image is None or self._image.isNull():
                painter.setPen(QColor("#78908c"))
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "等待实时画面")
            else:
                pixmap = QPixmap.fromImage(self._image)
                scaled = pixmap.scaled(
                    max(1, int(round(image_rect.width()))),
                    max(1, int(round(image_rect.height()))),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                actual = QRectF(
                    image_rect.left() + (image_rect.width() - scaled.width()) / 2.0,
                    image_rect.top() + (image_rect.height() - scaled.height()) / 2.0,
                    scaled.width(),
                    scaled.height(),
                )
                painter.drawPixmap(actual, scaled, QRectF(scaled.rect()))
                image_rect = actual

            left_n, top_n, right_n, bottom_n = self._guide_rect
            guide = QRectF(
                image_rect.left() + image_rect.width() * left_n,
                image_rect.top() + image_rect.height() * top_n,
                image_rect.width() * (right_n - left_n),
                image_rect.height() * (bottom_n - top_n),
            )
            confidence = self._alignment.get("confidence", self._pupil.get("confidence", 0.0))
            threshold = self._alignment.get("threshold", self._pupil.get("threshold", 0.6))
            fresh = bool(self._alignment.get("fresh", self._pupil.get("fresh", False)))
            # A stale last detection must never leave a green-looking guide on
            # screen while the camera is disconnected or the detector is late.
            color = QColor(*confidence_color_rgb(confidence, threshold)) if fresh else QColor("#8b9a98")
            ready = bool(self._alignment.get("ready"))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(color, 2.0, Qt.PenStyle.SolidLine if ready else Qt.PenStyle.DashLine))
            painter.drawRect(guide)
            painter.setPen(QPen(QColor("#dfeeea"), 1.0, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(guide.center().x() - 10, guide.center().y()), QPointF(guide.center().x() + 10, guide.center().y()))
            painter.drawLine(QPointF(guide.center().x(), guide.center().y() - 10), QPointF(guide.center().x(), guide.center().y() + 10))
            painter.setPen(QColor("#dfeeea"))
            painter.setFont(QFont("Segoe UI", 9))
            painter.drawText(QPointF(guide.left() + 7, guide.top() + 16), "眼眶正对范围")

            direction = str(self._alignment.get("direction") or self._pupil.get("direction") or "search")
            if direction not in {"center", "holding", "ready", "search"} and not ready:
                self._draw_arrow(painter, guide, direction)

            # Keep the confidence and state in the preview itself so a full
            # screen operator view still has the information needed to act.
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = 0.0
            try:
                threshold = float(threshold)
            except (TypeError, ValueError):
                threshold = 0.0
            state = str(self._alignment.get("state") or "searching")
            text = f"置信度 {confidence:.2f}"
            if threshold:
                text += f" / 阈值 {threshold:.2f}"
            text += f"  {state}"
            painter.setPen(QColor("#e7f2ef"))
            painter.drawText(QPointF(image_rect.left() + 8, image_rect.bottom() - 9), text)
            painter.end()


    class EyeCard(QGroupBox):
        def __init__(self, camera_name: str, guide_rect: tuple[float, float, float, float], parent: QWidget | None = None) -> None:
            super().__init__(camera_label(camera_name), parent)
            self.camera_name = camera_name
            self.state_label = QLabel("等待")
            self.state_label.setObjectName("cameraState")
            self.mapping_label = QLabel("物理相机 --")
            self.mode_label = QLabel("等待实际模式")
            self.preview = PreviewWidget(camera_name, guide_rect)
            self.pupil_label = QLabel("瞳孔检测中")
            self.pupil_label.setWordWrap(True)
            self.alignment_label = QLabel("等待瞳孔对准提示")
            self.alignment_label.setWordWrap(True)
            self.alignment_label.setObjectName("alignmentLabel")
            self.progress = QProgressBar()
            self.progress.setRange(0, 100)
            self.progress.setTextVisible(False)
            self.fps_label = QLabel("FPS --")
            self.frames_label = QLabel("写入 0 帧")
            self.reconnect_label = QLabel("重连 0")
            self.quality_label = QLabel("黑帧 --")

            header = QHBoxLayout()
            header.addWidget(self.mapping_label, 1)
            header.addWidget(self.state_label)
            body = QVBoxLayout()
            body.addLayout(header)
            body.addWidget(self.mode_label)
            body.addWidget(self.preview, 1)
            body.addWidget(self.pupil_label)
            body.addWidget(self.alignment_label)
            body.addWidget(self.progress)
            metrics = QHBoxLayout()
            for label in (self.fps_label, self.frames_label, self.reconnect_label, self.quality_label):
                metrics.addWidget(label)
            body.addLayout(metrics)
            self.setLayout(body)

        def update_status(self, camera: Mapping[str, Any], simulate: bool) -> None:
            state = str(camera.get("state") or "offline")
            self.state_label.setText(camera_state_label(state))
            self.state_label.setProperty("state", state)
            self.state_label.style().unpolish(self.state_label)
            self.state_label.style().polish(self.state_label)
            self.mapping_label.setText(
                f"物理相机 {camera.get('index', '--')} · {'必需' if camera.get('required', True) else '可选'}"
            )
            transform = str(camera.get("frame_transform") or "none")
            transform_names = {"none": "正常", "rotate_180": "旋转 180°", "flip_horizontal": "水平镜像", "flip_vertical": "上下翻转"}
            verified = "方向已验证" if camera.get("transform_verified") else "等待方向验证"
            actual = camera.get("actual") or camera.get("requested") or "--"
            self.mode_label.setText(f"{actual} · {transform_names.get(transform, transform)} · {verified}")
            fps = float(camera.get("capture_fps") or 0.0)
            target = float(camera.get("target_fps") or 0.0)
            minimum = float(camera.get("minimum_fps") or 0.0)
            self.fps_label.setText(f"FPS {fps:.1f} / {target:.0f}")
            self.frames_label.setText(f"写入 {int(camera.get('recorded_frames') or 0):,} 帧")
            reconnects = int(camera.get("reconnect_count") or 0)
            self.reconnect_label.setText(f"重连 {reconnects}")
            self.reconnect_label.setProperty("warning", reconnects > 0)
            self.quality_label.setText(f"黑帧 {float(camera.get('black_percent') or 0.0):.1f}%")
            pupil = camera.get("pupil") if isinstance(camera.get("pupil"), Mapping) else {}
            alignment = pupil.get("alignment") if isinstance(pupil.get("alignment"), Mapping) else {}
            confidence = float(alignment.get("confidence") or pupil.get("confidence") or 0.0)
            threshold = float(alignment.get("threshold") or 0.0)
            if pupil.get("state") == "error":
                self.pupil_label.setText("瞳孔算法不可用")
            elif pupil.get("valid"):
                self.pupil_label.setText(f"已捕捉 · 置信度 {confidence:.2f} · 延迟 {float(pupil.get('latency_ms') or 0.0):.1f} ms")
            else:
                self.pupil_label.setText(f"未锁定 · 置信度 {confidence:.2f} / 阈值 {threshold:.2f}")
            guidance = "模拟模式 · 跳过真实瞳孔门禁" if simulate else str(alignment.get("guidance") or pupil.get("guidance") or "等待瞳孔对准提示")
            self.alignment_label.setText(guidance)
            self.alignment_label.setProperty("ready", bool(alignment.get("ready")))
            progress = max(0.0, min(1.0, float(alignment.get("progress") or 0.0)))
            self.progress.setValue(round(progress * 100))
            self.preview.set_status(pupil, alignment)


    class ClinicalCaptureWindow(QMainWindow):
        """Native V2 operator window backed by one ClinicalCaptureManager."""

        def __init__(
            self,
            manager: ClinicalCaptureManager,
            *,
            settings_path: Path,
            config_path: Path,
            autostart: bool = True,
        ) -> None:
            super().__init__()
            self.manager = manager
            self.settings_path = Path(settings_path)
            self.config_path = Path(config_path)
            self._status: dict[str, Any] | None = None
            self._busy = False
            self._closing = False
            self._tasks: set[CallableTask] = set()
            self._last_export_path: Path | None = None
            self.pool = QThreadPool(self)
            self.pool.setMaxThreadCount(3)
            self.setWindowTitle("Clinical Capture V2 · 双眼临床采集")
            self.resize(1500, 960)
            self.setMinimumSize(1100, 760)
            self._build_ui()
            self._apply_styles()
            # The manager has not returned a status yet.  Initialize the
            # controls from an empty (therefore fail-closed) gate before the
            # window can receive user input.
            self._update_controls()
            interval = max(50, round(1000.0 / max(1, manager.config.preview_fps)))
            self._status_timer = QTimer(self)
            self._status_timer.setInterval(250)
            self._status_timer.timeout.connect(self._poll_status)
            self._status_timer.start()
            self._preview_timer = QTimer(self)
            self._preview_timer.setInterval(interval)
            self._preview_timer.timeout.connect(self._poll_previews)
            self._preview_timer.start()
            if autostart:
                QTimer.singleShot(0, self._start_manager)

        def _apply_styles(self) -> None:
            self.setStyleSheet(
                """
                QWidget { color: #e8f1ee; background: #0b171b; font-family: 'Segoe UI', 'Microsoft YaHei'; font-size: 13px; }
                QMainWindow { background: #081216; }
                QGroupBox { border: 1px solid #294149; border-radius: 5px; margin-top: 12px; padding: 12px; background: #102127; font-weight: 650; }
                QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; color: #8ddacc; }
                QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit { border: 1px solid #2a444c; border-radius: 3px; padding: 6px; color: #e8f1ee; background: #0a171c; }
                QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus { border-color: #78d1c0; }
                QPushButton { min-height: 32px; border: 1px solid #31535b; border-radius: 3px; padding: 0 12px; color: #d8e9e5; background: #142c34; }
                QPushButton:hover:!disabled { border-color: #78d1c0; background: #1a3b42; }
                QPushButton:disabled { color: #60716f; background: #0e1b20; }
                QPushButton#startButton { border-color: #5bbda9; color: #071613; background: #78d1c0; font-weight: 700; }
                QPushButton#stopButton { border-color: #c86d6c; color: #ffd7d4; background: #4a282d; }
                QLabel#title { font-size: 24px; font-weight: 700; color: #f0f8f5; }
                QLabel#subtitle { color: #91aaa5; }
                QLabel#banner { border-left: 4px solid #ffca6b; padding: 9px 12px; color: #f5d6a0; background: #17282d; font-size: 14px; }
                QLabel#banner[ready="true"] { border-left-color: #78d1c0; color: #bdeee2; background: #153231; }
                QLabel#recordingGate { border: 1px solid #c86d6c; padding: 8px 10px; color: #ffd7d4; background: #3a2429; font-size: 17px; font-weight: 700; }
                QLabel#recordingGate[ready="true"] { border-color: #5bbda9; color: #c9f5e9; background: #153b35; }
                QLabel#recordingReasons { padding: 6px 8px; color: #f5d6a0; background: #13272d; }
                QLabel#recordingReasons[ready="true"] { color: #bdeee2; background: #153231; }
                QLabel#cameraState[state="ready"], QLabel#cameraState[state="recording"] { color: #78d1c0; }
                QLabel#cameraState[state="reconnecting"], QLabel#cameraState[state="starting"] { color: #ffca6b; }
                QLabel#cameraState[state="error"] { color: #ef8c87; }
                QLabel#alignmentLabel { color: #ffca6b; padding: 5px 7px; background: #13272d; }
                QLabel#alignmentLabel[ready="true"] { color: #78d1c0; background: #153231; }
                QProgressBar { height: 5px; border: 0; background: #0a171c; }
                QProgressBar::chunk { background: #ffca6b; }
                QTableWidget { border: 1px solid #294149; gridline-color: #1e363d; background: #0d1c21; }
                QHeaderView::section { padding: 5px; border: 0; color: #91aaa5; background: #13272d; }
                QScrollArea { border: 0; }
                """
            )

        def _build_ui(self) -> None:
            root = QWidget()
            outer = QVBoxLayout(root)
            outer.setContentsMargins(18, 16, 18, 16)
            outer.setSpacing(12)

            header = QHBoxLayout()
            title_box = QVBoxLayout()
            title = QLabel("Clinical Capture V2")
            title.setObjectName("title")
            subtitle = QLabel("Pupil Reproduction Detector2D · 双眼实时预览与临床采集")
            subtitle.setObjectName("subtitle")
            title_box.addWidget(title)
            title_box.addWidget(subtitle)
            header.addLayout(title_box, 1)
            self.mode_chip = QLabel("模式：启动中")
            self.state_chip = QLabel("状态：启动中")
            self.connection_chip = QLabel("本地服务：初始化")
            for chip in (self.mode_chip, self.state_chip, self.connection_chip):
                chip.setFrameShape(QFrame.Shape.StyledPanel)
                chip.setMargin(7)
                header.addWidget(chip)
            outer.addLayout(header)

            self.banner = QLabel("正在启动双路相机和瞳孔检测…")
            self.banner.setObjectName("banner")
            self.banner.setWordWrap(True)
            outer.addWidget(self.banner)

            splitter = QSplitter(Qt.Orientation.Horizontal)
            splitter.setChildrenCollapsible(False)
            splitter.addWidget(self._build_controls())
            splitter.addWidget(self._build_main_panel())
            splitter.setStretchFactor(0, 0)
            splitter.setStretchFactor(1, 1)
            splitter.setSizes([330, 1120])
            outer.addWidget(splitter, 1)
            self.setCentralWidget(root)

        def _build_controls(self) -> QWidget:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            panel = QWidget()
            layout = QVBoxLayout(panel)
            layout.setContentsMargins(0, 0, 8, 0)

            session_group = QGroupBox("会话信息")
            form = QFormLayout(session_group)
            self.subject_edit = QLineEdit()
            self.subject_edit.setPlaceholderText("例如 P-001（仅填脱敏编号）")
            self.visit_edit = QLineEdit()
            self.visit_edit.setPlaceholderText("例如 V1")
            self.operator_edit = QLineEdit()
            self.operator_edit.setPlaceholderText("操作员")
            self.age_spin = QSpinBox()
            self.age_spin.setRange(0, 130)
            self.age_spin.setSpecialValueText("未记录")
            self.sex_combo = QComboBox()
            self.sex_combo.addItem("未记录", "unknown")
            self.sex_combo.addItem("女", "female")
            self.sex_combo.addItem("男", "male")
            self.sex_combo.addItem("其他", "other")
            self.protocol_edit = QLineEdit("双眼高速采集")
            self.screening_combo = QComboBox()
            self.screening_combo.addItem("无", "none")
            self.screening_combo.addItem("病理筛查记录", "pathology")
            self.screening_combo.addItem("情绪筛查记录", "emotion")
            self.screening_combo.addItem("自定义", "custom")
            self.notes_edit = QTextEdit()
            self.notes_edit.setPlaceholderText("研究/筛查备注，不填写姓名、身份证号或病历号")
            self.notes_edit.setMaximumHeight(72)
            self.consent_check = QCheckBox("已完成知情同意与院内数据授权确认")
            form.addRow("受试者编号 *", self.subject_edit)
            form.addRow("访视编号 *", self.visit_edit)
            form.addRow("操作员 *", self.operator_edit)
            form.addRow("年龄", self.age_spin)
            form.addRow("性别", self.sex_combo)
            form.addRow("方案", self.protocol_edit)
            form.addRow("筛查标签", self.screening_combo)
            form.addRow("备注", self.notes_edit)
            form.addRow("", self.consent_check)
            for edit in (self.subject_edit, self.visit_edit, self.operator_edit):
                edit.textChanged.connect(self._update_controls)
            self.consent_check.toggled.connect(self._update_controls)
            layout.addWidget(session_group)

            actions = QGroupBox("采集控制")
            actions_layout = QVBoxLayout(actions)
            self.recording_gate_label = QLabel("暂不可录制")
            self.recording_gate_label.setObjectName("recordingGate")
            self.recording_gate_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.recording_reasons_label = QLabel("正在检查设备、瞳孔检测和会话信息…")
            self.recording_reasons_label.setObjectName("recordingReasons")
            self.recording_reasons_label.setWordWrap(True)
            actions_layout.addWidget(self.recording_gate_label)
            actions_layout.addWidget(self.recording_reasons_label)
            self.start_button = QPushButton("开始录制")
            self.start_button.setObjectName("startButton")
            self.start_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
            self.start_button.setToolTip("仅在双眼设备预检和真实瞳孔置信度门禁通过后可用")
            self.start_button.clicked.connect(self._start_session)
            self.stop_button = QPushButton("停止并保存")
            self.stop_button.setObjectName("stopButton")
            self.stop_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
            self.stop_button.clicked.connect(self._stop_session)
            self.preflight_button = QPushButton("重新检查设备")
            self.preflight_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
            self.preflight_button.clicked.connect(self._restart_manager)
            self.export_button = QPushButton("导出当前会话 ZIP")
            self.export_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
            self.export_button.clicked.connect(self._export_current)
            self.open_data_button = QPushButton("打开数据目录")
            self.open_data_button.clicked.connect(self._open_data_directory)
            for button in (self.start_button, self.stop_button, self.preflight_button, self.export_button, self.open_data_button):
                actions_layout.addWidget(button)
            self.operation_label = QLabel("等待设备状态")
            self.operation_label.setWordWrap(True)
            actions_layout.addWidget(self.operation_label)
            layout.addWidget(actions)

            device_group = QGroupBox("设备映射与方向")
            device_layout = QGridLayout(device_group)
            device_layout.addWidget(QLabel("逻辑眼位"), 0, 0)
            device_layout.addWidget(QLabel("物理索引"), 0, 1)
            device_layout.addWidget(QLabel("画面方向"), 0, 2)
            self.device_controls: dict[str, tuple[QSpinBox, QComboBox]] = {}
            config_by_name = {camera.name: camera for camera in self.manager.config.cameras}
            for row, name in enumerate(("right_eye", "left_eye"), start=1):
                camera = config_by_name.get(name)
                role_label = QLabel(camera_label(name))
                index = QSpinBox()
                index.setRange(0, MAX_CAMERA_INDEX)
                index.setValue(int(camera.index) if camera else row - 1)
                transform = QComboBox()
                transform.addItems(list(FRAME_TRANSFORMS))
                if camera:
                    transform.setCurrentText(camera.frame_transform)
                self.device_controls[name] = (index, transform)
                device_layout.addWidget(role_label, row, 0)
                device_layout.addWidget(index, row, 1)
                device_layout.addWidget(transform, row, 2)
            self.scan_button = QPushButton("扫描相机")
            self.scan_button.clicked.connect(self._scan_cameras)
            self.apply_device_button = QPushButton("保存并重启相机")
            self.apply_device_button.clicked.connect(self._apply_device_settings)
            device_layout.addWidget(self.scan_button, 3, 0, 1, 2)
            device_layout.addWidget(self.apply_device_button, 3, 2)
            self.device_status_label = QLabel("本机映射尚未确认")
            self.device_status_label.setWordWrap(True)
            device_layout.addWidget(self.device_status_label, 4, 0, 1, 3)
            layout.addWidget(device_group)

            review_group = QGroupBox("最近会话复核")
            review_layout = QVBoxLayout(review_group)
            self.review_table = QTableWidget(0, 3)
            self.review_table.setHorizontalHeaderLabels(["会话", "状态", "受试者"])
            self.review_table.horizontalHeader().setStretchLastSection(True)
            self.review_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.review_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            self.review_table.setMaximumHeight(150)
            review_layout.addWidget(self.review_table)
            self.review_label = QLabel("停止后可导出原始视频、标注视频、瞳孔 CSV 与两类报告")
            self.review_label.setWordWrap(True)
            review_layout.addWidget(self.review_label)
            layout.addWidget(review_group)
            layout.addStretch(1)
            scroll.setWidget(panel)
            return scroll

        def _build_main_panel(self) -> QWidget:
            panel = QWidget()
            layout = QVBoxLayout(panel)
            layout.setContentsMargins(0, 0, 0, 0)
            guide = guide_rect_normalized(self.manager.config)
            self.eye_cards: dict[str, EyeCard] = {}
            previews = QHBoxLayout()
            previews.setSpacing(12)
            for name in ("right_eye", "left_eye"):
                card = EyeCard(name, guide)
                self.eye_cards[name] = card
                previews.addWidget(card, 1)
            layout.addLayout(previews, 4)

            quality_group = QGroupBox("实时质量与导出结果")
            quality_layout = QVBoxLayout(quality_group)
            self.quality_summary = QLabel("等待设备状态")
            self.quality_summary.setWordWrap(True)
            quality_layout.addWidget(self.quality_summary)
            self.export_path_label = QLabel("导出路径：尚未导出")
            self.export_path_label.setWordWrap(True)
            quality_layout.addWidget(self.export_path_label)
            layout.addWidget(quality_group, 0)
            return panel

        def _submit(
            self,
            function: Callable[[], Any],
            *,
            operation: str,
            success: Callable[[Any], None] | None = None,
        ) -> None:
            if self._busy and not self._closing:
                return
            self._busy = True
            self.operation_label.setText(operation)
            self._update_controls()
            task = CallableTask(function)
            self._tasks.add(task)

            def on_success(result: Any) -> None:
                self._busy = False
                self._tasks.discard(task)
                if success is not None:
                    success(result)
                self._update_controls()

            def on_failed(message: str) -> None:
                self._busy = False
                self._tasks.discard(task)
                self.operation_label.setText(f"{operation}失败：{message}")
                self.banner.setText(message)
                self.banner.setProperty("ready", False)
                self.banner.style().unpolish(self.banner)
                self.banner.style().polish(self.banner)
                if not self._closing:
                    QMessageBox.warning(self, "Clinical Capture", message)
                self._update_controls()

            task.signals.succeeded.connect(on_success)
            task.signals.failed.connect(on_failed)
            self.pool.start(task)

        def _start_manager(self) -> None:
            self._submit(self.manager.start, operation="正在启动相机和 Detector2D…", success=self._on_manager_started)

        def _on_manager_started(self, status: Any) -> None:
            self._status = status if isinstance(status, dict) else self.manager.status()
            self.operation_label.setText("设备启动完成，等待实时预览")
            self._render_status()

        def _poll_status(self) -> None:
            if self._closing:
                return
            try:
                status = self.manager.status()
            except Exception as error:
                detail = str(error).strip() or error.__class__.__name__
                camera_names = tuple(
                    str(camera.name)
                    for camera in getattr(getattr(self.manager, "config", None), "cameras", ())
                    if getattr(camera, "name", None)
                )
                self._status = status_failure_payload(
                    error,
                    previous_status=self._status,
                    simulate=bool(getattr(getattr(self.manager, "config", None), "simulate", False)),
                    camera_names=camera_names,
                )
                self.operation_label.setText(f"状态读取失败：{detail}")
                self._render_status()
                return
            self._status = status
            self._render_status()

        def _poll_previews(self) -> None:
            if self._closing:
                return
            guide = guide_rect_normalized(self.manager.config)
            for name, card in self.eye_cards.items():
                try:
                    payload = self.manager.preview_jpeg(name, guide_rect=None)
                except Exception:
                    payload = None
                if payload:
                    card.preview.set_frame_bytes(payload)
                # ``guide`` is painted by PreviewWidget, after the JPEG has
                # been scaled, so it remains crisp at any window size.
                if card.preview._guide_rect != guide:
                    card.preview._guide_rect = guide
                    card.preview.update()

        def _render_status(self) -> None:
            status = self._status or {}
            simulate = bool(status.get("simulate"))
            app_state = str(status.get("app_state") or "starting")
            preflight = status.get("preflight") if isinstance(status.get("preflight"), Mapping) else {}
            self.mode_chip.setText(f"模式：{'模拟' if simulate else '现场'}")
            self.state_chip.setText(f"状态：{state_label(app_state)}")
            status_error = str(status.get("status_error") or "").strip()
            self.connection_chip.setText(
                f"本地服务：错误 · {status_error}" if status_error else "本地服务：在线"
            )
            settings = status.get("device_settings") if isinstance(status.get("device_settings"), Mapping) else {}
            confirmed = bool(settings.get("confirmed")) or simulate
            self.device_status_label.setText(
                "本机映射已确认" if confirmed else "本机映射尚未确认：请扫描、选择左右眼和画面方向后保存"
            )

            cameras = preflight.get("cameras") if isinstance(preflight.get("cameras"), Mapping) else {}
            for name, card in self.eye_cards.items():
                item = cameras.get(name)
                if isinstance(item, Mapping):
                    card.update_status(item, simulate)

            gate = recording_gate_status(
                status,
                session_fields=self._session_fields(),
                busy=self._busy,
            )
            alignment = preflight.get("pupil_alignment") if isinstance(preflight.get("pupil_alignment"), Mapping) else {}
            if app_state in {"recording", "stopping"}:
                message = "正在采集：原始 AVI 和时间戳直接写盘；如短暂掉线，等待页面显示重连结果。"
            elif gate["can_record"]:
                message = "设备预检、双眼瞳孔门禁和会话信息均已通过，可以开始录制。"
            else:
                first_reason = recording_reason_label(gate["reasons"][0]) if gate["reasons"] else "等待设备预检"
                message = f"暂不可录制：{first_reason}"
            self.banner.setText(message)
            self.banner.setProperty("ready", bool(gate["can_record"]))
            self.banner.style().unpolish(self.banner)
            self.banner.style().polish(self.banner)

            required = [value for value in cameras.values() if isinstance(value, Mapping) and value.get("required", True)]
            fps_values = [float(value.get("capture_fps") or 0.0) for value in required if float(value.get("capture_fps") or 0.0) > 0]
            reconnects = sum(int(value.get("reconnect_count") or 0) for value in required)
            quality_bits = [
                f"必需设备 {sum(1 for value in required if value.get('state') in {'ready', 'recording'})}/{len(required)}",
                f"平均 FPS {sum(fps_values) / len(fps_values):.1f}" if fps_values else "平均 FPS --",
                f"重连 {reconnects} 次",
                f"置信度阈值 {float(alignment.get('confidence_threshold') or self.manager.config.pupil_confidence_threshold):.2f}",
            ]
            last = status.get("last_session") if isinstance(status.get("last_session"), Mapping) else None
            if last:
                stats = last.get("camera_stats") if isinstance(last.get("camera_stats"), Mapping) else {}
                pupil_rates = []
                for value in stats.values():
                    if isinstance(value, Mapping):
                        pupil = value.get("pupil") if isinstance(value.get("pupil"), Mapping) else {}
                        if pupil.get("valid_rate") is not None:
                            pupil_rates.append(f"{camera_label(value.get('name'))} 有效率 {float(pupil.get('valid_rate') or 0.0):.1f}%")
                if pupil_rates:
                    quality_bits.append("；".join(pupil_rates))
            self.quality_summary.setText(" · ".join(quality_bits))
            self._render_recording_gate(gate)
            self._refresh_review_table()
            self._update_controls()

        def _session_fields(self) -> dict[str, Any]:
            """Return the operator-entered fields used by the display gate."""
            return {
                "subject_code": self.subject_edit.text().strip(),
                "visit_id": self.visit_edit.text().strip(),
                "operator": self.operator_edit.text().strip(),
                "consent_confirmed": self.consent_check.isChecked(),
            }

        def _render_recording_gate(self, gate: Mapping[str, Any]) -> None:
            """Render one unambiguous status and its actionable blockers."""
            can_record = bool(gate.get("can_record"))
            self.recording_gate_label.setText(str(gate.get("label") or "暂不可录制"))
            self.recording_gate_label.setProperty("ready", can_record)
            self.recording_gate_label.style().unpolish(self.recording_gate_label)
            self.recording_gate_label.style().polish(self.recording_gate_label)
            reasons = [recording_reason_label(reason) for reason in gate.get("reasons", []) if str(reason).strip()]
            if can_record:
                text = (
                    "设备预检和会话信息均已通过（模拟模式跳过真实瞳孔门禁）。"
                    if bool(self._status and self._status.get("simulate"))
                    else "设备预检、双眼瞳孔门禁和会话信息均已通过。"
                )
            else:
                text = "阻塞原因：\n" + "\n".join(f"- {reason}" for reason in reasons)
            self.recording_reasons_label.setText(text)
            self.recording_reasons_label.setProperty("ready", can_record)
            self.recording_reasons_label.style().unpolish(self.recording_reasons_label)
            self.recording_reasons_label.style().polish(self.recording_reasons_label)

        def _update_controls(self, *_args: Any) -> None:
            status = self._status or {}
            app_state = str(status.get("app_state") or "starting")
            recording = app_state in {"recording", "stopping"}
            gate = recording_gate_status(status, session_fields=self._session_fields(), busy=self._busy)
            self._render_recording_gate(gate)
            can_start = bool(gate.get("can_record"))
            if can_start:
                self.start_button.setToolTip("设备、瞳孔门禁和会话信息均已通过，可以开始录制")
            else:
                reasons = "；".join(recording_reason_label(reason) for reason in gate.get("reasons", []))
                self.start_button.setToolTip(f"暂不可录制：{reasons}")
            self.start_button.setEnabled(can_start)
            self.stop_button.setEnabled(not self._busy and recording)
            self.preflight_button.setEnabled(not self._busy and not recording)
            self.export_button.setEnabled(not self._busy and bool(status.get("last_session")))
            self.scan_button.setEnabled(not self._busy and not recording)
            self.apply_device_button.setEnabled(not self._busy and not recording)
            for index, transform in self.device_controls.values():
                index.setEnabled(not self._busy and not recording)
                transform.setEnabled(not self._busy and not recording)
            self.subject_edit.setEnabled(not recording and not self._busy)
            self.visit_edit.setEnabled(not recording and not self._busy)
            self.operator_edit.setEnabled(not recording and not self._busy)
            self.consent_check.setEnabled(not recording and not self._busy)

        def _metadata(self) -> SessionMetadata:
            age = self.age_spin.value() or None
            return SessionMetadata.from_mapping(
                {
                    "subject_code": self.subject_edit.text().strip(),
                    "visit_id": self.visit_edit.text().strip(),
                    "operator": self.operator_edit.text().strip(),
                    "age": age,
                    "sex": self.sex_combo.currentData(),
                    "protocol": self.protocol_edit.text().strip(),
                    "screening": self.screening_combo.currentData(),
                    "notes": self.notes_edit.toPlainText(),
                    "consent_confirmed": self.consent_check.isChecked(),
                }
            )

        def _start_session(self) -> None:
            try:
                metadata = self._metadata()
            except ValueError as error:
                QMessageBox.warning(self, "会话信息不完整", str(error))
                return
            self._submit(lambda: self.manager.start_session(metadata), operation="正在确认门禁并开始录制…", success=self._on_session_started)

        def _on_session_started(self, record: Any) -> None:
            session_id = getattr(record, "session_id", "")
            self.operation_label.setText(f"已开始录制：{session_id}")
            self.banner.setText("录制已开始。请保持受试者和镜头稳定，页面中的重连次数会写入质量报告。")

        def _stop_session(self) -> None:
            self._submit(self.manager.stop_session, operation="正在停止并保存会话…", success=self._on_session_stopped)

        def _on_session_stopped(self, record: Any) -> None:
            session_id = getattr(record, "session_id", "") if record is not None else ""
            self.operation_label.setText(f"会话已保存：{session_id}")
            self.banner.setText("会话已保存。请检查质量与瞳孔有效率，再导出 ZIP。")
            self._refresh_review_table()

        def _export_current(self) -> None:
            status = self._status or {}
            last = status.get("last_session") if isinstance(status.get("last_session"), Mapping) else None
            session_id = str(last.get("session_id")) if last else ""
            if not session_id:
                QMessageBox.information(self, "导出", "当前没有可导出的已停止会话。")
                return
            self._submit(lambda: self.manager.export_session(session_id), operation="正在生成标注视频和两类报告…", success=self._on_exported)

        def _on_exported(self, path: Any) -> None:
            self._last_export_path = Path(path) if path else None
            self.export_path_label.setText(f"导出路径：{self._last_export_path or '未知'}")
            self.operation_label.setText("导出完成：包含原始 AVI、标注 AVI、瞳孔 CSV 和报告")
            if self._last_export_path:
                QMessageBox.information(self, "导出完成", f"已生成：\n{self._last_export_path}")

        def _refresh_review_table(self) -> None:
            try:
                records = self.manager.sessions()[-12:]
            except Exception:
                return
            self.review_table.setRowCount(len(records))
            for row, record in enumerate(reversed(records)):
                metadata = record.get("metadata") if isinstance(record.get("metadata"), Mapping) else {}
                values = (
                    str(record.get("session_id") or "--"),
                    state_label(record.get("state")),
                    str(metadata.get("subject_code") or "--"),
                )
                for column, value in enumerate(values):
                    self.review_table.setItem(row, column, QTableWidgetItem(value))

        def _restart_manager(self) -> None:
            self._submit(self.manager.restart, operation="正在释放相机句柄并重新检查…", success=self._on_manager_started)

        def _scan_cameras(self) -> None:
            self._submit(lambda: self.manager.scan_cameras(max_index=MAX_CAMERA_INDEX), operation="正在扫描物理相机，请稍候…", success=self._on_scan_result)

        def _on_scan_result(self, result: Any) -> None:
            candidates = result.get("cameras", []) if isinstance(result, Mapping) else []
            if not candidates:
                QMessageBox.warning(self, "扫描相机", "没有发现可用相机。请检查 USB、Windows 相机权限和占用程序。")
                return
            lines = [
                f"物理索引 {item.get('index')}：{item.get('width')}×{item.get('height')} @ {item.get('fps')} fps"
                for item in candidates if isinstance(item, Mapping)
            ]
            QMessageBox.information(self, "扫描相机结果", "\n".join(lines) + "\n\n请根据缩略图手动选择右眼/左眼并保存。")

        def _apply_device_settings(self) -> None:
            values = {
                name: {"index": index.value(), "frame_transform": transform.currentText()}
                for name, (index, transform) in self.device_controls.items()
            }
            previous = self.manager.config
            previous_confirmed = self.manager.device_settings_confirmed
            previous_error = self.manager.device_settings_error

            def operation() -> Mapping[str, Any]:
                candidate = apply_camera_settings(previous, {"version": 1, "confirmed": True, "cameras": values})
                status = self.manager.reconfigure(candidate, device_settings_confirmed=True, device_settings_error=None)
                issues = _binding_issues(
                    status,
                    stale_timeout_ms=self.manager.config.stale_frame_timeout_seconds * 1000.0,
                )
                if issues and not candidate.simulate:
                    self.manager.reconfigure(previous, device_settings_confirmed=previous_confirmed, device_settings_error=previous_error)
                    raise RuntimeError("新设置未通过检查，已恢复原设置：" + "；".join(issues))
                save_camera_settings(self.settings_path, candidate)
                return status

            self._submit(operation, operation="正在保存映射并重启相机…", success=lambda status: self._on_settings_saved(status, values))

        def _on_settings_saved(self, status: Any, values: Mapping[str, Any]) -> None:
            self.operation_label.setText("相机映射已保存，方向将在首帧进入处理链后验证")
            for name, settings in values.items():
                controls = self.device_controls.get(name)
                if controls:
                    controls[0].setValue(int(settings["index"]))
                    controls[1].setCurrentText(str(settings["frame_transform"]))
            self._status = status if isinstance(status, dict) else self.manager.status()
            self._render_status()

        def _open_data_directory(self) -> None:
            path = Path(self.manager.config.data_dir)
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:  # pragma: no cover - convenience for development on Linux
                import subprocess
                subprocess.Popen(["xdg-open", str(path)])

        def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API name
            if self._closing:
                event.accept()
                return
            self._closing = True
            event.ignore()
            self._status_timer.stop()
            self._preview_timer.stop()
            self.operation_label.setText("正在安全停止录制和相机…")

            def finished(_result: Any) -> None:
                self.pool.waitForDone(3000)
                QApplication.instance().quit()

            self._submit(self.manager.close, operation="正在安全停止录制和相机…", success=finished)


else:

    class ClinicalCaptureWindow:  # pragma: no cover - informative fallback
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("PySide6 is not installed. Install requirements-clinical-capture-v2.txt")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clinical Capture V2 native PySide6 workstation")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    default_data_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ClinicalCapture" / "data"
    parser.add_argument("--data-dir", type=Path, default=default_data_dir)
    parser.add_argument("--simulate", action="store_true", help="use visible synthetic camera streams")
    parser.add_argument("--no-start", action="store_true", help="open the UI without starting cameras (development only)")
    return parser


def create_window(args: argparse.Namespace) -> ClinicalCaptureWindow:
    """Create a V2 window and its manager without starting the Qt event loop."""
    config_path = Path(args.config)
    config = load_config(config_path, data_dir=args.data_dir, simulate=True if args.simulate else None)
    settings_path = settings_path_for(config.data_dir)
    config, confirmed, settings_error = load_camera_settings(config, settings_path)
    if config.simulate:
        confirmed = True
    manager = ClinicalCaptureManager(config, device_settings_confirmed=confirmed, device_settings_error=settings_error)
    window = ClinicalCaptureWindow(
        manager,
        settings_path=settings_path,
        config_path=config_path,
        autostart=not args.no_start,
    )
    return window


def _report_startup_error(data_dir: Path, message: str) -> None:
    """Leave an actionable trace when a windowed build cannot start Qt."""
    log_text = f"Clinical Capture V2 startup failed\n\n{message}\n"
    targets = [data_dir / "clinical_capture_startup_error.log"]
    if getattr(sys, "frozen", False):
        targets.append(Path(sys.executable).resolve().parent / "clinical_capture_startup_error.log")
    for target in targets:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(log_text, encoding="utf-8")
            break
        except OSError:
            continue
    if os.name == "nt" and getattr(sys, "frozen", False):
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                f"Clinical Capture V2 启动失败。\n\n{message}\n\n请把 clinical_capture_startup_error.log 交给维护人员。",
                "Clinical Capture V2",
                0x10,
            )
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    mp.freeze_support()
    args = build_parser().parse_args(argv)
    try:
        args.data_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Keep the original launch error useful even if the selected data path
        # is not writable; the fallback beside the executable may still work.
        pass
    if not QT_AVAILABLE:
        detail = f"\n\n{QT_IMPORT_ERROR}" if QT_IMPORT_ERROR else ""
        message = (
            "PySide6 is required for Clinical Capture V2. "
            "Install requirements-clinical-capture-v2.txt" + detail
        )
        _report_startup_error(args.data_dir, message)
        raise SystemExit(message)
    logging.basicConfig(
        filename=str(args.data_dir / "clinical_capture.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Clinical Capture V2")
    app.setStyle("Fusion")
    window = create_window(args)
    window.show()
    exit_code = app.exec()
    # The close event normally handles this.  Calling close is idempotent and
    # covers headless test/application termination paths as well.
    if not window._closing:
        window.manager.close()
    return int(exit_code)


if __name__ == "__main__":  # pragma: no cover - use qt_main in packaged builds
    raise SystemExit(main())
