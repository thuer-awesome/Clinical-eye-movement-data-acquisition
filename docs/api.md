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

`GET /api/status` 的 `preflight.pupil_alignment` 表示录制前总门禁：`required`、`ready`、`progress`、`hold_seconds`、`confidence_threshold`、实际 `candidate_filter` 参数（包括 `min_diameter_ratio`）和当前 `guidance`。每只眼的 `preflight.cameras.<eye>.pupil` 还包含 `quality_ok`、`quality_state`、`rejected_reason`、`temporal_distance` 和 `temporal_stable`；其 `alignment` 包含状态（`searching`、`adjust`、`holding`、`ready` 或 `error`）、置信度、中心是否合格、检测帧新鲜度和方向提示。被质量层拒绝的候选（包括过小的睫毛/反光轮廓）会令 `direction=search`，只提示重新寻找/确认瞳孔，不生成上下左右箭头。真实模式下，只要任一必需眼位未对准，`preflight.ready=false`，`POST /api/session/start` 会返回 `409`，不能通过 API 绕过。模拟模式会明确标记对准门禁为跳过。

相机状态还会返回 `frame_transform`、`transform_applied` 和 `transform_verified`。后者在首帧经过 worker 的方向处理并进入共享内存后变为 `true`，可用来确认旋转/镜像设置确实生效；真实模式下首帧验证前不会通过录制门禁。`stats.frames_in_interval`、递增的 `sequence` 和 `timestamp_ns` 共同证明统计周期确有新帧，零帧或重复统计不会刷新看门狗，`capture_fps=0` 会阻塞录制。发生短暂 USB 空帧时，状态会短暂显示 `reconnecting`，并更新 `reconnect_count`；恢复成功后继续原会话写盘。`last_frame_age_ms` 只由真实帧刷新，不能用重连提示判断帧流仍然健康。

## 开始会话示例

```powershell
$payload = @{ subject_code = 'P20260827-01'; visit_id = 'V1'; operator = 'OP-01'; consent_confirmed = $true } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8785/api/session/start -Method Post -ContentType application/json -Body $payload
```

服务端会拒绝路径穿越、重复相机索引、相同左右眼、非法受试者编号、未确认知情同意、超范围筛查分数和未完成瞳孔对准。UI 可以显示状态，但不应通过“online”字段自行推断硬件就绪；以 `preflight.ready`、`pupil_alignment.ready` 和必需相机的 `quality_ok` 为准。
