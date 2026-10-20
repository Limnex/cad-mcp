# CAD MCP

CAD MCP 是面向 Codex 等 MCP 客户端的本地 CAD 服务端，提供 104 个 `cad_*` 工具，
用于创建、检查和修改 DXF/DWG，配置 DraftingProfile 2.0、DWG 图框、布局视口以及 PDF
出图。仓库同时包含 Python 服务端、Codex 插件清单和内置 Skill。

仓库可以克隆到任意目录。安装后通过全局 `cad-mcp` 命令启动，不依赖仓库绝对路径。

> [!WARNING]
> **Codex 桌面端不要冷启动 AutoCAD。** 作者实测发现：AutoCAD 尚未运行时，通过
> Codex 桌面端调用 CAD MCP 启动 AutoCAD，会导致 AutoCAD 崩溃。请先手动打开
> AutoCAD 并等待主界面完全加载，再通过 MCP 执行健康检查、打开图纸、新建图纸和其他
> `acad` 操作。作者通过 `cc-haha` 使用本 MCP 时暂未复现该问题，但这只是当前实测结果，
> 不代表所有电脑和 AutoCAD 版本均不会发生。

## 能力与边界

| 模式 | 主要用途 | 运行要求 |
|:-----|:---------|:---------|
| `dxf` | 创建和修改 DXF、基础图元、图层、样式和结构化检查 | 不需要安装 AutoCAD |
| `acad` | 操作原生 DWG、布局、视口、页面设置、打印资源和关联字段 | Windows、本机 AutoCAD，并先手动打开 AutoCAD |

CAD MCP 只负责把已经确定的数据转换为 CAD 实体。工程尺寸、工艺路线和规范结论仍需由
用户或专业设计资料确定。完整 DWT、打印资源和原生 DWG 能力不能由 DXF 模式替代。

版本 `1.5.2` 为 `cad_make_block` 增加 `annotative=true`，在 `acad` 模式创建并回读原生注释性块；默认 `false` 保留原行为。DXF 模式传入 `true` 会明确拒绝，不以普通块冒充。

版本 `1.5.1` 允许 `cad_open_drawing` 传入现有本机图纸的盘符绝对路径；新建和另存为仍限定在受控图纸目录。打开目录外文件后，显式保存可能写回原位置，应先核对目标路径。

版本 `1.5.0` 新增按严格 `*_manifest.json` 规格卡审查指定 DWG 块，以及封闭边界
`SOLID` 实心填充和写后回读验证。无法读取或没有明确规格的字段返回“无法验证”，不会
用常见默认值替代。

## 编辑已有实体

用 `cad_get_entity_properties` 按十进制句柄读取当前属性，再用 `cad_edit_entity_properties` 修改。支持通用外观、文字、直线、圆，以及圆弧、多段线、块引用、填充、椭圆和点。

新增几何编辑面向 XY 平面，保留原标高和 Z 比例。块内容可按属性标签修改编号、名称、数值，也可按读取结果中的路径修改内部固定文字；固定文字会复制所需块定义，仅影响选中实例，其他同名块保留原内容。数值按字符串传入（如 `"12.00"`），保留显示格式。

编辑默认 `dry_run=true` 只预览差异；正式修改需显式设置 `false`。图层和线型须已存在，锁定图层拒绝修改；写后回读，失败尝试回滚，不自动保存。完整参数与示例见[工具契约](skills/cad-mcp/references/tool-contracts.md#已有实体属性读取与编辑)。

## 安装

需要 Windows、Python 3.11-3.14、`uv` 和 Codex CLI。仅使用 `acad` 模式时需要
本机 AutoCAD。

```powershell
git clone <repository-url> cad-mcp
Set-Location .\cad-mcp
pwsh -File .\scripts\install.ps1
```

安装脚本会安装 `cad-mcp` 命令、登记仓库内的本地插件市场，并安装
`cad-mcp@cad-mcp`。完成后重启 Codex 或新建任务。

验证安装：

```powershell
cad-mcp config show
codex plugin list
```

详见 [安装指南](docs/installation.md)。

## 安全开始

默认使用 `acad` 模式生成原生 DWG。先手动打开 AutoCAD，再执行健康检查和图纸操作。
无需 AutoCAD 的 DXF 工作流仍可显式选择。

### DXF 模式

1. 使用 `cad-mcp config set mode dxf` 设置默认模式。
2. 在客户端中先调用 `cad_health(reconnect=false)`。
3. 新建或打开 DXF，完成绘制后执行 `cad_check_drawing` 再保存。

### AutoCAD 模式

1. **手动启动 AutoCAD，并等待主界面完全加载。**
2. 在 Codex 桌面端调用 `cad_health(reconnect=false)`，不得用 `reconnect=true` 冷启动。
3. 确认 AutoCAD 已运行后，再切换到 `acad` 模式并新建或打开图纸。
4. 写入前确认当前活动图纸，重要操作先预览或 dry-run。
5. 保存前执行 `cad_check_drawing`；需要保留窗口时不要让 MCP 关闭 AutoCAD。

详细流程见 [使用指南](docs/usage.md)。冷启动崩溃和日志排查见
[故障排查](docs/troubleshooting.md)。

stdio 服务使用常驻工作进程执行工具，主进程监控超时；单次调用永久阻塞时，只清理自有工作进程，保留 AutoCAD。写入失去响应会标记为“结果未知”并阻止后续写操作，需人工核对 CAD 后重启 MCP；这不代表 CAD 操作已中止或回滚。恢复步骤见[使用指南](docs/usage.md#保存与故障停止)，异常模拟方法见[开发与验证](docs/development.md#异常模拟与进程隔离验证)。

## 配置

```powershell
cad-mcp config show
cad-mcp config path
cad-mcp config set drawings-dir "D:\CAD"
cad-mcp config set log-dir "D:\CAD-Logs"
cad-mcp config set mode acad
```

用户配置默认保存在 `~/.cad-mcp/config.json`，不会提交到 GitHub。配置优先级和环境变量
说明见 [配置指南](docs/configuration.md)。

## 文档

- [安装指南](docs/installation.md)
- [使用指南](docs/usage.md)
- [配置指南](docs/configuration.md)
- [故障排查](docs/troubleshooting.md)
- [架构说明](docs/architecture.md)
- [开发与验证](docs/development.md)
- [内置 Skill](skills/cad-mcp/SKILL.md)

## 开发验证

```powershell
pwsh -File .\scripts\verify.ps1
```

该命令同步锁定依赖、运行测试并临时构建 Python wheel；构建检查完成后自动清理，不生成或
发布 ZIP。GitHub Actions 会在 push 和 pull request 时执行相同类型的测试与构建检查。

## 许可证

本项目采用 [MIT License](LICENSE)。
