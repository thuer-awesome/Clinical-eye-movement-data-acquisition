# Clinical Capture 临床双眼高速采集

Clinical Capture 是面向临床研究和筛查记录的 Windows 双眼眼部相机采集工作站。它把相机打开、帧率预检、原始视频写盘、Pupil 2D 瞳孔检测、质量审计和 ZIP 导出收在一个本地页面里，适合医生按固定流程操作，也适合后续工程人员逐模块调试。

> **用途边界**：本软件是研究/筛查数据采集和质量审计工具，不是医疗器械软件，不提供诊断、治疗或风险分层结论。上线前仍需完成伦理、知情同意、院内数据治理、设备电气/光学安全和真人现场验收。

[![CI](https://github.com/thuer-awesome/Clinical-eye-movement-data-acquisition/actions/workflows/ci.yml/badge.svg)](https://github.com/thuer-awesome/Clinical-eye-movement-data-acquisition/actions/workflows/ci.yml)

## 能做什么

- 两路独立近眼相机进程；默认请求 `320×240 @ 240 fps, MJPG`，每路直接写 AVI 和逐帧时间戳。
- 预览、瞳孔检测、录制和浏览器请求相互隔离：浏览器卡顿不会把原始帧排成长队。
- 复用 `pupil-detectors` 的 `Detector2D`，以独立、限速的最新帧检测线程输出中心、椭圆、直径、置信度和延迟。
- 首次使用扫描相机，明确保存物理索引和画面方向；更换 USB 口或相机后可重新映射。
- 预检显示驱动实际宽高、FPS、FOURCC、黑帧比例、帧流超时和写盘状态；低帧率重启是有限的，不会无限重试。
- 结束时生成 `quality.json`、`analysis_ready.csv`，导出时保留原始证据并生成标注复核视频和 SHA-256 清单。
- 受试者编号、访视编号和操作员字段强制脱敏格式；必须确认知情同意后才可开始。

## 先给医生

从 GitHub Actions 的 Windows 构建产物或 Release 下载 `ClinicalCapture-portable.zip`，完整解压后双击目录内的 `ClinicalCapture.exe`（也可双击 `ClinicalCapture-Start.cmd`）。不需要安装 Python、Node 或打开 VSCode。

第一次在一台电脑上使用：点击“扫描相机”→根据缩略图分别设为右眼/左眼→确认方向→“保存并重启相机”。之后填写脱敏会话信息、勾选知情同意、开始采集；结束后先看质量指标，再在“已完成会话”中导出 ZIP。详细步骤见 [临床操作 SOP](docs/clinical-sop.md) 和 [Windows 安装](docs/installation.md)。

默认数据目录为 `%LOCALAPPDATA%\\ClinicalCapture\\data`。不要只复制一个 `.exe`；PyInstaller onedir 运行需要同目录的 `_internal` 文件。

## 开发者快速开始

需要 Python 3.11（Windows 现场推荐）和可用的 C++/OpenCV wheel。无相机时先使用模拟模式：

```powershell
py -3.11 -m venv .venv
& .\\.venv\\Scripts\\python.exe -m pip install -r requirements-clinical-capture.txt httpx
& .\\.venv\\Scripts\\python.exe -m apps.clinical_capture.main --simulate --no-browser --port 8785
```

浏览器打开 <http://127.0.0.1:8785/>。模拟画面只验证页面、会话、停止、导出和算法链路，不能证明真实相机质量。

真实相机模式：

```powershell
& .\\.venv\\Scripts\\python.exe -m apps.clinical_capture.main `
  --config configs\\clinical-capture.json `
  --data-dir "$env:LOCALAPPDATA\\ClinicalCapture\\data"
```

常用检查：

```powershell
& .\\.venv\\Scripts\\python.exe scripts\\camera_probe.py --max-index 7
& .\\.venv\\Scripts\\python.exe -m unittest discover -s tests -p "test*.py" -v
```

构建便携版：

```powershell
& .\\scripts\\install_clinical_capture.ps1
& .\\scripts\\build_clinical_capture.ps1
```

输出在 `dist/ClinicalCapture/` 和 `dist/ClinicalCapture-portable.zip`；Inno Setup 已安装时可追加 `-BuildInstaller` 生成安装程序。

## 文档地图

| 文档 | 面向谁 | 内容 |
| --- | --- | --- |
| [架构](docs/architecture.md) | 接手开发者 | 进程、线程、共享帧、状态机和故障收口 |
| [算法说明](docs/algorithm.md) | 算法/分析人员 | Detector2D 输入输出、阈值、时间戳和已知限制 |
| [相机与质量](docs/camera-tuning.md) | 设备工程师 | UVC/DirectShow、分辨率、帧率、清晰度、曝光和带宽 |
| [数据格式](docs/data-format.md) | 分析人员 | ZIP 内每个文件、CSV 字段和 manifest 校验 |
| [API](docs/api.md) | 前后端开发者 | 本地 REST 路由、状态字段和错误语义 |
| [Windows 安装](docs/installation.md) | IT/现场支持 | 便携版、源码运行和打包发布 |
| [临床操作 SOP](docs/clinical-sop.md) | 医生/操作员 | 采集前、采集中、结束和异常处理 |
| [交接与调试](docs/handoff.md) | 维护者 | 模块责任、验收清单、已知缺口和排障顺序 |

## 项目结构

```text
apps/
  clinical_capture/       FastAPI 服务、状态机、相机设置、瞳孔检测、导出和 UI
  multicamera_capture/    双缓冲共享内存帧交换（仅临床版所需模块）
configs/                  现场默认配置（可复制后修改）
docs/                     操作、算法、相机、数据、API 和交接文档
packaging/                PyInstaller spec、医生端说明和启动脚本
installer/                可选 Inno Setup 脚本
scripts/                  相机探测、安装和构建脚本
tests/                    无相机模拟测试、编码清晰度和导出完整性测试
```

## 版本与许可证

当前版本为 `0.4.0`。本项目自有代码和资料暂未授予默认开源许可证；公开分发前请获得项目负责人的明确授权。`pupil-detectors` 由 Pupil Labs 以 MIT 许可证发布，运行依赖和来源见 [NOTICE.md](NOTICE.md)。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要上传真实受试者数据、凭据或运行日志。
