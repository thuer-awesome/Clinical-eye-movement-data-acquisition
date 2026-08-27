# Contributing

## 提交前检查

1. 先确认修改属于正确临床模块，并更新相关文档和测试。
2. 不要提交 `.venv/`、运行日志、缓存、压缩包、便携解包目录或真实受试者数据。
3. 涉及相机安装位置、采集参数、编码、标定或时间同步的改动，补充验证条件和已知限制。
4. 涉及第三方依赖或厂家 SDK 的改动，记录版本、来源、许可证和再分发条件。
5. 仿真测试不能替代真实双眼相机、长时间写盘、照明安全和真人临床验收。

## 本地验证

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -p "test*.py" -v
& .\.venv\Scripts\python.exe -m compileall -q apps scripts tests
```

若本机没有真实相机或外设，应在提交说明中明确使用了 Mock/仿真验证。
