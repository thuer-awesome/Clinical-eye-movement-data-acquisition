# Clinical Capture 临床双眼高速采集

Clinical Capture 是面向临床研究和筛查记录的 Windows 双眼眼部相机采集工作站。V2 在独立 `version2-pyqt` 分支提供原生 PySide6 页面，把 Pupil Reproduction 标定前预览体验、相机预检、原始视频写盘、Pupil 2D 瞳孔检测、质量审计和 ZIP 导出收在一个窗口里；原浏览器版入口继续保留，便于兼容和调试。

> **用途边界**：本软件是研究/筛查数据采集和质量审计工具，不是医疗器械软件，不提供诊断、治疗或风险分层结论。上线前仍需完成伦理、知情同意、院内数据治理、设备电气/光学安全和真人现场验收。

[![CI](https://github.com/thuer-awesome/Clinical-eye-movement-data-acquisition/actions/workflows/ci.yml/badge.svg)](https://github.com/thuer-awesome/Clinical-eye-movement-data-acquisition/actions/workflows/ci.yml)

## 能做什么

- 两路独立近眼相机进程；默认请求 `320×240 @ 240 fps, MJPG`，每路直接写 AVI 和逐帧时间戳。
- 预览、瞳孔检测、录制和浏览器请求相互隔离：浏览器卡顿不会把原始帧排成长队。
- 复用 `pupil-detectors` 的 `Detector2D`，以独立、限速的最新帧检测线程输出中心、椭圆、直径、置信度和延迟。
- 首次使用扫描相机，明确保存物理索引和画面方向；更换 USB 口或相机后可重新映射。
- 预检显示驱动实际宽高、FPS、FOURCC、黑帧比例、帧流超时和写盘状态；低帧率重启是有限的，不会无限重试。
- 采集中遇到瞬时 USB/DirectShow 空帧时，worker 会有界释放并重连同一设备，恢复后继续当前会话；页面和质量报告记录重连次数。
- 录制前要求双眼瞳孔达到置信度阈值并连续稳定保持；未对准时按页面提示向左/右/上/下微调相机，后端也会阻止绕过 UI 开始录制。
- 采集控制区域明确显示“可以录制/暂不可录制”，并逐条说明设备预检、瞳孔对准、帧流或会话信息的阻塞原因，医生不需要猜测哪个条件未满足。
- V2 同屏显示左右眼实时图像、检测椭圆、置信度、方向箭头和连续保持进度；中央“眼眶正对范围”帮助医生粗调镜头，框线按当前真实置信度由红到黄再到绿变化，掉线/过期帧显示灰色，但不参与 `valid` 或录制门禁计算。
- 结束时生成 `quality.json`、`analysis_ready.csv`、`*_pupil.csv`，导出时保留原始证据并生成带瞳孔轮廓的复核视频、代表性静帧、`pupil_report.json`、面向医生的 `clinical_report.json` 和 SHA-256 清单。
- 受试者编号、访视编号和操作员字段强制脱敏格式；必须确认知情同意后才可开始。

## 先给医生

从 GitHub Actions 的 Windows 构建产物或 Release 下载 `ClinicalCaptureV2-portable.zip`，完整解压后双击目录内的 `ClinicalCaptureV2.exe`（也可双击 `ClinicalCaptureV2-Start.cmd`）。不需要安装 Python、Node、浏览器服务或打开 VSCode。

第一次在一台电脑上使用：点击“扫描相机”→根据画面分别设为右眼/左眼→确认方向→“保存并重启相机”。正式采集前根据每只眼实时箭头和文字，把整只瞳孔移入画面并改善焦距/曝光；只有真实检测置信度和几何连续达标后，开始按钮才会启用。结束后先看质量指标、`pupil_report.json` 和 `clinical_report.json`，再导出 ZIP。详细步骤见 [V2 页面说明](docs/qt-v2.md)、[临床操作 SOP](docs/clinical-sop.md) 和 [Windows 安装](docs/installation.md)。

默认数据目录为 `%LOCALAPPDATA%\\ClinicalCapture\\data`。不要只复制一个 `.exe`；PyInstaller onedir 运行需要同目录的 `_internal` 文件。

## 开发者快速开始

需要 Python 3.11（Windows 现场推荐）和可用的 C++/OpenCV wheel。无相机时先使用模拟模式：

```powershell
& .\\scripts\\install_clinical_capture_v2.ps1
& .\\.venv-v2\\Scripts\\python.exe -m apps.clinical_capture.qt_main --simulate
```

模拟画面只验证 Qt 页面、会话、停止、导出和算法链路，不能证明真实相机质量。兼容浏览器版仍可运行 `python -m apps.clinical_capture.main --simulate --no-browser --port 8785`。

真实相机模式：

```powershell
& .\\.venv-v2\\Scripts\\python.exe -m apps.clinical_capture.qt_main `
  --config configs\\clinical-capture.json `
  --data-dir "$env:LOCALAPPDATA\\ClinicalCapture\\data"
```

常用检查：

```powershell
& .\\.venv-v2\\Scripts\\python.exe scripts\\camera_probe.py --max-index 7
& .\\.venv-v2\\Scripts\\python.exe -m unittest discover -s tests -p "test*.py" -v
```

构建便携版：

```powershell
& .\\scripts\\install_clinical_capture_v2.ps1
& .\\scripts\\build_clinical_capture_v2.ps1
```

输出在 `dist/ClinicalCaptureV2/` 和 `dist/ClinicalCaptureV2-portable.zip`；Inno Setup 已安装时可追加 `-BuildInstaller` 生成安装程序。V1 的 `install_clinical_capture.ps1` / `build_clinical_capture.ps1` 保留用于浏览器版兼容构建。

## 文档地图

| 文档 | 面向谁 | 内容 |
| --- | --- | --- |
| [架构](docs/architecture.md) | 接手开发者 | 进程、线程、共享帧、状态机和故障收口 |
| [算法说明](docs/algorithm.md) | 算法/分析人员 | Detector2D 输入输出、阈值、时间戳和已知限制 |
| [相机与质量](docs/camera-tuning.md) | 设备工程师 | UVC/DirectShow、分辨率、帧率、清晰度、曝光和带宽 |
| [数据格式](docs/data-format.md) | 分析人员 | ZIP 内原始/标注图像、瞳孔 CSV、报告和 manifest 校验 |
| [API](docs/api.md) | 前后端开发者 | 本地 REST 路由、状态字段和错误语义 |
| [Windows 安装](docs/installation.md) | IT/现场支持 | 便携版、源码运行和打包发布 |
| [V2 原生页面](docs/qt-v2.md) | 医生/现场工程师 | 左右眼预览、引导框、门禁、重连和导出 |
| [临床操作 SOP](docs/clinical-sop.md) | 医生/操作员 | 采集前、采集中、结束和异常处理 |
| [交接与调试](docs/handoff.md) | 维护者 | 模块责任、验收清单、已知缺口和排障顺序 |

## 项目结构

```text
apps/
  clinical_capture/       Qt/FastAPI 入口、状态机、相机设置、瞳孔检测、导出和 UI
  multicamera_capture/    双缓冲共享内存帧交换（仅临床版所需模块）
configs/                  现场默认配置（可复制后修改）
docs/                     操作、算法、相机、数据、API 和交接文档
packaging/                V1/V2 PyInstaller spec、医生端说明和启动脚本
installer/                可选 Inno Setup 脚本
scripts/                  相机探测、安装和构建脚本
tests/                    无相机模拟测试、编码清晰度和导出完整性测试
```

## 版本与许可证

当前版本为 `0.7.0`（V2 Qt 页面）。本项目自有代码和资料暂未授予默认开源许可证；公开分发前请获得项目负责人的明确授权。`pupil-detectors` 由 Pupil Labs 以 MIT 许可证发布，PySide6/Qt 及其他运行依赖的来源见 [NOTICE.md](NOTICE.md)。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要上传真实受试者数据、凭据或运行日志。
