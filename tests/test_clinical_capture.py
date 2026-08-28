from __future__ import annotations

import json
import hashlib
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import patch

import cv2
import numpy as np

from apps.clinical_capture.camera_settings import (
    apply_camera_settings,
    load_camera_settings,
    save_camera_settings,
    settings_document,
)
from apps.clinical_capture.exporter import export_session
from apps.clinical_capture.manager import ClinicalCaptureManager
from apps.clinical_capture.models import CameraConfig, ClinicalConfig, SessionMetadata, SessionState
from apps.clinical_capture.pupil import (
    PupilDetectorService,
    _alignment_direction,
    _alignment_guidance,
    guide_rect_pixels,
    pupil_candidate_quality,
)
from apps.clinical_capture.qt_app import (
    QT_AVAILABLE,
    confidence_color_rgb,
    direction_label,
    guide_rect_normalized,
    recording_gate_reasons,
    recording_reason_label,
    recording_gate_status,
    status_failure_payload,
)
from apps.clinical_capture.server import create_app
from apps.clinical_capture.storage import SessionStore
from apps.clinical_capture.worker import (
    _close_recording,
    _open_recording,
    _reconnect_capture,
    _recording_duration_seconds,
    normalize_frame,
)
from apps.multicamera_capture.shared_frame import FrameSnapshot


