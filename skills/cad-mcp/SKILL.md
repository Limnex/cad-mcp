---
name: cad-mcp
description: 使用 CAD MCP 创建、检查和修改 DXF/DWG，通过外部 DraftingProfile 2.0 配置通用 DWT 环境，并处理环境工程语义模型、DWG 图框、布局视口与 PDF 出图；不替代工程设计计算或规范判断。
---

# CAD MCP 使用指南

## 使用边界

CAD MCP 负责把已确定的工程数据转成 CAD 实体，并负责检查、更新和出图。工程参数、构筑物尺寸、工艺路线和规范结论必须依据用户资料确定，不能由 CAD 工具自行臆造。

模型、制图配置、规则集和模板清单应尽量外部化，避免把课程项目、特定工艺或学校图框写死在服务端代码中。

服务通过已安装的 `cad-mcp` 命令启动，不依赖源码所在目录。图纸目录由 `CAD_DRAWINGS_DIR` 配置，未配置时使用 `~/CAD`；不要推断或调用某台电脑上的固定脚本路径。

### AutoCAD 启动前置条件

在 Codex 桌面端使用 `acad` 模式时，先调用 `cad_health(reconnect=false)`，并按用户是否已确认 AutoCAD 打开区分后续操作。用户明确说明 CAD 已打开且要求操作图纸时，可直接按任务要求连接已有实例：按完整路径复用并绑定已打开的图纸，或打开指定图纸后绑定，或新建图纸；不再单独索要一次连接授权。CAD 是否打开未知时，停下来向用户确认；用户说明未打开时，请其手动启动并等待主界面加载。`connected=false` 仅表示 MCP 尚未连接，不能据此认定 CAD 未运行。后端只连接已有实例；找不到实例或连接失败时停止，不自动启动、关闭实例或循环重试。作者实测的冷启动崩溃根因尚未确认。

作者通过 `cc-haha` 使用本 MCP 时暂未复现相同问题。这只是当前实测差异，不得推断为普遍兼容性结论；重要图纸仍优先采用手动启动 AutoCAD 的顺序。详细处理见 [工作流与异常处理](references/workflows.md)。

## 先选择工作方式

### 通用 DWT 配置工作流（配置模板时推荐）

适用于按外部 JSON 建立或维护通用 DWT 图纸环境。DWT 与独立 DWG 块库分离，服务端不得写死项目、学校、图框或本机打印资源名称。

1. `cad_health(reconnect=false)`，确认服务版本、模式和活动图纸
2. 切换到 `acad`；读取图纸设置、图层、文字/尺寸样式、布局、页面设置和真实打印资源
3. `cad_load_drafting_profile` 加载严格的 DraftingProfile 2.0，核对绝对路径与 SHA-256
4. `cad_preview_dwt_profile` 或 `cad_apply_dwt_profile(dry_run=true)`，核对计划、冲突、未支持项和目标图纸
5. 只有计划无冲突/未支持项且用户确认后，才在 30 分钟内以原参数和 `confirmation_token` 调用 `cad_apply_dwt_profile(dry_run=false)`
6. `cad_validate_dwt_profile` 逐项核验；不以 `cad_check_drawing` 替代
7. 保持图纸打开，由用户在 AutoCAD 中手动“另存为 DWT”；不存在 `cad_save_as_dwt`

正式应用只支持 `acad`，整个配置应用共用一个撤销组，不自动保存当前 DWG/DWT。预览后的配置、目标图纸、图纸快照或保护选项一旦变化，旧确认令牌失效，必须重新预览。已有图层只提交预览计划中列出的差异字段；相同值不调用 COM setter。若请求实际改变当前图层的冻结状态，必须在写入任何图层属性前返回冲突。文字样式 `model_height=0` 表示可变字高，不得据此写入 `TEXTSIZE=0`；设为当前样式时保留图纸原有默认字高。尺寸小数分隔符只接受 `.` 或 `,`；AutoCAD ActiveX 使用单字符字符串并立即读回，DXF 底层继续保存整数字符编码。

### 环境工程语义工作流

适用于包含多个构筑物、管网关系、纵断面、标注、材料表、图纸目录或正式图框的任务。推荐顺序：

1. `cad_health(reconnect=false)`
2. 用 `cad_get_drawing_settings`、`cad_list_layers_detailed`、`cad_list_textstyles`、`cad_list_dimstyles` 建立只读基线
3. `cad_load_drafting_profile`、`cad_load_environment_rule_set`
4. 若使用既有图框：加载并校验模板清单
5. `cad_validate_environment_model`
6. `cad_preview_environment_drawing`
7. 评估视图比例和图框可用区域
8. AutoCAD 模式下从只读模板克隆工作副本
9. `cad_apply_environment_drawing(dry_run=true)`，确认后再正式应用
10. 创建布局、插入图框、填写字段、创建并锁定视口、配置打印
11. `cad_check_drawing`、保存，并按需发布 PDF

详细调用契约见 [tool-contracts.md](references/tool-contracts.md)，流程和异常分支见 [workflows.md](references/workflows.md)。

### 直接图元工作流

仅适用于局部补画、简单几何或精确单对象修改：新建或打开图纸，设置图层和样式，绘制或编辑实体，检查范围和视口适配，查询实体，执行 `cad_check_drawing`，最后保存。`cad_open_drawing` 可用受控图纸目录内的文件名，或现有本机图纸的盘符绝对路径；目录外图纸的保存位置须先与用户核对。

