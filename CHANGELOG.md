# Changelog

## 0.8.0 - Detector2D candidate quality gate

- Added a conservative acceptance/audit layer around the existing `Detector2D` output: clipped, implausibly elongated, upper-lid/eyebrow-like, size-jump and position-jump candidates are retained for audit but marked `quality_ok=0` and `valid=0`.
- Debounced large candidate jumps with a short stability track and reset alignment state after each subject so a previous session cannot unlock the next recording.
- Suppressed directional arrows for rejected candidates; the operator is told to re-find/confirm the pupil instead of being guided toward an eyelid or eyebrow.
- Added per-eye rejection counts and trajectory-distance summaries to `pupil_report.json`, and documented the new CSV/report fields for handoff and analysis.
- Added a configurable minimum candidate-size check (`pupil_too_small`) to suppress tiny high-confidence eyelash/specular-reflection false positives observed during camera adjustment.
- Clarified Qt startup failures and missing camera status, and made the recording gate visibly fail closed.
- Hardened session lifecycle acknowledgements: a start/stop timeout or worker crash finalizes the session as `error`, preserves the worker root cause, clears stale recording events, and blocks a new subject until the capture workers are restarted.

## 0.7.0 - PySide6 version 2 workstation

- Added a native PySide6 operator window with simultaneous right/left eye previews.
- Added a centered eye-orbit framing guide, structured adjustment arrows, confidence/hold progress, and recording controls backed by the existing manager.
- Colored the framing guide from red to yellow to green using the current detector confidence; stale/disconnected frames are shown in gray.
- Kept the guide visual-only: Detector2D geometry, confidence threshold, freshness, and continuous hold remain the recording gate.
- Added standalone V2 dependency, PyInstaller, portable-package, and Inno Setup entry points.
- Hardened the PyInstaller spec against incompatible third-party ICU DLLs that can prevent QtCore from loading on Windows.

## 0.6.0 - 2026-08-28

- 相机 worker 对连续空帧执行有界重连，短暂 USB/DirectShow 抖动不再立即终止当前会话；重连耗尽仍安全结束并保留已写文件。
- 帧流看门狗改为只依据真实 `stats` 帧更新时间，避免重连状态消息掩盖实际超时。
- 实时状态、`quality.json`、`pupil_report.json` 和临床报告记录方向是否已应用、方向是否已验证、重连次数及尝试次数。
- 页面显示“正在重连”和“方向已应用”，并在相机卡片和报告中提供可审计的运行证据。

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
