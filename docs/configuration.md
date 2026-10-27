# 配置

默认配置文件是 `~/.cad-mcp/config.json`。建议使用命令修改，不需要手工编辑 JSON。

```powershell
cad-mcp config show
cad-mcp config path
cad-mcp config set drawings-dir "D:\CAD"
cad-mcp config set log-dir "D:\CAD-Logs"
cad-mcp config set mode acad
cad-mcp config set semantic-policy strict
cad-mcp config unset drawings-dir
```

配置优先级从高到低：

1. `CAD_DRAWINGS_DIR`、`CAD_LOG_DIR`、`CAD_MODE`、`CAD_SEMANTIC_POLICY` 环境变量。
2. 用户配置文件。
3. 默认值 `~/CAD`、`~/.cad-mcp/logs`、`acad`（DWG）和 `strict`（严格语义模式）。

未配置模式时使用 `acad`，用于原生 DWG；需先手动打开 AutoCAD。若不使用 AutoCAD，
显式运行 `cad-mcp config set mode dxf`。已有用户配置和 `CAD_MODE` 环境变量仍优先于默认值。

可用 `CAD_CONFIG_FILE` 临时指定另一份配置文件。用户配置、日志和图纸均位于仓库外，
不会随 Git 提交或插件升级被覆盖。


## 严格语义模式

默认 `semantic_policy=strict`。所有经 MCP 新建的顶层模型空间和纸空间图元，
必须在创建的同一次调用中绑定已登记的工程对象。该默认值改变旧版直接绘图的调用要求。

先调用 `cad_register_semantic_object`，登记 `project_id`、`object_id`、`name`、
`object_type`、`source`；可选 `parent_id` 和 `status`（`declared`、`confirmed`、`pending`）。
父对象须先登记。同编号同内容重复登记无变化；内容不同则拒绝，不静默修改用途。
登记保存在图纸的 `CAD_MCP_OBJECTS` 字典中，图元身份保存在 `CAD_MCP_ENV` XData 中。

例如先登记一个对象，再绘制其轮廓：

```json
{"params":{"object":{"project_id":"P1","object_id":"tank-01","name":"二沉池 01","object_type":"secondary_clarifier","source":"用户确认的设计资料","status":"confirmed"}}}
```

```json
{"params":{"cx":0,"cy":0,"radius":6000,"semantic":{"project_id":"P1","object_id":"tank-01","role":"池体外轮廓","view_id":"plan"}}}
```

第二次调用使用 `cad_draw_circle`。普通绘图、插块、复制、阵列、修剪、炸开重组、
图框、布局和视口创建均执行服务端检查；完整工具和网关工具执行同一策略。
复制时须明确目标对象；每个新图元获得新的 `entity_uid`。一次阵列绑定同一对象，
适用于该对象的重复部件；多个独立构筑物应分别登记绘制，或使用完整工程模型。
创建布局、正式应用 DWT 配置也可能产生视口实体，严格模式下须提供语义绑定；
DWT 只读预览仍可不提供绑定。

`cad_apply_environment_drawing` 从完整工程模型取得对象名称、类型和归属，在正式执行的
同一事务内先登记对象，再逐图元创建并验证身份；预览不写登记。辅助线可以登记为
真实的辅助对象或所属对象的辅助部件，不得为通过检查而编造用途。

图元创建、身份写入和回读验证任一步失败，整个调用回滚，不自动保存。
AutoCAD 回滚无法确认时禁止后续写入、保存、关闭或切换模式，须人工核对后重启 MCP。

`cad_get_entity_properties` 额外返回 `semantic`；也可用 `cad_get_entity_semantics`
单独读取。`cad_check_semantics` 只报告未登记、登记失联、身份冲突、重复身份及部分
几何属性变化，不自动回补，不将登记内容当作工程事实。现有图纸不会被自动迁移。
块内部图元继承块实例的工程归属，本阶段不单独登记其内部全部零件。

新增的名称、类型、来源等身份说明不计入几何同步指纹。相同模型再次应用时，旧图元
通过原位更新身份记录完成升级，保留句柄、人工外观和几何基线；实际几何变化仍按
原有增量同步规则处理。正式应用的 `metadata_updates` 列出仅更新身份记录的句柄。

