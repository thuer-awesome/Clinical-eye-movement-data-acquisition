# Changelog

## 0.5.0 - 2026-08-28

- 增加真实相机录制前的双眼瞳孔对准门禁：按瞳孔中心偏移给出上下左右微调方向，置信度和中心位置连续达标后才允许开始录制。
- 前端实时显示每只眼的对准提示和保持进度，后端 `/api/session/start` 同步强制校验，避免通过 API 绕过门禁。
- 导出增加 `*_pupil_preview.jpg` 和 `pupil_report.json`，与原始 AVI、`*_pupil.csv`、标注复核 AVI 一起进入 ZIP，并纳入 manifest。
- 导出增加面向医生的 `clinical_report.json`，汇总脱敏会话、双眼质量、瞳孔统计、筛查备注和复核文件索引。
- 修正 CI 源码编译路径，避免不存在目录产生误导性警告。

## 0.4.0 - 2026-08-27

- 将临床采集功能提炼为独立、可交接的仓库结构。
- 导出时保留原始 AVI，并生成时间戳对齐的 `_annotated.avi` 复核视频。
- 生成带文件用途、大小和 SHA-256 的 `export_manifest.json`。
- 相机重启增加 DirectShow 句柄释放等待和有限恢复重试。
- 固定 `pupil-detectors==2.0.2`，补充 Windows 安装、算法、相机、数据格式和现场 SOP 文档。

## 0.3.1

- 便携版 Clinical Capture 工作站的初始临床采集实现。