class ClinicalModelTests(unittest.TestCase):
    def test_alignment_direction_is_structured_for_qt_guidance(self) -> None:
        self.assertEqual(
            _alignment_direction(
                width=320,
                height=240,
                center_x=24.0,
                center_y=210.0,
                geometry_ok=True,
                center_tolerance=0.30,
            ),
            "left_down",
        )
        self.assertEqual(direction_label("left_down"), "左下")

    def test_operator_guide_is_wide_and_independent_of_gate(self) -> None:
        rect = guide_rect_pixels(320, 240, margin_x=0.16, margin_y=0.20)
        self.assertEqual(rect, (51, 48, 268, 191))
        config = ClinicalConfig.defaults(simulate=True)
        self.assertEqual(guide_rect_normalized(config), (0.16, 0.20, 0.84, 0.80))
        self.assertIn(QT_AVAILABLE, (True, False))

    def test_confidence_color_transitions_from_low_to_high(self) -> None:
        low = confidence_color_rgb(0.0, 0.6)
        near = confidence_color_rgb(0.3, 0.6)
        high = confidence_color_rgb(0.6, 0.6)
        above = confidence_color_rgb(0.95, 0.6)
        self.assertEqual(low, (239, 91, 91))
        self.assertGreater(near[1], low[1])
        self.assertEqual(high, (82, 205, 142))
        self.assertEqual(above, high)

    def test_alignment_guidance_points_toward_the_offset(self) -> None:
        message = _alignment_guidance(
            "right_eye",
            width=320,
            height=240,
            center_x=24.0,
            center_y=210.0,
            confidence=0.35,
            threshold=0.6,
            geometry_ok=True,
            error=None,
            center_tolerance=0.30,
        )
        self.assertIn("左、下", message)
        self.assertIn("微调", message)

    def test_pupil_candidate_quality_rejects_brow_edges_and_clipped_ellipses(self) -> None:
        accepted = pupil_candidate_quality(
            frame_width=320,
            frame_height=240,
            center_x=160,
            center_y=120,
            axis_width=18,
            axis_height=14,
            diameter=18,
        )
        self.assertTrue(accepted["ok"])

        elongated = pupil_candidate_quality(
            frame_width=320,
            frame_height=240,
            center_x=160,
            center_y=40,
            axis_width=40,
            axis_height=8,
            diameter=40,
        )
        self.assertFalse(elongated["ok"])
        self.assertEqual(elongated["reason"], "ellipse_too_elongated")

        tiny_reflection = pupil_candidate_quality(
            frame_width=320,
            frame_height=240,
            center_x=160,
            center_y=120,
            axis_width=12,
            axis_height=10,
            diameter=12,
        )
        self.assertFalse(tiny_reflection["ok"])
        self.assertEqual(tiny_reflection["reason"], "pupil_too_small")

        clipped = pupil_candidate_quality(
            frame_width=320,
            frame_height=240,
            center_x=2,
            center_y=2,
            axis_width=20,
            axis_height=20,
            diameter=20,
        )
        self.assertFalse(clipped["ok"])
        self.assertEqual(clipped["reason"], "pupil_not_fully_exposed")

    def test_pupil_candidate_quality_rejects_one_frame_position_jump(self) -> None:
        result = pupil_candidate_quality(
            frame_width=320,
            frame_height=240,
            center_x=300,
            center_y=220,
            axis_width=18,
            axis_height=14,
            diameter=18,
            previous={"x": 160, "y": 120, "diameter": 18},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "temporal_jump")
        self.assertGreater(float(result["temporal_distance"]), 0.35)

    def test_rejected_brow_candidate_never_emits_directional_guidance(self) -> None:
        class Detector:
            def detect(self, _gray):
                return {
                    "confidence": 0.99,
                    "ellipse": {"center": (32.0, 8.0), "axes": (30.0, 5.0), "angle": 0.0},
                    "diameter": 30.0,
                }

        service = PupilDetectorService("right_eye", 0, owner=None)
        snapshot = FrameSnapshot(1, time.perf_counter_ns(), np.zeros((48, 64), dtype=np.uint8))
        try:
            service._detect_snapshot(snapshot, Detector())
            status = service.status()
            self.assertFalse(status["valid"])
            self.assertFalse(status["quality_ok"])
            self.assertEqual(status["direction"], "search")
            self.assertFalse(status["alignment"]["ready"])
            self.assertIn(status["rejected_reason"], {"ellipse_too_elongated", "upper_lid_or_brow_candidate"})
            self.assertTrue("眉毛" in status["guidance"] or "眼睑" in status["guidance"])
            self.assertNotIn("微调", status["guidance"])
        finally:
            service.close()

    def test_detector_error_is_visible_as_error_not_ready(self) -> None:
        class Detector:
            def detect(self, _gray):
                raise RuntimeError("native detector unavailable")

        service = PupilDetectorService("right_eye", 0, owner=None)
        snapshot = FrameSnapshot(1, time.perf_counter_ns(), np.zeros((48, 64), dtype=np.uint8))
        try:
            service._detect_snapshot(snapshot, Detector())
            status = service.status()
            self.assertEqual(status["state"], "error")
            self.assertEqual(status["quality_state"], "error")
            self.assertEqual(status["rejected_reason"], "detector_error")
            self.assertFalse(status["alignment"]["ready"])
            self.assertIn("算法异常", status["guidance"])
        finally:
            service.close()

    def test_persistent_position_jump_requires_multiple_candidate_frames(self) -> None:
        class Detector:
            center = (20.0, 24.0)

            def detect(self, _gray):
                return {
                    "confidence": 0.95,
                    "ellipse": {"center": self.center, "axes": (12.0, 10.0), "angle": 0.0},
                    "diameter": 12.0,
                }

        detector = Detector()
        service = PupilDetectorService(
            "right_eye",
            0,
            owner=None,
            candidate_stability_frames=3,
        )
        frame = np.zeros((48, 64), dtype=np.uint8)
        try:
            service._detect_snapshot(FrameSnapshot(1, time.perf_counter_ns(), frame), detector)
            self.assertTrue(service.status()["quality_ok"])

            detector.center = (50.0, 24.0)
            service._detect_snapshot(FrameSnapshot(2, time.perf_counter_ns(), frame), detector)
            self.assertFalse(service.status()["quality_ok"])
            self.assertEqual(service.status()["rejected_reason"], "temporal_jump")
            self.assertEqual(service.status()["direction"], "search")

            service._detect_snapshot(FrameSnapshot(3, time.perf_counter_ns(), frame), detector)
            self.assertFalse(service.status()["quality_ok"])
            service._detect_snapshot(FrameSnapshot(4, time.perf_counter_ns(), frame), detector)
            self.assertTrue(service.status()["quality_ok"])
            self.assertTrue(service.status()["temporal_stable"])
        finally:
            service.close()

    def test_reset_alignment_requires_a_new_candidate_after_a_session(self) -> None:
        class Detector:
            def detect(self, _gray):
                return {
                    "confidence": 0.95,
                    "ellipse": {"center": (32.0, 24.0), "axes": (18.0, 12.0), "angle": 0.0},
                    "diameter": 15.0,
                }

        service = PupilDetectorService(
            "right_eye",
            0,
            owner=None,
            alignment_hold_seconds=0.08,
            alignment_max_age_seconds=0.5,
        )
        frame = np.zeros((48, 64), dtype=np.uint8)
        try:
            service._detect_snapshot(FrameSnapshot(1, time.perf_counter_ns(), frame), Detector())
            # The service enforces a 0.1 s minimum hold for deterministic
            # safety; leave scheduling margin instead of sleeping exactly at
            # the threshold.
            time.sleep(0.16)
            self.assertTrue(service.status()["alignment"]["ready"])
            service.reset_alignment()
            reset = service.status()
            self.assertFalse(reset["alignment"]["ready"])
            self.assertEqual(reset["alignment"]["state"], "searching")
            self.assertEqual(reset["direction"], "search")
            self.assertEqual(reset["rejected_reason"], "session_reset")
        finally:
            service.close()

    def test_recording_gate_explains_each_missing_requirement(self) -> None:
        status = {
            "app_state": "ready",
            "simulate": False,
            "device_settings": {"confirmed": True},
            "preflight": {
                "ready": False,
                "blocking_reasons": [
                    "right_eye: 帧率 80.0 低于 156.0",
                    "left_eye: 请将左眼整只瞳孔移入画面",
                ],
                "pupil_alignment": {
                    "required": True,
                    "ready": False,
                    "guidance": "请将左眼相机向左微调",
                },
            },
        }
        result = recording_gate_status(
            status,
            session_fields={
                "subject_code": "",
                "visit_id": "V1",
                "operator": "",
                "consent_confirmed": False,
            },
        )
        self.assertFalse(result["can_record"])
        self.assertEqual(result["label"], "暂不可录制")
        self.assertIn("right_eye: 帧率 80.0 低于 156.0", result["reasons"])
        self.assertIn("left_eye: 请将左眼整只瞳孔移入画面", result["reasons"])
        self.assertTrue(any("会话信息未完成" in reason for reason in result["reasons"]))
        self.assertEqual(recording_reason_label("right_eye: 帧率过低"), "右眼：帧率过低")

    def test_recording_gate_reports_ready_only_when_all_conditions_pass(self) -> None:
        status = {
            "app_state": "ready",
            "simulate": True,
            "device_settings": {"confirmed": True},
            "preflight": {
                "ready": True,
                "blocking_reasons": [],
                "pupil_alignment": {"required": False, "ready": True},
            },
        }
        fields = {
            "subject_code": "P-001",
            "visit_id": "V1",
            "operator": "Dr",
            "consent_confirmed": True,
        }
        result = recording_gate_status(status, session_fields=fields)
        self.assertTrue(result["can_record"])
        self.assertEqual(result["label"], "可以录制")
        self.assertEqual(recording_gate_reasons(status, session_fields=fields), [])

    def test_recording_gate_never_reports_ready_while_session_is_active(self) -> None:
        status = {
            "app_state": "recording",
            "simulate": True,
            "preflight": {"ready": True},
        }
        result = recording_gate_status(
            status,
            session_fields={
                "subject_code": "P-001",
                "visit_id": "V1",
                "operator": "Dr",
                "consent_confirmed": True,
            },
        )
        self.assertFalse(result["can_record"])
        self.assertTrue(any("当前会话正在录制或保存" in reason for reason in result["reasons"]))

    def test_status_failure_payload_fails_closed_and_exposes_connection_error(self) -> None:
        payload = status_failure_payload(
            RuntimeError("status socket closed"),
            previous_status={"app_state": "ready", "simulate": False},
            camera_names=("right_eye", "left_eye"),
        )
        self.assertEqual(payload["app_state"], "error")
        self.assertEqual(payload["status_error"], "status socket closed")
        self.assertFalse(payload["preflight"]["ready"])
        self.assertIn("本地服务状态读取失败：status socket closed", payload["preflight"]["blocking_reasons"])
        self.assertEqual(set(payload["preflight"]["cameras"]), {"right_eye", "left_eye"})
        gate = recording_gate_status(
            payload,
            session_fields={
                "subject_code": "P-001",
                "visit_id": "V1",
                "operator": "Dr",
                "consent_confirmed": True,
            },
        )
        self.assertFalse(gate["can_record"])
        self.assertTrue(any("本地服务状态读取失败" in reason for reason in gate["reasons"]))
        self.assertFalse(any("瞳孔对准未完成" in reason for reason in gate["reasons"]))

    def test_status_failure_payload_keeps_stop_path_for_active_recording(self) -> None:
        payload = status_failure_payload(
            "temporary status failure",
            previous_status={"app_state": "recording", "simulate": True, "session": {"session_id": "S1"}},
            camera_names=("right_eye",),
        )
        self.assertEqual(payload["app_state"], "recording")
        self.assertIsNotNone(payload["session"])

    def test_status_failure_payload_keeps_stop_path_for_pending_session(self) -> None:
        payload = status_failure_payload(
            "temporary status failure",
            previous_status={"app_state": "starting", "simulate": False, "session": {"session_id": "S2"}},
            camera_names=("right_eye", "left_eye"),
        )
        self.assertEqual(payload["app_state"], "stopping")
        self.assertIsNotNone(payload["session"])
        gate = recording_gate_status(
            payload,
            session_fields={
                "subject_code": "P-002",
                "visit_id": "V1",
                "operator": "Dr",
                "consent_confirmed": True,
            },
        )
        self.assertFalse(gate["can_record"])
        self.assertTrue(any("当前会话正在录制或保存" in reason for reason in gate["reasons"]))

    def test_empty_status_is_fail_closed_before_first_manager_poll(self) -> None:
        result = recording_gate_status(
            None,
            session_fields={
                "subject_code": "P-001",
                "visit_id": "V1",
                "operator": "Dr",
                "consent_confirmed": True,
            },
        )
        self.assertFalse(result["can_record"])
        self.assertTrue(any("尚未收到设备预检结果" in reason for reason in result["reasons"]))

    def test_recording_gate_keeps_unspecified_mapping_and_alignment_visible(self) -> None:
        status = {
            "app_state": "ready",
            "simulate": False,
            "preflight": {
                "ready": False,
                "blocking_reasons": ["右眼：帧率不足"],
                "pupil_alignment": {
                    "required": True,
                    "ready": False,
                    "guidance": "请将左眼相机向左微调",
                },
            },
        }
        result = recording_gate_status(
            status,
            session_fields={
                "subject_code": "P-001",
                "visit_id": "V1",
                "operator": "Dr",
                "consent_confirmed": True,
            },
        )
        self.assertFalse(result["can_record"])
        self.assertIn("左右眼相机映射和画面方向尚未确认", result["reasons"])
        self.assertTrue(any("双眼瞳孔对准未完成" in reason for reason in result["reasons"]))

    def test_recording_gate_rejects_invalid_session_identifiers_before_click(self) -> None:
        status = {
            "app_state": "ready",
            "simulate": True,
            "preflight": {"ready": True, "blocking_reasons": [], "pupil_alignment": {"required": False, "ready": True}},
        }
        result = recording_gate_status(
            status,
            session_fields={
                "subject_code": "../patient",
                "visit_id": "V/1",
                "operator": "Dr",
                "consent_confirmed": True,
            },
        )
        self.assertFalse(result["can_record"])
        self.assertTrue(any("受试者编号格式不正确" in reason for reason in result["reasons"]))

    def test_recording_gate_rejects_unverified_transform_and_inconsistent_preflight(self) -> None:
        status = {
            "app_state": "ready",
            "simulate": False,
            "device_settings": {"confirmed": True},
            "preflight": {
                "ready": True,
                "blocking_reasons": ["设备状态刚刚变为未知"],
                "pupil_alignment": {"required": True, "ready": True},
                "cameras": {
                    "right_eye": {"state": "ready", "capture_fps": 240.0, "transform_verified": False},
                    "left_eye": {"state": "ready", "capture_fps": 240.0, "transform_verified": True},
                },
            },
        }
        result = recording_gate_status(
            status,
            session_fields={
                "subject_code": "P-001",
                "visit_id": "V1",
                "operator": "Dr",
                "consent_confirmed": True,
            },
        )
        self.assertFalse(result["can_record"])
        self.assertIn("设备状态刚刚变为未知", result["reasons"])
        self.assertTrue(any("right_eye: 画面方向尚未通过首帧验证" in reason for reason in result["reasons"]))

    def test_recording_gate_rejects_unsaved_camera_settings(self) -> None:
        status = {
            "app_state": "ready",
            "simulate": True,
            "preflight": {"ready": True, "blocking_reasons": [], "pupil_alignment": {"required": False, "ready": True}},
        }
        result = recording_gate_status(
            status,
            session_fields={
                "subject_code": "P-001",
                "visit_id": "V1",
                "operator": "Dr",
                "consent_confirmed": True,
            },
            device_settings_dirty=True,
        )
        self.assertFalse(result["can_record"])
        self.assertTrue(any("尚未保存" in reason for reason in result["reasons"]))

    def test_pupil_alignment_requires_a_continuous_confident_hold(self) -> None:
        class Detector:
            def detect(self, _gray):
                return {
                    "confidence": 0.95,
                    "ellipse": {"center": (32.0, 24.0), "axes": (18.0, 12.0), "angle": 0.0},
                    "diameter": 15.0,
                }

        service = PupilDetectorService(
            "right_eye",
            0,
            owner=None,  # _detect_snapshot is exercised directly with a deterministic frame.
            alignment_hold_seconds=0.08,
            alignment_max_age_seconds=0.5,
        )
        snapshot = FrameSnapshot(1, time.perf_counter_ns(), np.zeros((48, 64), dtype=np.uint8))
        try:
            service._detect_snapshot(snapshot, Detector())
            first = service.status()["alignment"]
            self.assertFalse(first["ready"])
            self.assertEqual(first["state"], "holding")
            time.sleep(0.16)
            self.assertTrue(service.status()["alignment"]["ready"])
            self.assertAlmostEqual(service.status()["alignment"]["progress"], 1.0, places=2)
        finally:
            service.close()

    def test_operator_guide_overlay_does_not_change_detection_validity(self) -> None:
        class Detector:
            def detect(self, _gray):
                return {
                    "confidence": 0.95,
                    "ellipse": {"center": (32.0, 24.0), "axes": (18.0, 12.0), "angle": 0.0},
                    "diameter": 15.0,
                }

        service = PupilDetectorService(
            "right_eye",
            0,
            owner=None,
            alignment_hold_seconds=0.08,
            alignment_max_age_seconds=0.5,
        )
        snapshot = FrameSnapshot(1, time.perf_counter_ns(), np.zeros((48, 64), dtype=np.uint8))
        try:
            service._detect_snapshot(snapshot, Detector())
            before = service.status()
            frame = service.overlay(snapshot.frame, guide_rect=(0.12, 0.10, 0.88, 0.90))
            self.assertEqual(frame.shape, (48, 64, 3))
            after = service.status()
            self.assertEqual(before["valid"], after["valid"])
            self.assertEqual(before["confidence"], after["confidence"])
            self.assertEqual(before["alignment"]["center_ok"], after["alignment"]["center_ok"])
        finally:
            service.close()

    def test_default_configuration_keeps_camera_zero_valid(self) -> None:
        config = ClinicalConfig.defaults(simulate=True)
        self.assertEqual(config.cameras[1].index, 0)
        self.assertTrue(config.cameras[0].required)
        self.assertEqual([camera.name for camera in config.cameras], ["right_eye", "left_eye"])

    def test_metadata_requires_consent_and_safe_identifiers(self) -> None:
        with self.assertRaisesRegex(ValueError, "consent"):
            SessionMetadata.from_mapping({"subject_code": "P1", "visit_id": "V1", "operator": "Dr"})
        metadata = SessionMetadata.from_mapping(
            {
                "subject_code": "P-001",
                "visit_id": "V1",
                "operator": "Dr Test",
                "consent_confirmed": True,
            }
        )
        self.assertTrue(metadata.consent_confirmed)
        with self.assertRaises(ValueError):
            SessionMetadata.from_mapping(
                {"subject_code": "../escape", "visit_id": "V1", "operator": "Dr", "consent_confirmed": True}
            )

    def test_normalize_frame_applies_orientation(self) -> None:
        spec = CameraConfig(name="eye", index=0, width=4, height=3, fps=240, frame_transform="rotate_180")
        frame = np.arange(12, dtype=np.uint8).reshape(3, 4)
        result = normalize_frame(frame, spec)
        np.testing.assert_array_equal(result, frame[::-1, ::-1])

    def test_recording_duration_uses_last_capture_timestamp(self) -> None:
        self.assertAlmostEqual(
            _recording_duration_seconds(3_500_000_000, 2_000_000_000),
            1.5,
        )
        self.assertEqual(_recording_duration_seconds(1_000, 2_000), 0.0)

    def test_high_quality_mjpg_recording_is_readable_and_keeps_detail(self) -> None:
        spec = CameraConfig(name="eye", index=0, width=64, height=48, fps=240)
        frame = np.random.default_rng(1299).integers(0, 256, (48, 64), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as root:
            writer, metadata, _csv, filename, is_color, codec = _open_recording(spec, Path(root))
            try:
                self.assertEqual(codec, "MJPG q95")
                self.assertTrue(is_color)
                writer.write(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR))
            finally:
                _close_recording(writer, metadata)
            capture = cv2.VideoCapture(str(Path(root) / filename))
            try:
                ok, decoded = capture.read()
            finally:
                capture.release()
            self.assertTrue(ok)
            self.assertIsNotNone(decoded)
            difference = decoded[:, :, 0].astype(np.float32) - frame.astype(np.float32)
            mse = float(np.mean(difference * difference))
            psnr = float("inf") if mse == 0 else 10.0 * np.log10((255.0 * 255.0) / mse)
            self.assertGreater(psnr, 38.0)

    def test_recording_close_reports_resource_errors_without_raising(self) -> None:
        class BrokenWriter:
            def release(self) -> None:
                raise RuntimeError("writer already disconnected")

        class BrokenMetadata:
            def flush(self) -> None:
                raise OSError("volume unavailable")

            def close(self) -> None:
                raise OSError("volume unavailable")

        close_error = _close_recording(BrokenWriter(), BrokenMetadata())
        self.assertIsNotNone(close_error)
        self.assertIn("video writer close failed", close_error or "")
        self.assertIn("frame metadata flush failed", close_error or "")

    def test_configuration_uses_high_quality_preview_and_separate_record_codec(self) -> None:
        config = ClinicalConfig.defaults(simulate=True)
        self.assertEqual(config.jpeg_quality, 95)
        self.assertTrue(all(camera.fourcc == "MJPG" for camera in config.cameras))
        self.assertTrue(all(camera.record_fourcc == "MJPG" for camera in config.cameras))

    def test_camera_reconnect_is_bounded_and_reports_attempts(self) -> None:
        spec = CameraConfig(name="eye", index=0, width=4, height=3, fps=240)
        statuses: queue.Queue = queue.Queue()
        stop_event = threading.Event()

        class FakeCapture:
            def __init__(self) -> None:
                self.released = False

            def release(self) -> None:
                self.released = True

        old = FakeCapture()
        fresh = FakeCapture()
        calls = iter([RuntimeError("device busy"), fresh])

        def open_capture(_spec):
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value

        with patch("apps.clinical_capture.worker._open_capture", side_effect=open_capture), patch(
            "apps.clinical_capture.worker._actual_mode", return_value="4x3@240.0 FOURCC='MJPG'"
        ):
            result, actual, used = _reconnect_capture(
                spec,
                old,
                statuses,
                stop_event,
                attempts=2,
                delay_seconds=0.05,
                reconnect_count=1,
            )

        self.assertIs(result, fresh)
        self.assertEqual(actual, "4x3@240.0 FOURCC='MJPG'")
        self.assertEqual(used, 2)
        self.assertTrue(old.released)
        events = []
        while not statuses.empty():
            events.append(statuses.get_nowait())
        self.assertEqual([event["kind"] for event in events], ["reconnecting", "reconnect_failed", "reconnecting", "reconnected"])
        self.assertEqual(events[-1]["frame_transform"], "none")

    def test_reconnect_messages_do_not_refresh_frame_watchdog(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = ClinicalCaptureManager(ClinicalConfig.defaults(data_dir=root, simulate=True))
            try:
                manager._status_queue = queue.Queue()
                manager._drain_status()
                manager._status_queue.put(
                    {
                        "kind": "stats",
                        "camera": "right_eye",
                        "capture_fps": 240.0,
                        "recording": False,
                        "recorded_frames": 0,
                    }
                )
                manager._drain_status()
                first = manager._cameras["right_eye"]["last_frame_monotonic"]
                manager._cameras["right_eye"]["transform_verified"] = True
                manager._status_queue.put({"kind": "reconnecting", "camera": "right_eye", "attempt": 1, "attempts": 3})
                manager._drain_status()
                self.assertEqual(manager._cameras["right_eye"]["last_frame_monotonic"], first)
                self.assertFalse(manager._cameras["right_eye"]["transform_verified"])
            finally:
                # Restore the multiprocessing queue contract used by close().
                manager._status_queue = None
                manager.close()

    def test_zero_or_duplicate_stats_do_not_refresh_frame_watchdog(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = ClinicalCaptureManager(ClinicalConfig.defaults(data_dir=root, simulate=True))
            try:
                manager._status_queue = queue.Queue()
                manager._status_queue.put(
                    {
                        "kind": "stats",
                        "camera": "right_eye",
                        "sequence": 10,
                        "timestamp_ns": 10_000,
                        "frames_in_interval": 2,
                        "capture_fps": 240.0,
                        "recording": False,
                        "recorded_frames": 0,
                    }
                )
                manager._drain_status()
                first = manager._cameras["right_eye"]["last_frame_monotonic"]
                self.assertIsNotNone(first)
                manager._status_queue.put(
                    {
                        "kind": "stats",
                        "camera": "right_eye",
                        "sequence": 10,
                        "timestamp_ns": 10_000,
                        "frames_in_interval": 0,
                        "capture_fps": 0.0,
                        "recording": False,
                        "recorded_frames": 0,
                    }
                )
                manager._drain_status()
                self.assertEqual(manager._cameras["right_eye"]["last_frame_monotonic"], first)
            finally:
                manager._status_queue = None
                manager.close()

    def test_camera_settings_validate_mapping_and_preserve_capture_parameters(self) -> None:
        base = ClinicalConfig.defaults(simulate=True)
        payload = {
            "version": 1,
            "confirmed": True,
            "cameras": {
                "right_eye": {"index": 4, "frame_transform": "flip_horizontal"},
                "left_eye": {"index": 3, "frame_transform": "rotate_180"},
            },
        }
        updated = apply_camera_settings(base, payload)
        by_name = {camera.name: camera for camera in updated.cameras}
        self.assertEqual(by_name["right_eye"].index, 4)
        self.assertEqual(by_name["left_eye"].index, 3)
        self.assertEqual(by_name["right_eye"].frame_transform, "flip_horizontal")
        self.assertEqual(by_name["right_eye"].fps, base.cameras[0].fps)
        self.assertEqual(base.cameras[0].index, 1)

        duplicate = json.loads(json.dumps(payload))
        duplicate["cameras"]["left_eye"]["index"] = 4
        with self.assertRaisesRegex(ValueError, "different camera indices"):
            apply_camera_settings(base, duplicate)
        unconfirmed = json.loads(json.dumps(payload))
        unconfirmed["confirmed"] = False
        with self.assertRaisesRegex(ValueError, "explicitly confirmed"):
            apply_camera_settings(base, unconfirmed)
        unknown = json.loads(json.dumps(payload))
        unknown["cameras"]["right_eye"]["exposure"] = -8
        with self.assertRaisesRegex(ValueError, "unknown right_eye"):
            apply_camera_settings(base, unknown)

    def test_camera_settings_round_trip_and_invalid_file_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = ClinicalConfig.defaults(data_dir=Path(root) / "data", simulate=True)
            updated = apply_camera_settings(
                base,
                {
                    "version": 1,
                    "confirmed": True,
                    "cameras": {
                        "right_eye": {"index": 2, "frame_transform": "none"},
                        "left_eye": {"index": 5, "frame_transform": "flip_vertical"},
                    },
                },
            )
            path = Path(root) / "camera-settings.json"
            save_camera_settings(path, updated)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved, settings_document(updated))
            loaded, confirmed, error = load_camera_settings(base, path)
            self.assertTrue(confirmed)
            self.assertIsNone(error)
            self.assertEqual(loaded.cameras, updated.cameras)

            path.write_text("{broken", encoding="utf-8")
            fallback, confirmed, error = load_camera_settings(base, path)
            self.assertFalse(confirmed)
            self.assertIsNotNone(error)
            self.assertEqual(fallback.cameras, base.cameras)


class ClinicalStorageTests(unittest.TestCase):
    def test_session_is_atomic_and_export_contains_analysis_files(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = SessionStore(root)
            metadata = SessionMetadata.from_mapping(
                {"subject_code": "P1", "visit_id": "V1", "operator": "Dr", "consent_confirmed": True}
            )
            record = store.create(metadata, {"simulate": True})
            self.assertTrue((record.path / "session.json").exists())
            store.finalize(
                record,
                state="completed",
                camera_files={"right_eye": "right_eye.avi"},
                camera_stats={
                    "right_eye": {
                        "name": "right_eye",
                        "required": True,
                        "recorded_frames": 4,
                        "recording_codec": "MJPG q95",
                        "recording_duration_s": 1.25,
                        "quality_ok": True,
                    }
                },
            )
            self.assertTrue((record.path / "analysis_ready.csv").exists())
            analysis_csv = (record.path / "analysis_ready.csv").read_text(encoding="utf-8")
            self.assertIn("recording_codec", analysis_csv)
            self.assertIn("recording_duration_s", analysis_csv)
            self.assertIn("MJPG q95", analysis_csv)
            self.assertIn("1.25", analysis_csv)
            exported = store.export_zip(record.session_id)
            with zipfile.ZipFile(exported) as archive:
                names = set(archive.namelist())
            self.assertIn("session.json", names)
            self.assertIn("quality.json", names)
            self.assertIn("analysis_ready.csv", names)

    def test_invalid_session_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = SessionStore(root)
            with self.assertRaises(ValueError):
                store.path_for("../escape")


class ClinicalExportTests(unittest.TestCase):
    def test_export_preserves_raw_video_and_adds_review_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = SessionStore(root)
            metadata = SessionMetadata.from_mapping(
                {"subject_code": "P6", "visit_id": "V1", "operator": "Dr", "consent_confirmed": True}
            )
            config = ClinicalConfig.defaults(data_dir=root, simulate=True)
            record = store.create(metadata, config.public_dict())
            video_path = record.path / "right_eye.avi"
            writer = cv2.VideoWriter(
                str(video_path),
                cv2.VideoWriter_fourcc(*"MJPG"),
                30.0,
                (64, 48),
                True,
            )
            self.assertTrue(writer.isOpened())
            try:
                for frame_index in range(5):
                    frame = np.full((48, 64, 3), 30 + frame_index * 10, dtype=np.uint8)
                    writer.write(frame)
            finally:
                writer.release()
            (record.path / "right_eye_frames.csv").write_text(
                "frame_index,capture_sequence,timestamp_ns,wall_time\n"
                + "".join(
                    f"{index},{index + 1},{1_000_000_000 + index * 33_000_000},2026-01-01T00:00:00Z\n"
                    for index in range(5)
                ),
                encoding="utf-8",
            )
            (record.path / "right_eye_pupil.csv").write_text(
                "sequence,capture_timestamp_ns,valid,confidence,center_x,center_y,axis_width,axis_height,angle,latency_ms,quality_ok,rejected_reason,temporal_distance\n"
                "1,1000000000,1,0.95,32,24,18,12,5,2.5,1,,0.0\n"
                "2,1033000000,0,0.87,32,8,40,8,0,2.6,0,ellipse_too_elongated,0.42\n",
                encoding="utf-8",
            )
            store.finalize(
                record,
                state="completed",
                camera_files={"right_eye": video_path.name},
                camera_stats={
                    "right_eye": {
                        "name": "right_eye",
                        "required": True,
                        "recorded_frames": 5,
                        "recording_codec": "MJPG q95",
                        "recording_duration_s": 1.25,
                        "frame_transform": "rotate_180",
                        "transform_applied": True,
                        "transform_verified": True,
                        "reconnect_count": 1,
                        "reconnect_attempts_total": 2,
                        "quality_ok": True,
                    }
                },
            )

            raw_hash = hashlib.sha256(video_path.read_bytes()).hexdigest()
            archive_path = export_session(store, record.session_id)

            self.assertEqual(hashlib.sha256(video_path.read_bytes()).hexdigest(), raw_hash)
            self.assertTrue((record.path / "right_eye_annotated.avi").exists())
            self.assertTrue((record.path / "right_eye_pupil_preview.jpg").exists())
            self.assertIsNotNone(cv2.imread(str(record.path / "right_eye_pupil_preview.jpg")))
            pupil_report = json.loads((record.path / "pupil_report.json").read_text(encoding="utf-8"))
            self.assertEqual(pupil_report["session_id"], record.session_id)
            self.assertEqual(pupil_report["eyes"]["right_eye"]["valid_samples"], 1)
            quality_filter = pupil_report["eyes"]["right_eye"]["quality_filter"]
            self.assertEqual(quality_filter["quality_ok_samples"], 1)
            self.assertEqual(quality_filter["quality_rejected_samples"], 1)
            self.assertEqual(quality_filter["rejected_reason_counts"], {"ellipse_too_elongated": 1})
            self.assertEqual(quality_filter["temporal_distance"]["p95"], 0.42)
            self.assertTrue(pupil_report["eyes"]["right_eye"]["camera_runtime"]["transform_verified"])
            self.assertEqual(pupil_report["eyes"]["right_eye"]["camera_runtime"]["reconnect_count"], 1)
            self.assertEqual(pupil_report["eyes"]["right_eye"]["camera_runtime"]["recorded_frames"], 5)
            self.assertEqual(pupil_report["eyes"]["right_eye"]["camera_runtime"]["recording_codec"], "MJPG q95")
            self.assertEqual(pupil_report["eyes"]["right_eye"]["camera_runtime"]["recording_duration_s"], 1.25)
            clinical_report = json.loads((record.path / "clinical_report.json").read_text(encoding="utf-8"))
            self.assertEqual(clinical_report["session_id"], record.session_id)
            self.assertEqual(clinical_report["quality"]["cameras"]["right_eye"]["recorded_frames"], 5)
            self.assertEqual(clinical_report["quality"]["cameras"]["right_eye"]["recording_duration_s"], 1.25)
            self.assertEqual(clinical_report["quality"]["cameras"]["right_eye"]["reconnect_attempts_total"], 2)
            manifest = json.loads((record.path / "export_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["manifest_self_excluded"])
            inventory = {item["path"]: item for item in manifest["files"]}
            self.assertEqual(inventory["right_eye.avi"]["kind"], "raw_video")
            self.assertEqual(inventory["right_eye.avi"]["sha256"], raw_hash)
            self.assertEqual(inventory["right_eye_annotated.avi"]["kind"], "annotated_video")
            self.assertEqual(inventory["right_eye_pupil_preview.jpg"]["kind"], "annotated_image")
            self.assertEqual(inventory["pupil_report.json"]["kind"], "pupil_report")
            self.assertEqual(inventory["clinical_report.json"]["kind"], "clinical_report")
            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())
            self.assertIn("right_eye.avi", names)
            self.assertIn("right_eye_annotated.avi", names)
            self.assertIn("right_eye_pupil_preview.jpg", names)
            self.assertIn("pupil_report.json", names)
            self.assertIn("clinical_report.json", names)
            self.assertIn("export_manifest.json", names)


class ClinicalManagerSimulationTests(unittest.TestCase):
    def test_recording_ack_timeout_cleans_pending_session_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = ClinicalCaptureManager(ClinicalConfig.defaults(data_dir=root, simulate=True))
            metadata = SessionMetadata.from_mapping(
                {"subject_code": "P8", "visit_id": "V1", "operator": "Dr", "consent_confirmed": True}
            )
            manager._state = SessionState.READY
            manager.preflight = lambda: {"ready": True, "blocking_reasons": []}
            for item in manager._cameras.values():
                item.update({"state": "ready", "capture_fps": 240.0})
            manager._wait_for_recording_ack = lambda _expected, timeout: False
            try:
                with self.assertRaisesRegex(RuntimeError, "录制启动超时"):
                    manager.start_session(metadata)
                self.assertIsNone(manager._session)
                self.assertEqual(manager.state, SessionState.ERROR)
                self.assertIsNotNone(manager._record_event)
                self.assertFalse(manager._record_event.is_set())
            finally:
                manager.close()

    def test_recording_worker_error_is_preserved_after_forced_stop(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = ClinicalCaptureManager(ClinicalConfig.defaults(data_dir=root, simulate=True))
            try:
                manager._status_queue = queue.Queue()
                manager._status_queue.put(
                    {
                        "kind": "error",
                        "camera": "right_eye",
                        "message": "video writer failed",
                    }
                )
                manager._status_queue.put(
                    {
                        "kind": "recording_stopped",
                        "camera": "right_eye",
                        "path": "right_eye.avi",
                        "recorded_frames": 12,
                        "forced": True,
                    }
                )
                manager._status_queue.put({"kind": "stopped", "camera": "right_eye"})
                manager._drain_status()
                item = manager._cameras["right_eye"]
                self.assertEqual(item["state"], "error")
                self.assertEqual(item["error"], "video writer failed")
                self.assertFalse(item["recording"])
            finally:
                manager._status_queue = None
                manager.close()

    def test_recording_stop_timeout_marks_session_error_and_blocks_restart(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = ClinicalCaptureManager(ClinicalConfig.defaults(data_dir=root, simulate=True))
            metadata = SessionMetadata.from_mapping(
                {"subject_code": "P9", "visit_id": "V1", "operator": "Dr", "consent_confirmed": True}
            )
            try:
                record = manager.store.create(metadata, manager.config.public_dict())
                record.state = "recording"
                record.started_at = "2026-08-29T00:00:00+00:00"
                manager.store.save_record(record)
                manager._session = record
                manager._state = SessionState.RECORDING
                for item in manager._cameras.values():
                    item.update({"state": "recording", "recording": True, "capture_fps": 240.0})
                manager._wait_for_recording_stop = lambda _expected, timeout: False
                stopped = manager.stop_session()
                self.assertIsNotNone(stopped)
                assert stopped is not None
                self.assertEqual(stopped.state, "error")
                self.assertEqual(manager.state, SessionState.ERROR)
                self.assertIsNone(manager._session)
                self.assertTrue(
                    any("上一会话仍在录制" in reason for reason in manager.preflight()["blocking_reasons"])
                )
                with self.assertRaisesRegex(RuntimeError, "尚未就绪"):
                    manager.start_session(metadata)
            finally:
                manager.close()

    def test_recording_ack_success_does_not_resurrect_concurrently_stopped_session(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = ClinicalCaptureManager(ClinicalConfig.defaults(data_dir=root, simulate=True))
            metadata = SessionMetadata.from_mapping(
                {"subject_code": "P10", "visit_id": "V1", "operator": "Dr", "consent_confirmed": True}
            )
            manager._state = SessionState.READY
            manager.preflight = lambda: {"ready": True, "blocking_reasons": []}
            for item in manager._cameras.values():
                item.update({"state": "ready", "capture_fps": 240.0})

            def stop_then_ack(_expected, **_kwargs):
                manager.stop_session(failure_reason="test concurrent stop")
                return True

            manager._wait_for_recording_ack = stop_then_ack
            try:
                with self.assertRaisesRegex(RuntimeError, "启动确认前已停止"):
                    manager.start_session(metadata)
                self.assertIsNone(manager._session)
                self.assertNotEqual(manager.state, SessionState.RECORDING)
            finally:
                manager.close()

    def test_restart_finalizes_pending_recording_handshake(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = ClinicalCaptureManager(ClinicalConfig.defaults(data_dir=root, simulate=True))
            metadata = SessionMetadata.from_mapping(
                {"subject_code": "P11", "visit_id": "V1", "operator": "Dr", "consent_confirmed": True}
            )
            try:
                record = manager.store.create(metadata, manager.config.public_dict())
                record.state = "recording"
                manager.store.save_record(record)
                manager._session = record
                manager._state = SessionState.STARTING
                assert manager._record_event is not None
                manager._record_event.set()
                with patch.object(manager, "start", return_value={"app_state": "ready"}):
                    result = manager._restart_once()
                self.assertEqual(result["app_state"], "ready")
                self.assertIsNone(manager._session)
                self.assertIsNotNone(manager._record_event)
                self.assertFalse(manager._record_event.is_set())
            finally:
                manager.close()

    def test_concurrent_stop_finalizes_session_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = ClinicalCaptureManager(ClinicalConfig.defaults(data_dir=root, simulate=True))
            metadata = SessionMetadata.from_mapping(
                {"subject_code": "P12", "visit_id": "V1", "operator": "Dr", "consent_confirmed": True}
            )
            entered_wait = threading.Event()
            release_wait = threading.Event()
            results = []

            def wait_for_stop(_expected, **_kwargs):
                entered_wait.set()
                release_wait.wait(timeout=2.0)
                return True

            def stop() -> None:
                results.append(manager.stop_session())

            try:
                record = manager.store.create(metadata, manager.config.public_dict())
                record.state = "recording"
                manager.store.save_record(record)
                manager._session = record
                manager._state = SessionState.RECORDING
                for item in manager._cameras.values():
                    item.update({"state": "recording", "recording": True})
                manager._wait_for_recording_stop = wait_for_stop
                with patch.object(manager.store, "finalize", wraps=manager.store.finalize) as finalize:
                    first = threading.Thread(target=stop)
                    second = threading.Thread(target=stop)
                    first.start()
                    self.assertTrue(entered_wait.wait(timeout=1.0))
                    second.start()
                    release_wait.set()
                    first.join(timeout=3.0)
                    second.join(timeout=3.0)
                    self.assertFalse(first.is_alive())
                    self.assertFalse(second.is_alive())
                    self.assertEqual(finalize.call_count, 1)
                self.assertEqual(sum(result is None for result in results), 1)
                self.assertEqual(sum(isinstance(result, type(record)) for result in results), 1)
                self.assertIsNone(manager._session)
            finally:
                release_wait.set()
                manager.close()

    def test_shutdown_ignores_process_that_failed_before_start(self) -> None:
        class UnstartedProcess:
            pid = None

            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

            def join(self, **_kwargs):  # pragma: no cover - must never be called
                raise AssertionError("join called on an unstarted process")

            def is_alive(self):  # pragma: no cover - must never be called
                raise AssertionError("is_alive called on an unstarted process")

        with tempfile.TemporaryDirectory() as root:
            manager = ClinicalCaptureManager(ClinicalConfig.defaults(data_dir=root, simulate=True))
            process = UnstartedProcess()
            manager._processes = {"right_eye": process}
            try:
                with patch("apps.clinical_capture.manager.time.sleep"):
                    manager._shutdown_workers()
                self.assertTrue(process.closed)
            finally:
                manager.close()

    def test_stop_waits_for_recording_stopped_after_late_stats(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = ClinicalCaptureManager(ClinicalConfig.defaults(data_dir=root, simulate=True))
            try:
                manager._status_queue = queue.Queue()
                manager._state = SessionState.STOPPING
                manager._stopping_record_generation = 7
                manager._recording_stop_pending = {"right_eye"}
                manager._recording_stop_baseline = {"right_eye": 10}
                manager._cameras["right_eye"].update(
                    {"state": "recording", "recording": True, "sequence": 10}
                )
                # A stale stats packet must not be treated as writer closure.
                manager._status_queue.put(
                    {
                        "kind": "stats",
                        "camera": "right_eye",
                        "sequence": 11,
                        "timestamp_ns": 11,
                        "frames_in_interval": 1,
                        "capture_fps": 240.0,
                        "recording": True,
                        "recording_generation": 7,
                    }
                )
                self.assertFalse(
                    manager._wait_for_recording_stop({"right_eye"}, timeout=0.05)
                )
                self.assertEqual(manager._recording_stop_pending, {"right_eye"})

                manager._status_queue.put(
                    {
                        "kind": "recording_stopped",
                        "camera": "right_eye",
                        "path": "right_eye.avi",
                        "recorded_frames": 11,
                        "recording_generation": 7,
                    }
                )
                self.assertTrue(
                    manager._wait_for_recording_stop({"right_eye"}, timeout=0.5)
                )
                self.assertFalse(manager._recording_stop_pending)
            finally:
                manager._status_queue = None
                manager.close()

    def test_late_stopped_stats_preserve_recording_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = ClinicalCaptureManager(ClinicalConfig.defaults(data_dir=root, simulate=True))
            try:
                manager._status_queue = queue.Queue()
                manager._state = SessionState.STOPPING
                manager._stopping_record_generation = 8
                manager._recording_stop_pending = {"right_eye"}
                manager._cameras["right_eye"].update(
                    {
                        "state": "recording",
                        "recording": True,
                        "recording_path": "right_eye.avi",
                        "recording_codec": None,
                        "recording_duration_s": 0.0,
                        "sequence": 20,
                    }
                )
                manager._status_queue.put(
                    {
                        "kind": "recording_stopped",
                        "camera": "right_eye",
                        "path": "right_eye.avi",
                        "recording_codec": "MJPG q95",
                        "recorded_frames": 20,
                        "recording_duration_s": 1.25,
                        "recording_generation": 8,
                    }
                )
                # The worker emits this after clearing its local path/codec;
                # it must not erase the stop ACK's durable evidence.
                manager._status_queue.put(
                    {
                        "kind": "stats",
                        "camera": "right_eye",
                        "sequence": 21,
                        "timestamp_ns": 21,
                        "frames_in_interval": 1,
                        "capture_fps": 240.0,
                        "recorded_frames": 20,
                        "recording": False,
                        "recording_path": None,
                        "recording_codec": None,
                        "recording_duration_s": 0.0,
                        "recording_generation": 8,
                    }
                )
                manager._drain_status()
                item = manager._cameras["right_eye"]
                self.assertEqual(item["recording_path"], "right_eye.avi")
                self.assertEqual(item["recording_codec"], "MJPG q95")
                self.assertEqual(item["recording_duration_s"], 1.25)
            finally:
                manager._status_queue = None
                manager.close()

    def test_stop_does_not_readd_camera_after_queued_stop_ack(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = ClinicalCaptureManager(ClinicalConfig.defaults(data_dir=root, simulate=True))
            metadata = SessionMetadata.from_mapping(
                {"subject_code": "P13", "visit_id": "V1", "operator": "Dr", "consent_confirmed": True}
            )
            seen: dict[str, set[str]] = {}
            try:
                record = manager.store.create(metadata, manager.config.public_dict())
                record.state = "recording"
                manager.store.save_record(record)
                manager._session = record
                manager._state = SessionState.RECORDING
                manager._active_record_generation = 7
                manager._recording_expected = {"right_eye"}
                manager._cameras["right_eye"].update(
                    {"state": "recording", "recording": True, "sequence": 4}
                )
                manager._status_queue = queue.Queue()
                manager._status_queue.put(
                    {
                        "kind": "recording_stopped",
                        "camera": "right_eye",
                        "path": "right_eye.avi",
                        "recorded_frames": 4,
                        "recording_generation": 7,
                    }
                )

                def capture_expected(expected, **_kwargs):
                    seen["expected"] = set(expected)
                    return True

                manager._wait_for_recording_stop = capture_expected
                manager.stop_session()
                self.assertEqual(seen.get("expected"), set())
            finally:
                manager._status_queue = None
                manager.close()

    def test_real_session_without_post_reconnect_transform_verification_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = ClinicalCaptureManager(ClinicalConfig.defaults(data_dir=root, simulate=False))
            metadata = SessionMetadata.from_mapping(
                {"subject_code": "P14", "visit_id": "V1", "operator": "Dr", "consent_confirmed": True}
            )
            try:
                record = manager.store.create(metadata, manager.config.public_dict())
                record.state = "recording"
                manager.store.save_record(record)
                manager._session = record
                manager._state = SessionState.RECORDING
                for item in manager._cameras.values():
                    item.update(
                        {
                            "state": "ready",
                            "recording": False,
                            "capture_fps": 240.0,
                            "recorded_frames": 3,
                            "black_percent": 0.0,
                            "transform_verified": False,
                        }
                    )
                manager._wait_for_recording_stop = lambda _expected, **_kwargs: True
                stopped = manager.stop_session()
                self.assertIsNotNone(stopped)
                assert stopped is not None
                self.assertEqual(stopped.state, "error")
                self.assertTrue(
                    all(not value["quality_ok"] for value in stopped.camera_stats.values())
                )
                self.assertTrue(any("质量检查未通过" in reason for reason in [stopped.error or ""]))
            finally:
                manager.close()

    def test_backend_rejects_start_when_pupil_alignment_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = ClinicalCaptureManager(ClinicalConfig.defaults(data_dir=root, simulate=False))
            metadata = SessionMetadata.from_mapping(
                {"subject_code": "P7", "visit_id": "V1", "operator": "Dr", "consent_confirmed": True}
            )
            manager._state = SessionState.READY
            manager.preflight = lambda: {
                "ready": False,
                "blocking_reasons": ["right_eye: 请将右眼整只瞳孔移入画面"],
            }
            try:
                with self.assertRaisesRegex(RuntimeError, "整只瞳孔"):
                    manager.start_session(metadata)
            finally:
                manager.close()

    def test_unconfirmed_real_device_mapping_blocks_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = ClinicalCaptureManager(
                ClinicalConfig.defaults(data_dir=root, simulate=False),
                device_settings_confirmed=False,
            )
            try:
                check = manager.preflight()
                self.assertFalse(check["ready"])
                self.assertIn("请先确认左右眼相机和画面方向", check["blocking_reasons"])
            finally:
                manager.close()

    def test_backend_preflight_blocks_zero_fps_and_unverified_transform(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            manager = ClinicalCaptureManager(
                ClinicalConfig.defaults(data_dir=root, simulate=False),
                device_settings_confirmed=True,
            )
            try:
                manager._state = SessionState.READY
                now = time.monotonic()
                for item in manager._cameras.values():
                    item.update(
                        {
                            "state": "ready",
                            "capture_fps": 0.0,
                            "last_frame_monotonic": now,
                            "transform_verified": False,
                        }
                    )
                check = manager.preflight()
                self.assertFalse(check["ready"])
                self.assertTrue(any("帧率 0.0" in reason for reason in check["blocking_reasons"]))
                self.assertTrue(any("画面方向尚未通过首帧验证" in reason for reason in check["blocking_reasons"]))
            finally:
                manager.close()

    def test_simulated_session_starts_stops_and_can_start_again(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = ClinicalConfig.defaults(data_dir=root, simulate=True)
            manager = ClinicalCaptureManager(config)
            try:
                started = manager.start()
                self.assertEqual(started["app_state"], "ready")
                self.assertTrue(started["preflight"]["ready"])
                self.assertTrue(
                    all(
                        camera["capture_fps"] > 0
                        for camera in started["preflight"]["cameras"].values()
                        if camera["required"]
                    )
                )
                metadata = SessionMetadata.from_mapping(
                    {"subject_code": "P2", "visit_id": "V1", "operator": "Dr", "consent_confirmed": True}
                )
                record = manager.start_session(metadata)
                self.assertEqual(record.state, "recording")
                stopped = manager.stop_session()
                self.assertIsNotNone(stopped)
                assert stopped is not None
                self.assertEqual(stopped.state, "completed")
                self.assertTrue((stopped.path / "right_eye.avi").exists())
                self.assertTrue((stopped.path / "right_eye_pupil.csv").exists())
                self.assertTrue((stopped.path / "left_eye_pupil.csv").exists())
                self.assertIn("confidence", (stopped.path / "right_eye_pupil.csv").read_text(encoding="utf-8-sig"))
                self.assertEqual(manager.status()["app_state"], "ready")
                second = manager.start_session(
                    SessionMetadata.from_mapping(
                        {"subject_code": "P3", "visit_id": "V1", "operator": "Dr", "consent_confirmed": True}
                    )
                )
                self.assertEqual(second.state, "recording")
                manager.stop_session()
            finally:
                manager.close()


class ClinicalDeviceSettingsApiTests(unittest.TestCase):
    def test_settings_persist_and_cannot_change_during_recording(self) -> None:
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            data_dir = root_path / "data"
            config = ClinicalConfig.defaults(data_dir=data_dir, simulate=True)
            config_path = root_path / "clinical.json"
            config_path.write_text(json.dumps(config.public_dict()), encoding="utf-8")
            payload = {
                "version": 1,
                "confirmed": True,
                "cameras": {
                    "right_eye": {"index": 8, "frame_transform": "flip_horizontal"},
                    "left_eye": {"index": 9, "frame_transform": "rotate_180"},
                },
            }

            app = create_app(config_path, data_dir=data_dir, simulate=True)
            with TestClient(app) as client:
                response = client.put("/api/device-settings", json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue((root_path / "camera-settings.json").exists())
                configured = client.get("/api/config").json()["config"]["cameras"]
                self.assertEqual({item["name"]: item["index"] for item in configured}, {"right_eye": 8, "left_eye": 9})

                started = client.post(
                    "/api/session/start",
                    json={"subject_code": "P5", "visit_id": "V1", "operator": "Dr", "consent_confirmed": True},
                )
                self.assertEqual(started.status_code, 200, started.text)
                blocked = client.put("/api/device-settings", json=payload)
                self.assertEqual(blocked.status_code, 409, blocked.text)
                self.assertEqual(client.get("/api/status").json()["app_state"], "recording")
                self.assertEqual(client.post("/api/session/stop", json={}).status_code, 200)

            restarted = create_app(config_path, data_dir=data_dir, simulate=True, autostart=False)
            with TestClient(restarted) as client:
                settings = client.get("/api/device-settings").json()
                self.assertTrue(settings["confirmed"])
                self.assertEqual(settings["cameras"]["right_eye"]["index"], 8)
                self.assertEqual(settings["cameras"]["left_eye"]["frame_transform"], "rotate_180")

    def test_simulated_runtime_reconfigures_without_changing_eye_ids_or_session_config(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = ClinicalConfig.defaults(data_dir=root, simulate=True)
            manager = ClinicalCaptureManager(config)
            try:
                manager.start()
                candidate = apply_camera_settings(
                    config,
                    {
                        "version": 1,
                        "confirmed": True,
                        "cameras": {
                            "right_eye": {"index": 6, "frame_transform": "none"},
                            "left_eye": {"index": 7, "frame_transform": "rotate_180"},
                        },
                    },
                )
                restarted = manager.reconfigure(candidate, device_settings_confirmed=True)
                self.assertTrue(restarted["preflight"]["ready"])
                self.assertEqual(restarted["preflight"]["cameras"]["right_eye"]["index"], 6)
                self.assertEqual(restarted["preflight"]["cameras"]["left_eye"]["index"], 7)
                record = manager.start_session(
                    SessionMetadata.from_mapping(
                        {"subject_code": "P4", "visit_id": "V1", "operator": "Dr", "consent_confirmed": True}
                    )
                )
                captured_config = json.loads((record.path / "config.json").read_text(encoding="utf-8"))
                captured = {camera["name"]: camera for camera in captured_config["cameras"]}
                self.assertEqual(captured["right_eye"]["index"], 6)
                self.assertEqual(captured["left_eye"]["frame_transform"], "rotate_180")
                manager.stop_session()
            finally:
                manager.close()


if __name__ == "__main__":
    unittest.main()
