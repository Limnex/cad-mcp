# 架构

## 调用链路

```text
Codex / MCP 客户端
        |
        v
.mcp.json -> cad-mcp 命令 -> Python MCP 服务端
                                      |
                         +------------+------------+
                         |                         |
                         v                         v
                    DXF 后端                 AutoCAD COM 后端
                    ezdxf                   已运行的 AutoCAD
```

MCP 客户端只调用已安装到 `PATH` 的 `cad-mcp` 命令，所以仓库可以位于任意目录。
无参数调用启动 stdio 服务；`cad-mcp config ...` 只处理用户配置，不启动 MCP 或 AutoCAD。

## 仓库层级

本仓库只维护 CAD MCP 一个插件，仓库根目录同时作为插件根目录和 Python 项目根目录。
市场清单中的插件来源为 `./`，不再嵌套 `plugins/cad-mcp/`。

- `.agents/plugins/marketplace.json`：将仓库登记为 Codex 本地插件市场。
- `.codex-plugin/plugin.json`：插件身份、版本和内置组件声明。
- `.mcp.json`：将 MCP 名称映射到 `cad-mcp` 命令。
- `src/cad_mcp/core`：契约、校验、事务、注册表和运行时诊断。
- `src/cad_mcp/drawing`：DXF 文档与 AutoCAD 后端。
- `src/cad_mcp/dwt`：DraftingProfile 2.0 和 DWT 工作流。
- `src/cad_mcp/engineering`：环境工程模型、管网、设施和表格编译。
- `skills/cad-mcp`：智能体工作流、安全边界、参考资料和 JSON 示例。
- `scripts`：安装与验证入口。
- `tests`：自动测试；`tools/manual`：需要手工运行的验证工具。
- `pyproject.toml`、`uv.lock`：Python 打包、测试配置与锁定依赖。
- `.venv`：本地 Python 开发环境，不纳入版本管理。
- `.cache/pytest`：测试缓存、临时图纸与日志；`.cache/manual`：人工验证输出。

用户的正式图纸与 MCP 运行日志仍由用户配置决定，不能放进测试缓存目录。

## AutoCAD 进程边界

`acad` 模式通过 Windows COM 连接 AutoCAD。作者实测中，Codex 桌面端在 AutoCAD 未运行
时触发冷启动会导致 AutoCAD 崩溃；手动打开 AutoCAD 后再由 MCP 建立连接和操作图纸则
可继续工作。`cc-haha` 暂未复现该问题，说明现象可能与客户端启动链路有关，但目前没有
足够证据确定根因。

因此该项目把“AutoCAD 已由用户手动启动”作为 Codex 桌面端进入 `acad` 模式的运行前置
条件，而不是在服务端或文档中假设 COM 冷启动可靠。
