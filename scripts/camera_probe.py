from __future__ import annotations

import argparse

import cv2


def probe_camera(index: int) -> dict[str, int | float] | None:
    capture = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    try:
        if not capture.isOpened():
            return None
        success, frame = capture.read()
        if not success or frame is None:
            return None
        height, width = frame.shape[:2]
        return {
            "index": index,
            "width": width,
            "height": height,
            "fps": capture.get(cv2.CAP_PROP_FPS),
        }
    finally:
        capture.release()


def main() -> int:
    parser = argparse.ArgumentParser(description="枚举可由 OpenCV 打开的摄像头")
    parser.add_argument("--max-index", type=int, default=9)
    args = parser.parse_args()

    cameras = [
        camera
        for index in range(args.max_index + 1)
        if (camera := probe_camera(index)) is not None
    ]
    if not cameras:
        print("没有发现可打开的摄像头。请检查 USB、驱动和 Windows 相机权限。")
        return 1

    print("发现以下摄像头：")
    for camera in cameras:
        print(
            f"  index={camera['index']} "
            f"frame={camera['width']}x{camera['height']} "
            f"reported_fps={camera['fps']:.1f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
