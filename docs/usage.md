# 使用指南

## 选择模式

默认模式为 `acad`，用于原生 DWG。首次使用前先手动打开 AutoCAD；不使用 AutoCAD 时，
可显式选择 `dxf`。模式配置在 MCP 服务下次启动时读取，当前会话用 `cad_switch_mode` 切换。

`dxf` 模式适合无需 AutoCAD 的文件生成和基础编辑。`acad` 模式通过 Windows COM 操作
已经运行的 AutoCAD，用于原生 DWG、布局、视口、页面设置、打印资源和关联字段。

```powershell
cad-mcp config set mode acad
# 需要独立生成 DXF 时再使用：
# cad-mcp config set mode dxf
cad-mcp config show
```

切换默认配置不会替代会话内的状态检查。每次开始前都应调用
`cad_health(reconnect=false)`，确认当前模式、连接、活动文档和最近错误编号。

## Codex 桌面端安全启动

在 Codex 桌面端使用 `acad` 模式时，按以下顺序操作：

1. 手动启动 AutoCAD，等待主界面完全加载。
2. 保存或关闭不希望 MCP 修改的图纸，确认当前活动窗口。
3. 调用 `cad_health(reconnect=false)`；不要用 `reconnect=true` 冷启动 AutoCAD。
4. 用户已明确 CAD 打开并要求操作图纸时，可直接连接已有实例，无须额外确认。`connected=false` 只表示 MCP 未连接；CAD 是否打开未知时先向用户确认。
5. 使用 MCP 按完整路径复用并绑定已打开图纸，或打开指定图纸后绑定，或新建图纸；随后核对文档。找不到现有 CAD 实例时停止，不自动启动新实例。
6. 执行绘制、检查、保存等后续操作。

如果 AutoCAD 尚未运行，应停止 `acad` 操作并提示用户手动打开，而不是连续重试。作者
通过 `cc-haha` 使用本 MCP 时暂未复现相同崩溃，但仍建议在重要工作中采用上述顺序。

## DXF 基础流程

1. `cad_health(reconnect=false)`。
2. 新建或打开 DXF。
3. 设置图层、文字和尺寸样式。
4. 绘制或修改实体。
5. 查询关键实体并检查几何范围。
6. 执行 `cad_check_drawing`。
7. 保存文件并确认输出路径。

## AutoCAD 基础流程

1. 按“Codex 桌面端安全启动”手动打开 AutoCAD。
2. 健康检查并确认活动图纸。
3. 新建或打开工作图纸。
4. 写入前建立只读基线；重要配置先预览或 dry-run。
5. 写操作使用撤销组，发生关键错误后停止，不连续尝试修改。
6. 保存前执行 `cad_check_drawing`。
7. 用户要求保留窗口时，不关闭 AutoCAD 或当前图纸。

## 打开指定路径的图纸

`cad_open_drawing` 的 `filename` 可传受控图纸目录中的单个文件名，也可传现有本机图纸的盘符绝对路径，例如 `D:\项目\总图.dwg`。DXF 模式只接受 `.dxf`；acad 模式接受 `.dwg` 和 `.dxf`。不支持 UNC 网络路径、相对子路径或 `..` 回退。绝对路径只对“打开”生效；新建和另存为仍限定在受控目录。

打开不会自动保存。若在目录外图纸上显式调用 `cad_save_drawing`，DWG 和 DXF 模式下的 DXF 会写回原文件；acad 模式打开 DXF 后则在同目录生成 DWG，目标已存在时停止。保存前先核对目标路径并执行 `cad_check_drawing`。

## 创建注释性块

`cad_make_block` 可选传入 `annotative=true`，在 `acad` 模式把指定模型空间实体建为 AutoCAD 原生注释性块。成功响应除原有 `message` 外，还返回 `annotative=true`、`verified=true` 和 `readback`；回读包括块内实体数、原生注释性标记与源实体移除状态。命令使用指定基点并避免对象捕捉改变基点；失败时触发当前操作的撤销组回滚。

省略 `annotative` 等同于 `false`，旧建块行为不变。`dxf` 模式传 `true` 会在任何写入前返回 `UNSUPPORTED_MODE`。此开关只属于 `cad_make_block`，不改变 `cad_insert_block` 或 `cad_create_or_update_attribute_block` 的行为；插块时手动缩放不能代替原生注释性。

## DWT 与环境工程工作流

DraftingProfile 2.0 和环境工程语义模型必须先校验、再预览，展示冲突与未支持项并获得
用户确认后才能正式应用。固定图框不得缩放，模板源文件保持只读，打印资源必须根据当前
AutoCAD 的真实 PC3、纸张和 CTB/STB 列表核验。

详细调用顺序见内置 Skill 的
[`references/workflows.md`](../skills/cad-mcp/references/workflows.md)，参数与
返回字段见
[`references/tool-contracts.md`](../skills/cad-mcp/references/tool-contracts.md)。

## 查看图纸局部

