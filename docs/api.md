# 本地 API

服务默认监听 `http://127.0.0.1:8785`，只用于同一台电脑上的 UI。所有 JSON 写请求都应带 `Content-Type: application/json`；路由错误返回 FastAPI 标准 `{"detail": "..."}`。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/health` | 轻量存活检查，返回版本和状态 |
| GET | `/api/config` | 当前公开配置（不含敏感凭据） |
| GET | `/api/status` | 应用状态、预检、相机质量、活动/最近会话 |
| POST | `/api/preflight` | 重新读取预检 |
| POST | `/api/restart` | 有界重启相机和检测服务 |
| POST | `/api/shutdown` | 安全结束会话并请求本地服务退出 |
| GET | `/api/device-settings` | 读取本机相机映射和方向 |
| POST | `/api/cameras/scan` | 扫描索引并返回缩略图/协商模式 |
| PUT | `/api/device-settings` | 保存映射；录制期间返回 `409` |
| POST | `/api/session/start` | 创建会话并开始双路写盘 |
| POST | `/api/session/stop` | 结束会话并写质量报告 |
| GET | `/api/session/current` | 当前/最近会话 |
| GET | `/api/sessions` | 按时间倒序列出会话 |
| GET | `/api/session/{id}` | 读取一个会话元数据 |
| POST | `/api/session/{id}/screening` | 给已停止会话追加筛查备注 |
| GET | `/api/session/{id}/export-info` | 生成导出 ZIP，返回本机绝对路径和大小 |
| GET | `/api/session/{id}/export` | 下载导出 ZIP |
| GET | `/api/preview/{camera}.jpg` | 获取当前预览 JPEG |

`GET /api/status` 的 `preflight.pupil_alignment` 表示录制前总门禁：`required`、`ready`、`progress`、`hold_seconds`、`confidence_threshold` 和当前 `guidance`。每只眼的 `preflight.cameras.<eye>.pupil.alignment` 还包含状态（`searching`、`adjust`、`holding`、`ready` 或 `error`）、置信度、中心是否合格、检测帧新鲜度和方向提示。真实模式下，只要任一必需眼位未对准，`preflight.ready=false`，`POST /api/session/start` 会返回 `409`，不能通过 API 绕过。模拟模式会明确标记对准门禁为跳过。

## 开始会话示例

```powershell
$payload = @{ subject_code = 'P20260827-01'; visit_id = 'V1'; operator = 'OP-01'; consent_confirmed = $true } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8785/api/session/start -Method Post -ContentType application/json -Body $payload
```

服务端会拒绝路径穿越、重复相机索引、相同左右眼、非法受试者编号、未确认知情同意、超范围筛查分数和未完成瞳孔对准。UI 可以显示状态，但不应通过“online”字段自行推断硬件就绪；以 `preflight.ready`、`pupil_alignment.ready` 和必需相机的 `quality_ok` 为准。
