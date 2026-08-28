# 专业交接与调试手册

## 接手顺序

1. 先读根 README、[架构](architecture.md)、[算法](algorithm.md) 和 [相机调试](camera-tuning.md)。
2. 用 Python 3.11 建立 `.venv`，运行 `python -m unittest discover -s tests -p "test*.py" -v`。
3. 用 `--simulate --no-browser` 启动，走一遍设备页面、开始/停止、筛查备注和导出；确认 manifest 哈希与 ZIP 内容。
4. 再接真实相机，先运行 `scripts/camera_probe.py` 单路/双路探测，随后执行页面扫描和预检。
5. 只在软件链和设备质量都记录证据后，才安排真人/临床现场验收。

## 模块责任与常见改动

- 新增配置字段：改 `models.py` 的 `from_mapping/validate/public_dict`、示例 JSON、README/数据文档和测试。
- 改相机采集：改 `worker.py`，保留 `capture_timestamp_ns`、`frames.csv`、状态队列和 `recording_codec` 契约；不要把算法塞回高帧率循环。
- 处理掉线：优先调 `camera_read_failure_threshold`、`camera_reconnect_attempts` 和 `camera_reconnect_delay_seconds`；不要用“状态仍在线”代替真实帧时间戳，确认 `last_frame_age_ms` 和 `reconnect_count`。
- 改检测字段：同步 `pupil.py` 的 CSV header、对准门禁状态、UI 状态、`storage.py` 的汇总和 `docs/data-format.md`。
- 改导出：只在 `exporter.py` 派生文件；原始 AVI 不覆盖；同时维护 `_annotated.avi`、`*_pupil_preview.jpg`、`pupil_report.json`；manifest 排除自身哈希并增加回归测试。
- 改路由：更新 `docs/api.md`、请求超时和错误映射，确保录制中的设备设置仍返回 `409`。
- 改打包：同步 `packaging/clinical_capture.spec`、版本号、便携说明和 CI Windows job。
- V2 Qt 页面：入口是 `qt_main.py`，窗口和预览绘制在 `qt_app.py`；所有启动/重启/录制/停止/导出操作必须继续调用 `ClinicalCaptureManager`，不要在 UI 中另开相机句柄或复制 Detector2D。
- V2 依赖/打包：同步 `requirements-clinical-capture-v2.txt`、`packaging/clinical_capture_v2.spec`、`scripts/*_v2.ps1`、`packaging/ClinicalCaptureV2-*` 和 `installer/ClinicalCaptureV2.iss`；V1 浏览器构建仍需保持可用。

## 必须保留的安全边界

- 不把真实受试者数据、摄像头画面、凭据、局域网地址或设备密钥提交到 Git。
- 不把仿真 FPS、驱动请求 FPS 或浏览器预览 FPS 写成真实硬件验收结果。
- 不把筛查字段、瞳孔几何或任何启发式统计描述成诊断算法。
- 不把 V2 眼眶正对范围框当作检测 ROI 或置信度来源；框线颜色只是实时真实置信度的操作提示，范围框和颜色都不能改变结果，门禁仍由真实几何、阈值、新鲜度和连续保持决定。
- 不在 CI 中宣称真实双眼同步、照明安全、长时间稳定性或真人注视精度已经通过。

## 现场验收记录模板

记录日期、电脑型号、Windows 版本、Python/便携版版本、相机型号/固件、物理索引、USB 控制器/接口、请求模式、驱动回读模式、持续时长、每路实际 FPS、黑帧比例、曝光/增益、左右时间偏差、导出 ZIP SHA-256 和操作者。原始记录应放在受控的院内系统，不要把含画面的截图提交到公开仓库。

## 已知缺口

- 当前软件测试主要使用模拟相机；真实硬件需现场验证。
- 真实模式的录制前对准门禁依赖 `Detector2D` 的实时置信度和中心位置；阈值、中心容差和连续保持时间需按镜头/照明现场验收后再调整。
- 时间戳为主机单调时钟中点，不是硬件触发同步。
- `pupil-detectors` 是 2D 瞳孔几何检测，不提供 3D 深度、注视映射或诊断。
- OpenCV/DirectShow 对相机控制项的支持依驱动而异；曝光和增益回读失败时需使用厂家 SDK。
- `frame_transform` 是 worker 内的确定性软件像素变换，不改变 USB 传输；`transform_verified` 只证明首帧已经过代码路径，最终方向仍需用现场预览/标注导出人工确认。
- 重连只能覆盖短暂空帧或可恢复的设备句柄故障；持续断电、USB 控制器复位或驱动崩溃仍需现场更换接口/线材并重新预检。
- 便携包在 GitHub Actions 作为构建产物发布；正式 Release 前仍需在无 Python 的干净 Windows 电脑验收。
- Qt 页面需要在真实屏幕上检查左右画面非空、方向变换确实生效、箭头方向可理解、重连状态不掩盖帧流超时，并确认 1366×768 最小窗口仍能完成开始/停止/导出。
