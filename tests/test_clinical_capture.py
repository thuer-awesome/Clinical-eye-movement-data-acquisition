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
from apps.clinical_capture.pupil import PupilDetectorService, _alignment_direction, _alignment_guidance, guide_rect_pixels
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
from apps.clinical_capture.worker import _close_recording, _open_recording, _reconnect_capture, normalize_frame
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
                manager._status_queue.put({"kind": "reconnecting", "camera": "right_eye", "attempt": 1, "attempts": 3})
                manager._drain_status()
                self.assertEqual(manager._cameras["right_eye"]["last_frame_monotonic"], first)
            finally:
                # Restore the multiprocessing queue contract used by close().
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
                camera_stats={"right_eye": {"name": "right_eye", "required": True, "recorded_frames": 4, "quality_ok": True}},
            )
            self.assertTrue((record.path / "analysis_ready.csv").exists())
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
                "sequence,capture_timestamp_ns,valid,confidence,center_x,center_y,axis_width,axis_height,angle,latency_ms\n"
                "1,1000000000,1,0.95,32,24,18,12,5,2.5\n",
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
            self.assertTrue(pupil_report["eyes"]["right_eye"]["camera_runtime"]["transform_verified"])
            self.assertEqual(pupil_report["eyes"]["right_eye"]["camera_runtime"]["reconnect_count"], 1)
            clinical_report = json.loads((record.path / "clinical_report.json").read_text(encoding="utf-8"))
            self.assertEqual(clinical_report["session_id"], record.session_id)
            self.assertEqual(clinical_report["quality"]["cameras"]["right_eye"]["recorded_frames"], 5)
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