先新建或打开目标图纸，再调用 `cad_preview_region`。四个坐标使用图纸单位，表示模型空间
WCS XY 的矩形范围；即使直线端点都在范围外，穿过范围的部分仍会显示。输出保持几何比例，
图片尺寸与范围宽高比不同时留边，不拉伸图形。空白范围也会生成空白 PNG。

```json
{"params":{"x_min":0,"y_min":0,"x_max":10000,"y_max":6000,"width":1200,"height":800,"background":"white"}}
```

返回内容的第一项是文字 JSON，含 `image_path` 绝对路径、坐标范围、像素尺寸、渲染方式、
`image_included` 和 `warnings`；默认第二项是 `image/png` 图片内容，支持看图的客户端可以
直接传给模型。普通调用和 `cad_gateway_call` 均保留图片，网关的参数结构保持不变。

图片先保存到日志目录同级 `images/YYYY-MM-DD/`，文件名含时间和唯一标识，不覆盖旧图。
例如日志在 `D:/CAD/log/`，图片就在 `D:/CAD/images/日期/`。图片不自动删除，由用户按需清理。
内联图片读取/编码失败或超过 8 MiB 时，文字仍含路径和 `image_error`，PNG 文件保留。
客户端若拒绝整个图片响应，可再次调用时指定 `include_image=false`，以纯文字取得新预览的
路径；这会生成另一张图片。整个调用失败时，不能保证客户端已收到路径，也不会自动重试。

DXF 直接使用当前内存文档；AutoCAD 仅用临时 DXF 快照，不另存、保存原图纸，不更改
视图或布局打印设置。两种模式均使用 ezdxf 渲染 WCS XY 俯视图，未保存修改会包含在内。
代理对象、缺失字体和外部参照可能不完整；外部栅格图片不读取原文件，仅绘制边框。
SHX 大字体在有可用中文字体时仅在预览中替换，并在 `warnings` 中说明，图纸样式不变。

该工具会创建图片文件，因此标注为有副作用工具；它不修改图纸实体。AutoCAD 导出需要
已经运行并绑定的图纸，不会冷启动 AutoCAD。超过 100,000 个模型空间实体时拒绝生成预览。

## 保存与故障停止

stdio 服务把工具执行放入常驻工作子进程，主进程独立监控超时。工作进程启动最多等待
15 秒；请求发送后，只读操作最多等待 30 秒，写操作最多等待 120 秒。请求串行执行，
排队也有时限；不使用线程池并发修改 CAD。原有工具名称和参数不变。

AutoCAD 扫描内部仍有 30 秒协作式上限。若单次原生 COM 调用永不返回，主进程会结束
自己的工作子进程并返回错误，保留 AutoCAD；这只能让 MCP 恢复响应，不能证明 CAD
内部操作已中止。工作进程失效后，图纸绑定、内存配置缓存和确认令牌均需重新建立，
不会重放失败的请求；DXF 模式的未保存内存状态也不能自动恢复。

只读请求失效返回 `WORKER_UNAVAILABLE`：确认 CAD 运行后重新连接，按完整路径绑定
图纸，并重新加载配置和预览。写请求发送后失去响应返回 `EXECUTION_UNKNOWN`，
`retryable=false`、`rollback_status=not_verified`；此后阻止写入、保存、关闭、退出和模式
切换。重新连接或重新绑定不能解除该状态，必须人工核对 CAD 后重启 MCP；不得声称已
回滚或自动重试。普通重连不会启动新 AutoCAD 实例。

执行期间或失效后的 `cad_health(reconnect=false)` 可由主进程立即返回监控状态，不访问
COM；其中 `worker` 和 `last_known_document_open` 是缓存证据，不是新的 CAD 活性证明。

断线检查忽略最多 `1e-9 mm` 的数值噪声，且该阈值不超过查询容差的百万分之一；
真实小间隙以有效数字显示，不再四舍五入为 `0.0 mm`。检查不会吸附或修改图元。

网关模式先直接调用 `cad_gateway_list` 和 `cad_gateway_describe`，读取现有工具的
`inputSchema`：`required`、默认值、类型和范围。再调用 `cad_gateway_call`，例如：

```json
{"tool_name":"cad_query_entities","arguments":{"params":{"limit":100,"offset":0}}}
```

下一页使用返回的 `next_offset`。`cad_query_entities.limit` 范围是 1–100；不要套用
其他列举接口的 500 上限。参数错误返回 `INVALID_ARGUMENT`，错误详情包含字段位置和
限制，不会进入绘图操作；网关工具自身应直接调用，不放进 `cad_gateway_call`。

失败时读取结构化错误中的 `code`、`operation`、`retryable`、`hint` 和 `request_id`。
只有 `retryable=true` 且健康检查确认文件状态正常时，才考虑用相同参数重试。回滚和撤销组
不能替代文件备份。

日志默认位于 `~/.cad-mcp/logs/cad_mcp.log`。排查启动崩溃和连接问题见
[故障排查](troubleshooting.md)。
