# 算法与时间语义

## 实际使用的算法

临床采集使用 PyPI `pupil-detectors==2.0.2` 的 `pupil_detectors.Detector2D`。每路检测器接收一张 8-bit 灰度图，返回一个映射对象，核心字段为：

- `ellipse.center = (x, y)`：像素坐标；
- `ellipse.axes = (width, height)`：完整轴长，绘制时除以 2 得到 OpenCV 半轴；
- `ellipse.angle`：椭圆角度；
- `diameter`：瞳孔直径（像素）；
- `confidence`：`0–1` 置信度。

`PupilDetectorService` 不在 240 fps 相机循环中调用算法，而是以配置的 `pupil_detector_fps`（默认 60 Hz）读取共享内存里的最新帧。这样做的结果是：检测可以丢帧，原始 AVI 和逐帧时间戳不能丢帧。

## 有效性与输出

当几何字段完整、中心位于图像内且 `confidence >= pupil_confidence_threshold`（默认 `0.6`）时，样本写入 `valid=1`；否则保留该检测行但写入 `valid=0` 和错误/空几何字段。CSV 还记录检测完成时间和延迟，便于剔除异常样本。

## 录制前瞳孔对准门禁

真实相机模式不会仅因为“相机有画面”就允许录制。每个必需眼位都要同时满足：

1. `Detector2D` 返回完整椭圆几何，瞳孔中心在图像内；
2. `confidence >= pupil_confidence_threshold`（默认 `0.60`）；
3. 瞳孔中心位于画面中心容差内（默认距中心各方向不超过画面宽/高的 `30%`）；
4. 条件连续保持 `pupil_alignment_hold_seconds`（默认 `1.5` 秒），且检测帧没有过期（默认 `0.75` 秒）。

不满足时，服务端在 `/api/status` 的 `preflight.pupil_alignment.guidance` 和每只眼的 `pupil.alignment.guidance` 返回操作提示。提示会根据瞳孔中心相对画面的偏移给出“向左/向右/向上/向下微调相机”；尚未找到完整瞳孔时提示把整只瞳孔移入画面；置信度不足时提示微调焦距或曝光。达标期间显示倒计时/进度，连续保持完成后才将 `ready=true`，后端也会拒绝绕过 UI 的 `/api/session/start` 请求。

V2 同时返回结构化 `direction`（`left`、`right`、`up`、`down` 及对角组合），Qt 页面据此画箭头。`preview_guide_margin_x/y` 只定义预览中央的“眼眶正对范围”（默认约 68%×60%），帮助医生做相机正对和工作距离粗调；框线按真实 `confidence` 从红色过渡到橙黄色，再在达到配置阈值时显示绿色。它没有进入 `_detect_snapshot()` 的 `valid`、`center_ok` 或 `alignment_eligible` 计算。不能因为眼睛进入建议框或框线变绿就降低阈值或宣称样本有效。

模拟模式明确跳过真实对准门禁，用于无相机的流程和导出测试；模拟结果不能证明真实镜头、照明或瞳孔暴露质量。

预览中的椭圆只是最近一次有效结果；短暂失效时最多保持 100 ms 的最后有效标注，并标记 `PUPIL*`。这不会修改原始 AVI。导出标注视频会重新读取原始帧和 CSV，按 `capture_sequence` 优先、`timestamp_ns` 兜底匹配最近检测；检测结果超过 250 ms 不再绘制为有效结果。

相机方向在 `normalize_frame()` 中通过 OpenCV 像素变换执行，输出同时供共享内存、Detector2D 和原始 AVI 使用。首次输出帧会发出 `transform_verified` 状态，因此报告能够区分“已配置旋转”和“本次运行确实经过旋转代码路径”。方向变换不参与 USB 协商；空帧恢复由 worker 的有界重连策略处理，重连造成的时间间隔会自然保留在帧时间戳中。

## 时间戳和双眼对齐

相机 worker 在 `read()` 前后各取一次单调时钟，使用中点作为帧时间戳：

```text
capture_timestamp_ns = (read_started_ns + read_finished_ns) / 2
```

`*_frames.csv` 的 `capture_sequence` 与 `timestamp_ns` 是录制帧的事实索引；`*_pupil.csv` 引用检测所消费的共享帧序号和时间戳。该方案适合软件近似对齐和离线质量分析，但不等同于硬件触发同步。需要亚毫秒级左右眼同步时，必须增加共享触发、设备时间戳或专用同步硬件，并在现场重新验收。

## 不包含的算法

当前临床版不使用 3D `pye3d`、注视点映射、校准模型、病理分类、情绪识别或诊断规则。页面中的“病理相关/情绪相关”字段只是操作员填写的筛查标签、分数和备注，不是模型输出，也不能替代医生判断。

## 分析建议

1. 先读取 `quality.json`，剔除整体或必需相机 `quality_ok=false` 的会话。
2. 根据 `*_frames.csv` 和 `*_pupil.csv` 的序号/时间戳做对齐，不要按 CSV 行号猜测 240 fps 连续性。
3. 将 `valid=0`、低置信度、检测延迟异常或帧流超时样本标记为缺失；不要用前一帧无条件填充。
4. 将原始 AVI 作为证据源，`*_annotated.avi` 只用于人工复核和质控。
5. 报告中的 `confidence_min/mean/max` 同时包含低置信度检测；统计分析应以 `valid` 和预先批准的阈值筛选。保留低置信度行是为了审计，并不保证它们可用于定量结论。
