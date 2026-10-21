# 架构

## 调用链路

```text
Codex / MCP 客户端
        |
        v
.mcp.json -> cad-mcp 命令 -> MCP 主进程（超时监控）
                                      |
                                      v
                           常驻工作子进程（串行执行）
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

`core/isolation.py` 保留工具 Schema，替换 stdio 服务的执行入口；`worker.py` 使用私有
JSON-lines 协议串行调用原工具。图纸对象、COM 引用、配置缓存及确认令牌留在同一个工作
进程中，不传递或 pickle COM 对象。DXF 和完整/精简工具集使用相同执行入口，正常切换
模式不会新建工作进程或丢失规则缓存。直接导入后端的单元测试仍测试原始实现。

主进程在独立于工作进程的时钟下检查启动、执行和排队时限。stdout/stderr 持续排空，
发送也放在可被工作进程结束打断的后台线程中，避免大请求堵住输入管道。请求编号和每代
工作进程的独立响应队列防止接收过期结果。失效时只结束拥有的 Python 子进程，不自动
保存、撤销或关闭 CAD；写入结果未知会持续阻止写操作，直至人工核对后重启 MCP。

## AutoCAD 进程边界

`acad` 模式通过 Windows COM 连接 AutoCAD。作者实测中，Codex 桌面端在 AutoCAD 未运行
时触发冷启动会导致 AutoCAD 崩溃；手动打开 AutoCAD 后再由 MCP 建立连接和操作图纸则
可继续工作。`cc-haha` 暂未复现该问题，说明现象可能与客户端启动链路有关，但目前没有
足够证据确定根因。

因此该项目把“AutoCAD 已由用户手动启动”作为 Codex 桌面端进入 `acad` 模式的运行前置
条件，而不是在服务端或文档中假设 COM 冷启动可靠。

## 局部预览与图片传输

`drawing/preview.py` 使用 ezdxf 前端和 Matplotlib Agg 渲染二维模型空间，固定坐标范围、
画布大小和纵横比。DXF 文档不经保存，AutoCAD 则导出一次临时 DXF 快照，完成后删除自有
选择集与临时文件。渲染不改变原图纸实体、窗口视图和打印设置。

PNG 独占写入解析后 `log_dir` 同级 `images/本地日期/`，落盘成功后才构建文字元信息及
原生 MCP 图片内容。内联失败保留文字路径；普通和精简工具集均返回标准 `CallToolResult`。
`core/tool_results.py` 为原生结果添加明确的 JSON 传输封装，工作进程编码、主进程验证
并还原；旧工具继续返回原有 JSON 字符串。图纸对象及 COM 引用仍不跨进程。
