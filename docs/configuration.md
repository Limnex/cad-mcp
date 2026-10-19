# 配置

默认配置文件是 `~/.cad-mcp/config.json`。建议使用命令修改，不需要手工编辑 JSON。

```powershell
cad-mcp config show
cad-mcp config path
cad-mcp config set drawings-dir "D:\CAD"
cad-mcp config set log-dir "D:\CAD-Logs"
cad-mcp config set mode acad
cad-mcp config unset drawings-dir
```

配置优先级从高到低：

1. `CAD_DRAWINGS_DIR`、`CAD_LOG_DIR`、`CAD_MODE` 环境变量。
2. 用户配置文件。
3. 默认值 `~/CAD`、`~/.cad-mcp/logs` 和 `acad`（DWG）。

未配置模式时使用 `acad`，用于原生 DWG；需先手动打开 AutoCAD。若不使用 AutoCAD，
显式运行 `cad-mcp config set mode dxf`。已有用户配置和 `CAD_MODE` 环境变量仍优先于默认值。

可用 `CAD_CONFIG_FILE` 临时指定另一份配置文件。用户配置、日志和图纸均位于仓库外，
不会随 Git 提交或插件升级被覆盖。


## 工具暴露模式

默认不设置 `CAD_MCP_TOOLSET`，服务器直接暴露全部 CAD 工具，行为与既有客户端兼容。

当 MCP 客户端对单次 `tools/list` 的 Schema 复杂度有限制时，可设置：

```text
CAD_MCP_TOOLSET=webcodex
```

该模式只暴露三个轻量入口：

- `cad_gateway_list`：列出原始 CAD 工具。
- `cad_gateway_describe`：按名称读取单个原始工具的 Schema。
- `cad_gateway_call`：按名称在同一 CAD MCP 进程内调用原始工具。

原始工具实现不会复制，也不会启动多个 CAD MCP provider，因此当前图纸、DXF 内存状态和 AutoCAD 后端状态仍由同一个进程持有。未设置该变量或设置为 `all` 时，仍直接暴露全部工具。

WebCodex Runner 示例：

```toml
[[mcp.providers]]
id = "cad-mcp"
name = "CAD MCP"
executable = 'C:\path\to\cad-mcp-venv\Scripts\python.exe'
args = ["-m", "cad_mcp", "--toolset", "webcodex"]
env_from_env = { USERPROFILE = "USERPROFILE", SYSTEMROOT = "SYSTEMROOT" }
timeout_secs = 60
```