损坏的图元身份或对象登记分别报告为 `invalid_metadata`、`invalid_registry`，
不阻断可读取的几何属性。审计返回 `complete=false` 与 `issues`，单图元返回
`duplicate_check_complete=false` 时不得声称重复身份已排除。真实 CAD 通信错误仍返回失败。
语义查询和单图元属性加语义查询共用一次 CAD 操作锁，避免读取到其他客户端的半成品。

需要运行旧版流程时，由操作者在启动前显式设置 `CAD_SEMANTIC_POLICY=legacy`，
或运行 `cad-mcp config set semantic-policy legacy` 后重启服务。MCP 没有供 Agent
切换该策略的工具。兼容模式不强制直接绘图绑定；完整工程模型仍保留其原有身份机制。

## 工具暴露模式

默认不设置 `CAD_MCP_TOOLSET`，服务器直接暴露全部 CAD 工具，行为与既有客户端兼容。

当 MCP 客户端对单次 `tools/list` 的 Schema 复杂度有限制时，可设置：

```text
CAD_MCP_TOOLSET=webcodex
```

该模式只暴露三个轻量入口：

- `cad_gateway_list`：列出原始 CAD 工具。
- `cad_gateway_describe`：按名称读取单个原始工具的 Schema。
- `cad_gateway_call`：按名称调用原始工具，由常驻工作进程串行执行。

两种工具暴露模式都使用一个 CAD MCP provider：主进程接收请求并监控超时，一个常驻工作进程持有当前图纸、DXF 内存状态、配置缓存和 AutoCAD 后端状态。网关复用原始工具实现，不为每次调用新建进程。未设置该变量或设置为 `all` 时，仍直接暴露全部工具。

工作进程超时或崩溃后会失去这些内存状态，且不自动重放请求。恢复连接和写入限制见[使用指南](usage.md#保存与故障停止)；网关模式同样适用。

WebCodex Runner 示例：

```toml
[[mcp.providers]]
id = "cad-mcp"
name = "CAD MCP"
executable = 'C:\path\to\cad-mcp-venv\Scripts\python.exe'
args = ["-m", "cad_mcp", "--toolset", "webcodex"]
env_from_env = { USERPROFILE = "USERPROFILE", SYSTEMROOT = "SYSTEMROOT" }
timeout_secs = 900
```

这里的 `timeout_secs` 是客户端等待调用结果的时限。示例设为 900 秒，为完整布局工具
最长 600 秒的执行，以及工作进程启动、请求排队和响应传输预留余量。它不会改变服务端
的执行时限：普通读取为 30 秒、普通写入为 120 秒，完整布局预览/校验为 300 秒、应用为
600 秒。客户端等待时间过短可能先中断等待，不能据此认定 CAD 操作已停止或回滚。
时限及结果未知时的处理见[保存与故障停止](usage.md#保存与故障停止)。

## 配置、缓存诊断与会话范围

配置在服务进程导入时加载。即使已用环境变量覆盖目录，配置加载器仍读取并校验
`CAD_CONFIG_FILE` 或默认配置；错误配置可能导致启动失败。离线验证应显式指向独立
配置路径，见[开发与验证](development.md#显式隔离配置的离线回归)。

`cad_health` 的工作进程结果会包含 `semantic_policy`；尚无工作进程、繁忙或失效时
主进程缓存诊断可能省略它。字段缺失不表示 legacy，也不表示 strict 已核验。
策略只能由操作者在启动前设置，不由 Agent 为绕过检查自行改变。

DXF 图纸 ID 仅在当前工作进程有效。配置缓存属于整个工作进程，图档及活动样式分别
保存在各图纸会话中；失效重启需重新加载配置、打开图纸和生成确认令牌。统一未知写入
保护同时适用于 strict 和 legacy，不因切换策略或重连自动解除。完整字段见
[工具参考](api-reference.md)，环境工程模型结构见[数据结构](../skills/cad-mcp/references/data-schema.md)。
