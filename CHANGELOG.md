# Changelog

## 0.4.0 - 2026-08-27

- 将临床采集功能提炼为独立、可交接的仓库结构。
- 导出时保留原始 AVI，并生成时间戳对齐的 `_annotated.avi` 复核视频。
- 生成带文件用途、大小和 SHA-256 的 `export_manifest.json`。
- 相机重启增加 DirectShow 句柄释放等待和有限恢复重试。
- 固定 `pupil-detectors==2.0.2`，补充 Windows 安装、算法、相机、数据格式和现场 SOP 文档。

## 0.3.1

- 便携版 Clinical Capture 工作站的初始临床采集实现。
