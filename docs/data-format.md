# 会话数据格式

每次会话目录为 `%LOCALAPPDATA%\\ClinicalCapture\\data\\<session_id>`，其中 `<session_id>` 是 UTC 时间加随机后缀，例如 `20260827T120000Z-a1b2c3d4`。路径只接受安全字符，避免受试者输入穿越目录。

## 文件清单

| 文件 | 级别 | 用途 |
| --- | --- | --- |
| `session.json` | 必需 | 脱敏元数据、开始/结束时间、最终状态、相机统计和错误 |
| `config.json` | 必需 | 本次实际使用的相机索引、方向、分辨率/FPS/FOURCC 和质量参数 |
| `quality.json` | 必需 | 每路实际 FPS、黑帧比例、写入帧数、录制时长、编码器、方向生效状态、重连次数和 `quality_ok` |
| `analysis_ready.csv` | 必需 | 每路一行的汇总，适合导入统计软件前的快速筛选 |
| `right_eye.avi` / `left_eye.avi` | 有相机时必需 | 按采集时方向保存的原始视频，不含检测叠加 |
| `*_annotated.avi` | 导出时生成 | 复核视频；不覆盖原始 AVI |
| `*_pupil_preview.jpg` | 导出时生成 | 每只眼一张带瞳孔轮廓、中心和置信度的代表性复核静帧 |
| `*_frames.csv` | 有相机时必需 | `frame_index`、`capture_sequence`、`timestamp_ns`、UTC 墙上时间 |
| `*_pupil.csv` | 检测启用时生成 | Detector2D 结果、有效性、椭圆几何、置信度和延迟 |
| `pupil_report.json` | 导出时生成 | 每只眼检测样本/有效率/置信度统计、对准门禁参数和复核文件索引；不是诊断报告 |
| `clinical_report.json` | 导出时生成 | 面向医生的脱敏会话摘要、双眼质量、瞳孔质量、筛查备注和所有复核文件索引；不是诊断报告 |
| `screening.json` | 可选 | 研究/筛查标签、分数和备注；带非诊断免责声明 |
| `README.txt` | 必需 | 会话目录内自包含说明 |
| `export_manifest.json` | 导出时生成 | 文件用途、大小、SHA-256、相机映射和标注文件可用性 |

## `*_pupil.csv` 字段

`camera`、`eye_id`、`sequence`、`capture_timestamp_ns`、`detection_timestamp_ns`、`latency_ms`、`valid`、`confidence`、`center_x`、`center_y`、`radius`、`diameter`、`axis_width`、`axis_height`、`angle`、`detector_fps`、`quality_ok`、`rejected_reason`、`temporal_distance`、`error`。

`center_x/center_y/axis_*` 单位为像素；`valid` 为 `0/1`；`confidence` 为 `0–1`；时间戳为进程单调时钟纳秒，不能直接当作 Unix epoch。

`quality_ok` 表示候选是否通过瞳孔暴露、椭圆形状、最小尺寸和时间连续性筛选；它不是置信度的替代值，也不改变 Detector2D 输出的原始 `confidence`。`valid=1` 只有在 `quality_ok=1` 且置信度达到阈值时才成立。`rejected_reason` 记录被筛选候选的原因（例如 `pupil_not_fully_exposed`、`ellipse_too_elongated`、`pupil_too_small`、`temporal_jump`）；空值表示未记录拒绝原因。`temporal_distance` 是候选中心相对上一条稳定轨迹的归一化位移，用于审计突跳，不是像素单位。

`pupil_report.json` 是面向交接和分析人员的结构化摘要。它同时列出原始 AVI、`*_pupil.csv`、`*_annotated.avi` 和 `*_pupil_preview.jpg` 的对应关系，以及本次使用的置信度阈值、中心容差、连续保持时间和候选筛选参数。每只眼的 `quality_filter` 统计接受/拒绝样本数、接受率、`rejected_reason_counts` 和 `temporal_distance` 的 min/mean/p95/max；这能区分 Detector2D 原始候选与最终可用于门禁/分析的样本。每只眼还保存 `alignment_at_start` / `alignment_at_stop` 快照，记录录制门禁是否达标。原始视频仍是证据源，静帧和标注视频用于人工复核。

V2 预览使用 `preview_guide_margin_x/y` 绘制中央眼眶正对范围框，并在实时状态中标记 `operator_guide.gating=false`。框线按当前 Detector2D 的真实 `confidence` 从低到高着色；检测帧过期或相机掉线时不会沿用旧的绿色提示。该框和颜色不会写入原始 AVI，也不会改变 `valid` 或录制门禁；`direction` 字段只是 Detector2D 检测到的瞳孔偏移提示。

`clinical_report.json` 是面向医生/临床协调员的单文件摘要，包含脱敏会话字段、`quality.json` 的整体和逐眼质量、`pupil_report.json` 的瞳孔统计及筛查备注。它不替代原始视频、逐帧 CSV 或正式临床报告，也不生成诊断结论。

`analysis_ready.csv` 每路一行，除帧率、黑帧、写入帧数、录制时长、编码器、瞳孔有效率和检测延迟外，还包含 `frame_transform`、`transform_verified`、`reconnect_count` 和 `reconnect_attempts_total`，可直接作为下游筛选条件；需要逐帧断点时仍应读取对应的 `*_frames.csv`。`recording_duration_s` 由 worker 的最后一个采集时间戳减去开始录制时间戳计算，即使会话短于一个状态统计周期，也会通过 `recording_stopped` 确认包保留。

## `quality.json` 判读

必须同时检查：

- `overall.ready` 是否为真；
- 每个 `required=true` 相机的 `quality_ok`；
- `capture_fps` 是否达到 `minimum_fps`；
- `black_percent`、`recorded_frames`、`recording_duration_s`、`recording_codec` 和错误字段；
- `frame_transform`、`transform_verified`；若 `reconnect_count>0`，结合 `reconnect_attempts_total` 判断是否发生过短暂掉线；
- 瞳孔摘要中的 `valid_rate`、`latency_p95_ms` 和 `samples`。

## Manifest 校验

导出 ZIP 中的 `export_manifest.json` 自身不纳入 `files` 哈希，避免“哈希包含自身”的循环。可用 PowerShell 校验原始视频：

```powershell
Get-FileHash .\\right_eye.avi -Algorithm SHA256
```

将结果与 manifest 的 `files[].sha256` 比较。原始 AVI 是证据源；标注 AVI 可重新生成，出现生成警告时不得删除或覆盖原始文件。
