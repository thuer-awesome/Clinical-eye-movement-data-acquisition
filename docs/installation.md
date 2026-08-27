# 安装与发布

## 医生端：便携版

1. 在受控的院内电脑上下载 Windows Release/Actions 产物 `ClinicalCapture-portable.zip`。
2. 将整个 ZIP 解压到本地可写目录，例如 `C:\ClinicalCapture`；不要从压缩包内直接运行，也不要只复制 `ClinicalCapture.exe`。
3. 首次运行前确认 Windows 相机隐私设置允许桌面应用访问相机，并关闭厂家预览、Teams、Zoom 等占用程序。
4. 双击 `ClinicalCapture.exe` 或 `ClinicalCapture-Start.cmd`。页面只绑定本机 `127.0.0.1`，通常不需要开放防火墙入站端口。
5. 首次完成相机扫描/映射后再开始采集；数据默认在 `%LOCALAPPDATA%\\ClinicalCapture\\data`。

卸载便携版只需关闭服务后删除程序目录；会话数据位于 LocalAppData，需按院内留存/备份政策单独处理，不要未经授权删除。

## 开发端：源码运行

要求 Windows 10/11、Python 3.11、能安装 `opencv-python` 的 64 位环境。Python 3.13 不在发布验证范围内。

```powershell
& .\\scripts\\install_clinical_capture.ps1
& .\\.venv\\Scripts\\python.exe -m apps.clinical_capture.main --simulate --no-browser
```

源码模式不需要 Node；UI 是随 Python 服务提供的静态文件。真实设备模式去掉 `--simulate`，并使用 `configs/clinical-capture.json` 或复制后的现场配置。

## 构建便携版

```powershell
& .\\scripts\\build_clinical_capture.ps1
```

脚本将依赖冻结进 PyInstaller onedir，复制启动脚本和医生说明，并生成 `dist/ClinicalCapture-portable.zip`。可选 Inno Setup：

```powershell
& .\\scripts\\build_clinical_capture.ps1 -BuildInstaller
```

构建机必须能找到 `iscc.exe`。不要将 `dist/`、`.venv/`、真实 session 或模型权重提交到仓库；`.gitignore` 已覆盖这些路径。

## 发布验收

- 在 CI 的 Ubuntu job 运行单元测试和 `compileall`。
- 在 Windows job 构建便携包，上传 Actions artifact；正式 Release 前在一台无 Python 的干净电脑解压测试。
- 使用 `--simulate` 验证页面、开始/停止、导出和 manifest；使用真实相机另做现场记录。
- 发布说明必须区分“软件仿真通过”“设备驱动实测”“真人临床验收”，不能把三者混写成一个通过结论。
