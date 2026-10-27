# 故障排查

## Codex 桌面端冷启动 AutoCAD 崩溃

### 已观察现象

作者实测发现：AutoCAD 尚未运行时，通过 Codex 桌面端调用 CAD MCP 冷启动 AutoCAD，
会导致 AutoCAD 崩溃。手动打开 AutoCAD 并等待加载完成后，再通过 MCP 打开图纸、新建
图纸或执行其他 `acad` 操作，可以规避作者当前环境中观察到的冷启动问题。

作者通过 `cc-haha` 使用本 MCP 时暂未复现相同问题。这是客户端差异的实测记录，不足以
证明根因，也不代表 `cc-haha`、其他电脑或其他 AutoCAD 版本始终安全。

### 推荐处理

1. 停止当前 MCP 写操作，不自动重复冷启动。
2. 由用户手动启动 AutoCAD 并等待主界面完全加载。
3. 调用 `cad_health(reconnect=false)`，检查模式、连接、活动文档和最近错误编号。
4. 确认 AutoCAD 已运行后，再切换 `acad` 模式并打开或新建图纸。
5. 如果仍然崩溃，停止后续操作，保留日志和版本信息，不把重试当成修复。

## MCP 已启动但未连接 AutoCAD

- 确认 AutoCAD 已由用户手动打开。
- 确认当前任务加载了 `cad-mcp@cad-mcp` 插件。
- 先使用 `cad_health(reconnect=false)` 读取状态。
- `connected=false` 不表示 AutoCAD 未运行。用户已明确 CAD 打开并要求操作图纸时，可直接连接已有实例；运行状态未知时先向用户确认。
- 在 Codex 桌面端发现 AutoCAD 未运行时，不使用 `reconnect=true`。
- 后端不再回退到启动新实例；找不到可连接实例时返回 `PRECONDITION_FAILED`，检查现有实例和权限，不循环重试。
- 检查 `cad-mcp config show` 的默认模式是否符合预期。

## 工作进程超时或退出

- `WORKER_UNAVAILABLE`：只读请求或尚未发送的请求未完成。工作进程已清理，不重放原请求；重新连接并按路径绑定图纸，重新加载配置和获取预览令牌。
- `EXECUTION_UNKNOWN`：写请求发送后失去响应。CAD 可能已部分执行；此时返回失败不代表已回滚。人工核对 CAD 后重启 MCP，期间禁止自动重试、保存、关闭或退出。
- `cad_health(reconnect=false)` 的 `worker` 字段可在执行卡住时读取；它不调用 COM，不能据此认定 CAD 自身已经恢复。
- `worker_cleanup=unverified` 时不继续建立工作进程或发送请求，先检查残留的 MCP 工作进程。

## 日志与错误编号

默认日志位置：

```text
~/.cad-mcp/logs/cad_mcp.log
```

可以使用以下命令调整日志目录：

```powershell
cad-mcp config set log-dir "D:\CAD-Logs"
```

报告问题时建议提供：

- Windows、AutoCAD、Codex 或其他 MCP 客户端版本。
- CAD MCP 版本及当前 `dxf`/`acad` 模式。
- AutoCAD 是手动启动还是由客户端冷启动。
- 崩溃发生在健康检查、切换模式、打开图纸还是新建图纸阶段。
- `cad_health` 返回的 `recent_error_id` 和对应日志片段。

不要上传包含完整工程模型、敏感路径或项目数据的日志。无法稳定复现时，应明确写成实测
现象，不推断 COM、Codex 或 AutoCAD 中哪一方是根因。

## 写入超时、回滚与图纸选择

工具在受支持的 COM 写入超时后，会先等待空闲并核验目标图纸的实际结果；若确认成功，
正常返回且不会重放。只有未确认、部分写入或回滚未核实才进入保护状态。不要看到超时
就再次调用绘图，也不要通过重连、保存或关闭图纸绕过保护。

DXF 切换用 `cad_list_drawings` 找到图纸 ID，再用 `cad_activate_drawing`；AutoCAD 则按
完整路径调用 `cad_open_drawing` 复用/绑定目标文档。点 CAD 标签不能代替 MCP 绑定。
`cad_new_drawing` 返回路径但磁盘上没有 DWG，可能是正常的“新建未保存”状态。

极端缩小的视图曾使两直线 FILLET 无几何变化；当前 pair 执行会临时放大并恢复视图。
若仍失败，检查实际半径、拾取点、图层和回读结果，不把所有 FILLET 失败都归因于缩放。
详见[实测记录](validation/audit-fixes-2026-10-27.md)。

## 解析结果或工具数量不符

三个历史查询 `cad_get_drawing_info`、`cad_list_entities`、`cad_check_drawing` 成功时
返回纯文本；按 JSON 解析失败不等于 CAD 操作失败。缓存健康诊断缺少 `semantic_policy`
时，不要自行补默认值。输入的必填性、联合对象和 null 用法见[完整工具参考](api-reference.md)。

源码仍为1.5.3而客户端仅看到101个工具，可能加载了旧安装或缓存。当前源码为116个原始
工具；核对实际集合、命令路径和服务进程，按[安装说明](installation.md#源码与已安装服务一致性)
更新后再验证，不能只看同一个版本号。

## 测试临时目录拒绝访问

本机曾出现 Windows 对仓库 `.cache/pytest/tmp` 或临时目录拒绝访问，不能据此认定产品
功能失败。停止该次运行，保留错误；先核对权限和执行环境，再经授权选择独立测试目录。
不要循环删除缓存、修改个人配置或重跑写入测试。
