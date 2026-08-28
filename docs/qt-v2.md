# V2 原生 PySide6 页面

V2 复用现有 `ClinicalCaptureManager`、高帧率相机 worker、共享内存、`Detector2D` 和导出器，只替换医生端操作界面。Qt 页面不经 HTTP，也不另开一套录制或算法链路，因此浏览器版与 V2 的原始 AVI、时间戳、瞳孔 CSV、质量报告和断线恢复语义一致。

## 页面区域

- 左侧“会话信息”：填写脱敏受试者编号、访视、操作员、年龄/性别、方案、筛查标签和备注；知情同意未确认时不能开始。
- 左侧“采集控制”：重新检查、开始、停止、导出和打开数据目录。启动、重启、停止、导出均在线程池执行，Qt 主线程持续刷新画面。
- “采集控制”顶部会持续显示“可以录制”或“暂不可录制”。不可录制时，下面逐条列出当前阻塞原因（设备/映射、未保存设置、左右眼瞳孔门禁、帧流或会话信息）；填写字段、勾选知情同意、修改映射或相机状态变化后会立即刷新。状态读取失败时自动失败关闭，按钮保持禁用并显示错误原因。
- 左侧“设备映射与方向”：选择逻辑右眼/左眼对应的物理索引和 `none`、`rotate_180`、`flip_horizontal`、`flip_vertical`，保存后重启双路 worker。
- 中央左右眼预览：显示 worker 已应用方向变换后的实时画面，以及 Detector2D 椭圆、中心、置信度、方向箭头和保持进度。
- 底部实时质量：显示双路就绪数、平均 FPS、重连次数、置信度阈值、最近会话瞳孔有效率和 ZIP 路径。

## 建议框与真实门禁

两者必须区分：

| 项目 | 用途 | 是否允许开始录制 |
| --- | --- | --- |
| 眼眶正对范围框 | 帮医生把摄像头正对眼眶并保持在中央工作距离；默认约占预览宽 68%、高 60%，由 `preview_guide_margin_x/y` 控制 | 否，只是预览绘制 |
| 完整椭圆几何 | Detector2D 必须返回中心、轴长和角度，中心必须在图像内 | 是 |
| 瞳孔置信度 | `confidence >= pupil_confidence_threshold`，默认 `0.60` | 是 |
| 中心容差 | 瞳孔中心距图像中心不超过 `pupil_alignment_center_tolerance`，默认每轴 `30%` | 是 |
| 连续保持 | 上述条件持续 `1.5` 秒且检测帧未过期 | 是 |
| 方向首帧验证 | 真实模式下每路首帧已执行所选 `frame_transform` 并标记 `transform_verified=true` | 是 |
| 设置保存状态 | 映射或方向控件有修改但尚未点击“保存并重启相机” | 否 |

因此，进入大框但置信度不足时仍显示“未锁定”，开始按钮保持禁用；低于阈值的检测仍写入 `*_pupil.csv`，但 `valid=0`，供人工复核和下游缺失值处理。程序不会把低置信度样本包装为有效数据，也不能保证低置信度数据适合分析。

框线颜色直接来自当前 Detector2D 检测的真实 `confidence`：低置信度为红色，接近配置阈值时过渡为橙黄色，达到阈值时为绿色；没有检测或检测帧已过期/相机掉线时显示为灰色，避免把旧结果误看成当前高置信度。颜色只提供操作反馈，不使用建议框位置，也不使用颜色本身修改 `valid`、中心合格或连续保持状态。

## 调整顺序

1. 页面显示“寻找瞳孔”时，先移动相机和工作距离，让眼眶大致落在中央正对范围框内，并让整只瞳孔进入画面。
2. 出现左/右/上/下箭头时，小幅移动相机。文字提示是操作依据，箭头表示检测到的瞳孔在画面中的偏移方向。
3. 瞳孔已出现但置信度不足时，先调焦，再检查曝光、增益、IR 照明、睫毛遮挡和角膜反光。
4. 状态变为 `holding` 后不要继续移动；进度完成并显示“对准完成”才可开始。
5. 真实模式下双眼都必须通过；模拟模式会明确显示“跳过真实瞳孔门禁”，只能用于流程测试。

## 断线与方向

- worker 连续收到空帧达到阈值后释放旧 DirectShow 句柄，并对同一物理索引做有限次数重连。
- `reconnecting` 期间页面保留会话状态并显示尝试进度；成功后继续写同一 AVI，会话质量报告记录 `reconnect_count` 和 `reconnect_attempts_total`。
- 重连状态消息不会刷新帧流看门狗；只有真实 `stats` 帧会更新 `last_frame_age_ms`。重连耗尽或帧流持续过期时会安全结束会话并保留已有文件。
- `stats` 同时携带 `frames_in_interval`、递增序列和时间戳；零帧或重复统计不会刷新看门狗，FPS 为 `0` 会阻塞开始录制。状态轮询异常时 UI 失败关闭，不会沿用旧的“可以录制”。
- `frame_transform` 在 worker 的 `normalize_frame()` 中执行，随后同一像素流进入共享预览、Detector2D 和原始 AVI。首帧经过此路径后 `transform_verified=true`。它证明代码路径已执行，不代替医生对左右眼和上下方向的现场确认。
- 录制开始和停止都等待采集进程确认；任一确认超时、写入器异常或 worker 崩溃都会把会话保存为 `error`，页面保留错误原因并清除录制事件。为避免旧写入器串入下一位受试者，错误态下“开始录制”保持禁用，先点击“重启采集设备”并重新完成预检。

## 运行与打包

源码模拟运行：

```powershell
& .\scripts\install_clinical_capture_v2.ps1
& .\.venv-v2\Scripts\python.exe -m apps.clinical_capture.qt_main --simulate
```

构建医生端便携包：

```powershell
& .\scripts\build_clinical_capture_v2.ps1
```

输出为 `dist/ClinicalCaptureV2/ClinicalCaptureV2.exe` 和 `dist/ClinicalCaptureV2-portable.zip`。构建机有 Inno Setup 时使用 `-BuildInstaller` 生成安装程序。发布前必须在无 Python 的干净 Windows 电脑和真实双相机上验收，不能把 Qt 离屏烟测或模拟相机结果写成硬件/临床验收通过。

## 导出内容

V2 的“导出当前会话 ZIP”调用现有 `export_session()`，包含：

- `right_eye.avi` / `left_eye.avi` 原始证据视频；
- `*_annotated.avi` 和 `*_pupil_preview.jpg` 人工复核副本；
- `*_frames.csv`、`*_pupil.csv`；
- `quality.json`、`analysis_ready.csv`；
- `pupil_report.json`、`clinical_report.json`；
- `config.json`、`session.json`、`export_manifest.json`。

`clinical_report.json` 是脱敏采集质量摘要，不是诊断报告。真实会话、图片、日志和 ZIP 不得提交到公开 GitHub、issue 或公共云盘。