需要原生注释性块时，仅在 `acad` 模式调用 `cad_make_block(annotative=true)`；需要 ATTDEF 属性时调用 `cad_create_or_update_attribute_block(annotative=true)`。两种调用均检查 `verified` 与 `readback`。DXF 模式不支持此开关；不得用普通块加手动比例冒充。

### DWG 块审查与实心填充

审查指定 DWG 块时使用 `acad` 模式和显式 `*_manifest.json` 规格卡，调用 `cad_audit_block_definition`。它只读打开源文件，输出递归实体树、几何分析、连接点、对称性和独立指纹，并逐项返回“通过、偏差、无法验证”。规格卡未声明或运行时无法读取的值保持无法验证，不补默认值。

封闭边界实心填充使用 `cad_fill_closed_boundary_solid`。必须显式指定边界来源、图层、关联性、边界保留策略和 `dry_run`；先预览，再正式创建 `SOLID` HATCH 并核对回读结果。详细契约和规格卡结构见参考文档。

## 运行时安全契约

当前运行时版本为 `1.5.3`，注册 101 个 `cad_*` 工具。每次会话先调用 `cad_health(reconnect=false)`，核对模式、连接、文档状态、服务端与 Python 版本、后端能力和最近错误编号；用户明确 CAD 已打开且要求操作图纸时，可使用 `reconnect=true` 连接已有实例，无须额外确认。CAD 运行状态未知时先向用户确认。

- 所有公开工具输入拒绝未知字段，数值字段拒绝 `NaN`、正无穷和负无穷；参数校验失败视为未执行写入，不得猜测参数后继续。
- 工具函数捕获的运行错误返回 `ok: false`、`error` 和 `error_detail`；读取其中的 `code`、`operation`、`retryable`、`hint` 和 `request_id`。
- 只有 `retryable=true` 且重新健康检查确认当前文件状态正常时，才能考虑使用相同参数重试。
- AutoCAD 图元和布局写入必须先建立撤销组；建立失败时不得开始写入，回滚失败时按错误编号报告并停止。
- DXF 语义计划正式应用前建立内存快照，执行失败时恢复快照；快照和撤销组都不能替代文件备份。
- `cad_preview_dwt_profile` 只读且零写入；正式应用需要与配置哈希、目标图纸、图纸快照和选项绑定的确认令牌。
- PC3、纸张、CTB/STB、字体和 DWG 块库必须按本机实际资源核验；缺失或无法证实时进入冲突、未支持项或 `unverified_settings`，不得补默认值。
- 日志默认位于 `~/.cad-mcp/logs/cad_mcp.log`，可用 `CAD_LOG_DIR` 覆盖；只用于按错误编号排查，不记录完整工程模型或绘图参数。
- Codex 桌面端的用户即使要求进入 `acad` 模式，也不等于授权 MCP 冷启动 AutoCAD；AutoCAD 未运行时必须先要求用户手动启动。

## 不可违反的规则

- 需要 DWG 图框、布局、视口或 AutoCAD 当前窗口时必须使用 `acad` 模式；DXF 不支持时不得静默降级。
- 固定图框是纸空间成品，不得缩放。优先调整视口比例、视图范围、布局数量或图幅；不得改变工程模型实际尺寸。
- `cad_apply_environment_drawing` 默认先使用 `dry_run=true`。通常使用 `update_managed`；`replace_scope` 可能覆盖人工修改，必须展示范围并获得用户确认。
- 模板源文件只读，必须校验源文件哈希，并从模板克隆工作副本。
- 工具不支持目标能力时，明确报告缺失能力并停止该步骤，不得用近似图元或另一种输出冒充完成。
- 当前 ActiveX 不能可靠自动生成 CTB 或无对话框转换 CTB/STB 时，正式调用返回 `UNSUPPORTED_OPERATION`，不创建空文件；DXF 专属边界返回 `UNSUPPORTED_MODE`。
- 关联坐标/标高必须使用可读回的 AutoCAD 对象字段；无法证明关联关系时回滚，不得用静态 TEXT 冒充。需要正号或特殊零格式但 ActiveX 无法验证条件字段时，应明确停止或由用户选择非关联静态模式。
- AutoCAD COM 操作必须串行执行；不要并发修改同一活动文档，也不得绕过撤销组继续写入。
- 任一关键步骤失败时，停止后续写操作，报告失败工具、错误编号、原因、已完成动作、回滚状态和下一步建议。
- 保存前必须执行 `cad_check_drawing`，如实反馈警告、未支持项和人工修改冲突。

## 用户确认点

查询、健康检查、模型/DWT 校验和预览通常可直接执行。以下操作应在展示范围或试运行结果后获得确认：正式应用 DWT、删除图层/布局、覆盖同名块或文件、覆盖人工修改、保存到用户指定位置以及发布正式 PDF。

## 完成判定

普通绘图只有在目标文件已保存、关键实体可查询且校核结果已检查后，才可报告完成。DWT 配置阶段以正式应用未报错、`cad_validate_dwt_profile` 结果已核对且图纸保持打开为 MCP 完成点；最终 DWT 文件由用户手动另存。若用户要求保留窗口，禁止擅自关闭 AutoCAD 或图纸。

## 参考资料

- [工具契约](references/tool-contracts.md)
- [工作流与异常处理](references/workflows.md)
- [数据结构与配置](references/data-schema.md)
- [最小模型示例](examples/environment-model.json)
- [模板清单示例](examples/template-manifest.json)
- [块规格卡示例](examples/block-library_manifest.json)
- [DWT Profile 2.0 结构示例](examples/dwt-profile-v2.schema-example.json)
