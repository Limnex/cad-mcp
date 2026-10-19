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
- 在 Codex 桌面端发现 AutoCAD 未运行时，不使用 `reconnect=true`。
- 检查 `cad-mcp config show` 的默认模式是否符合预期。

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
