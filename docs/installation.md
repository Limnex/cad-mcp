# 安装

## 前置条件

- Windows 10 或 Windows 11
- Python 3.11-3.14
- `uv`
- AutoCAD，仅 `acad` 模式需要
- Codex CLI，用于登记和安装本地插件

## 从 GitHub 安装

```powershell
git clone <repository-url> cad-mcp
Set-Location .\cad-mcp
pwsh -File .\scripts\install.ps1
```

安装脚本执行三个动作：

1. 从仓库根目录安装或更新 `cad-mcp` 命令。
2. 当本机存在 Codex CLI 时，把仓库根目录登记为本地插件市场。
3. 安装 `cad-mcp@cad-mcp` 插件；相同版本已经安装时自动跳过。

重启 Codex 或新建任务后验证：

```powershell
cad-mcp config show
codex plugin list
```

`cad-mcp config show` 应显示图纸目录、日志目录、模式和配置文件位置；插件列表中应出现
`cad-mcp@cad-mcp`。移动仓库不会影响已经安装的命令；源码更新并提升版本号后，重新
运行安装脚本即可。

## Codex 桌面端首次使用

> [!WARNING]
> 作者实测发现，AutoCAD 完全关闭时，由 Codex 桌面端通过 MCP 冷启动 AutoCAD 会导致
> AutoCAD 崩溃。不要把“启动 AutoCAD”作为第一次 MCP 操作。

安全顺序：

1. 手动打开 AutoCAD。
2. 等待主界面、功能区和命令行加载完成。
3. 新建 Codex 任务或确认插件已经加载。
4. 调用 `cad_health(reconnect=false)`，不得用 `reconnect=true` 尝试冷启动。
5. 确认 AutoCAD 已运行后，再切换 `acad` 模式并打开或新建图纸。

作者通过 `cc-haha` 使用本 MCP 时暂未复现冷启动崩溃。该结果仅代表作者当前实测环境，
不是跨电脑、跨版本的兼容性保证。详见 [故障排查](troubleshooting.md)。

## 更新

```powershell
git pull
pwsh -File .\scripts\install.ps1
```

本项目通过 GitHub 源码仓库分发，不提供独立 ZIP 发布流程。

## 源码与已安装服务一致性

安装脚本的 `uv tool install --force` 会更新 Python 命令；同版本插件已安装时，插件安装
步骤会跳过。仓库源码、已安装 Python 命令、客户端缓存的 Skill/工具集合是不同副本，
重新安装命令不代表已有会话中的服务进程和 Skill 已更新。

当前源码仍标识 `1.5.3`，包含116个原始工具；未发布改动见[更新记录](../CHANGELOG.md)。
更新后重启服务或新建会话，检查 `cad_health` 的版本与 `tool_count`，同时对照
[完整工具参考](api-reference.md)核验实际工具集合。网关模式用 `cad_gateway_list` 列出
原始工具，不能把三个网关入口数量当成完整能力数量。

若仍只有旧工具或旧说明，记录命令解析位置（`Get-Command cad-mcp`）、插件版本和
实际列表，再按当前客户端的插件管理说明刷新缓存；不要循环安装或臆造不存在的更新命令。
本轮仓库测试未重新安装插件或更新依赖，因此不构成已安装服务同步验收。
