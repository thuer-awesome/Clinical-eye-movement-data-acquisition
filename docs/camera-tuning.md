# 相机、帧率与清晰度调试

## 默认现场配置

`configs/clinical-capture.json` 的默认值针对当前两路近眼灰度相机：

| 项目 | 值 | 说明 |
| --- | --- | --- |
| 分辨率请求 | `320×240` | 眼部 ROI 的高速模式，不做软件放大 |
| FPS 请求 | `240` | 驱动协商目标，不代表一定达到 |
| 采集 FOURCC | `MJPG` | UVC/MJPEG 载荷，降低 USB 吞吐 |
| 录制 FOURCC | `MJPG` | AVI 兼容性和原始细节折中 |
| 预览 JPEG | 质量 `95` | 只影响浏览器预览，不影响 AVI |
| 合格帧率 | `target_fps × minimum_fps_ratio` | 默认 `240 × 0.65 = 156 fps` |
| 黑帧上限 | `20%` | 按灰度均值和标准差估计 |
| 后端 | `dshow` | Windows DirectShow；也支持 `msmf`/`any` |

“目标 FPS”与“实际 FPS”必须区分。页面卡片显示的是 `VideoCapture` 读取循环实际测得值；`quality.json` 才是导出后应归档的审计结果。

## 首次映射

1. 只关闭其他相机软件，确认 Windows 相机权限允许桌面程序访问。
2. 页面点“设备设置”→“扫描相机”，查看缩略图、实际宽高和驱动 FPS。
3. 分别“设为右眼/左眼”，确认左右物理位置而不是只看索引。
4. 必要时设置 `正常`、`旋转 180°`、`水平镜像` 或 `上下翻转`。
5. “保存并重启相机”后等待两路都显示为就绪；更换 USB 接口或相机后必须重复。

映射保存在 `%LOCALAPPDATA%\\ClinicalCapture\\camera-settings.json`，不写入受试者目录。右眼/左眼是逻辑角色；物理索引 `0/1` 可能因 USB 拓扑变化而交换。

## 图像清晰度

- 先调镜头焦距、工作距离、机械固定和近红外照明，再谈软件参数。
- 保持传感器有效曝光，避免把曝光时间拉长到牺牲 FPS；`exposure`/`gain` 的回读值以驱动实际支持为准。
- 采集链路全程保留相机原始像素；浏览器 `object-fit`、JPEG 预览或标注视频都不会增加真实细节。
- 录制优先使用 OpenCV 内置 MJPG writer，并尝试质量 `95`；若驱动不支持，会回退到默认 FOURCC，同时在 `recording_codec` 中标记。
- 不要通过“640×480 + 软件缩放”制造清晰度假象。当前两台 U2_BGS_VGA 在 Windows DirectShow 下实测 `320×240@240 MJPG` 可用；请求 `640×480` 时曾回退到 `YUY2`、约 `1 fps`。这是当前硬件/驱动的现场事实，不是所有相机的通用保证。

## 原始探测

```powershell
& .\\.venv\\Scripts\\python.exe scripts\\camera_probe.py --max-index 7
```

如果需要只验证原始图像，先停止 Clinical Capture，再用厂家工具或 OpenCV 检查单路相机；不要在采集服务运行时让第二个程序抢同一个设备句柄。

## 故障判断

| 现象 | 优先判断 | 处理 |
| --- | --- | --- |
| 无画面/黑帧 | 镜头盖、IR 光源、驱动、USB 或曝光 | 先单路探测，再检查照明和权限 |
| 单路正常、双路低 FPS | USB 控制器/HUB/线材带宽 | 分配不同 USB 控制器，保持 MJPG 和 320×240 |
| 实际 FPS 低于 156 | 驱动协商、曝光、后台占用或线材 | 查看 `actual`、`read_p95_ms`，点击重新检查；仍失败不要开始会话 |
| 打开后约 1 fps | 设备句柄未释放或 FOURCC 回退 | 等待自动恢复；必要时关闭占用程序后重启服务 |
| 采集中短暂掉线后自动恢复 | USB/DirectShow 瞬时空帧 | worker 会在连续空帧后释放旧句柄并按 `camera_reconnect_attempts` 有界重连；页面显示“正在重连”和累计次数，恢复后继续写入同一会话 |
| 重连耗尽 | USB、线材、供电或驱动持续故障 | 会话安全结束并保留已写入 AVI、帧时间戳和质量报告；先更换独立 USB 控制器/线材，再重新预检 |
| 画面倒置/左右错 | 物理安装和逻辑映射混淆 | 用缩略图重新设索引和方向，不能只翻浏览器显示 |
| 预览卡顿但 AVI 正常 | 浏览器刷新频率或 JPEG 编码 | 先以 `quality.json` 和帧 CSV 为准 |

## 断线恢复参数

`clinical-capture.json` 默认使用连续 3 次空帧后重连，最多尝试 3 次，每次等待 `0.2 s`：

- `camera_read_failure_threshold`：触发重连前允许的连续 `read()` 空帧数；避免一次瞬时 USB 抖动就重开设备。
- `camera_reconnect_attempts`：一次恢复周期最多重新打开设备的次数；达到上限才报告进程错误。
- `camera_reconnect_delay_seconds`：释放句柄后等待下一次打开的间隔，给 DirectShow 和 USB 控制器时间完成复位。

重连期间不会伪造帧，也不会把状态消息当作“新帧”；帧流看门狗只依据真实 `stats` 帧更新时间。恢复成功时，原始 AVI 继续写入，时间戳 CSV 会保留掉线造成的间隔，质量报告中的 `reconnect_count` 和 `reconnect_attempts_total` 可用于复核。

## 旋转是否生效

`frame_transform` 是采集 worker 内的像素变换，先于共享内存、瞳孔检测和 AVI 写入执行；它不修改 DirectShow/UVC 的传输参数，因此不会导致相机掉线。首次真实帧进入流水线后，状态会将 `transform_verified=true`，页面卡片显示“方向已验证”；在首帧验证前，真实模式的“开始录制”保持禁用。会话 `quality.json` 和 `config.json` 同时保存方向值。验证时应以实时预览和导出的 `*_annotated.avi`/`*_pupil_preview.jpg` 为准，原始 AVI 保持同样的已配置方向但不叠加标记。
