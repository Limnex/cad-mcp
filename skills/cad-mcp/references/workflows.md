# CAD MCP 工作流与异常处理

## AutoCAD 会话前置检查

在 Codex 桌面端进入任何 `acad` 工作流前，先确认 AutoCAD 已由用户手动启动并完成界面加载。作者实测中，从 AutoCAD 完全关闭的状态通过 Codex 桌面端调用 MCP 冷启动会导致 AutoCAD 崩溃；手动启动后再使用 MCP 打开图纸、新建图纸和执行其他操作可以继续工作。

1. 调用 `cad_health(reconnect=false)`，不得用该步骤启动或重连 AutoCAD。
2. 用户明确 CAD 已打开且要求操作图纸时，可直接连接已有实例，并按路径复用/打开图纸或新建图纸；不再单独索要连接授权。`connected=false` 不等于 CAD 未运行。
3. CAD 是否打开未知时先向用户确认；已知未打开时请用户手动启动。后端找不到现有实例或连接失败时停止，不启动或关闭任何实例。
4. 如果 AutoCAD 仍然崩溃，停止后续写操作，记录阶段、错误编号和日志，不自动重复启动。

作者使用 `cc-haha` 时暂未复现同一问题，但该结果不构成对其他客户端、电脑或 AutoCAD 版本的保证。

## 通用 DWT 工作流

健康检查（`reconnect=false`） → 切换 `acad` → 读取图纸/图层/样式/布局/页面设置/打印资源基线 → 加载 DraftingProfile 2.0 → 预览 → 核对冲突与未支持项 → 用户确认 → 用同一参数和确认令牌正式应用 → 逐项 DWT 校验 → 保持图纸打开 → 用户手动另存为 DWT。

推荐的关键调用顺序：

1. `cad_health`、`cad_get_drawing_settings`、`cad_list_layers_detailed`、`cad_list_textstyles`、`cad_list_dimstyles`
2. `cad_list_layouts_detailed`、`cad_get_plot_environment`、`cad_list_page_setups`
3. `cad_load_drafting_profile`
4. `cad_preview_dwt_profile`，或 `cad_apply_dwt_profile(dry_run=true)`
5. 展示目标图纸、配置哈希、变更清单、删除/覆盖范围、冲突和未支持项，取得用户确认
6. 在 30 分钟内用完全相同的配置、目标、`update_existing`、`preserve_unmanaged` 和返回的 `confirmation_token` 调用 `cad_apply_dwt_profile(dry_run=false)`
7. `cad_validate_dwt_profile`；如仍有 mismatch、missing 或 unverified，分别处理或如实交给用户判断

预览是“施工清单”，确认令牌像一次性封条：它同时绑定配置 SHA-256、目标图纸、图纸快照、保护选项和计划。预览后任何一项变化都会使封条失效，必须重新预览。正式应用共用一个 AutoCAD 撤销组，某一步失败就停止后续步骤并尝试整体回滚；回滚成功也不等于文件备份。

图层施工单只填写预览中真正变化的栏位，不得把整行属性重新抄写一遍。底层写入前再次比较当前值：相同值跳过；如果冻结状态确实要变化而目标正是当前图层，则在写入说明、颜色等任何属性前停止并返回冲突。

没有 `cad_save_as_dwt`。MCP 不自动保存当前 DWG/DWT，最终 DWT 由用户在 AutoCAD 中手动“另存为”。

## 语义模型工作流

健康检查（含模式、文档状态、能力和最近错误编号） → 读取图纸设置、详细图层和文字样式基线 → 加载制图配置和规则 → 校验模板 → 校验环境工程模型 → 预览 → 评估视图比例 → dry-run → 用户确认 → 正式应用 → 布局和出图 → 检查 → 保存 → 发布 PDF。

视图放不下时，依次考虑调整视口比例、视图范围、布局数量、拆分视图或图幅。不得缩放固定图框，也不得改变模型实际尺寸。

## 直接图元工作流

健康检查 → 新建或打开图纸 → 设置图层和样式 → 绘制或编辑 → 查询关键实体 → 检查几何和制图环境 → 保存 → 验证保存结果。

打开图纸时，受控目录内可传文件名；目录外只传现有本机盘符绝对路径，不传相对子路径或 UNC。目录外文件不会因“打开”而自动保存；若后续要显式保存，先核对将写回的原路径。acad 模式打开 DXF 后保存会生成同目录 DWG。新建与另存为仍只能写入受控目录。

所有基线查询均为只读。字段无法由当前后端证实时必须保留 `null` 并列入 `unverified_fields`，不得用默认值补齐。DraftingProfile 2.0 已接入专用预览、正式应用和逐项校验；完整应用限定 `acad`，DXF 遇到 AutoCAD 专属能力返回 `UNSUPPORTED_MODE`。

若建块需随注释比例显示，使用 `cad_make_block(annotative=true)` 并核对返回的原生标记回读；这仅限 `acad` 模式。DXF 不支持时停止该步骤，不把插块时手动比例当作注释性。

## 指定 DWG 块审查工作流

手动启动 AutoCAD → `cad_health(reconnect=false)` → 切换 `acad` → 核对 DWG 与 `*_manifest.json` 的绝对路径 → `cad_audit_block_definition` → 检查源文件哈希未变化 → 阅读递归实体树和派生分析 → 按“通过、偏差、无法验证”处理每一项。

规格卡像验收表：没有写在表里的目标不能由工具代填。代理实体、循环嵌套、实体过多、几何参数不可读或嵌套变换无法用于派生分析时，保留已读取证据并返回“无法验证”。不要把包围盒中心称作面积形心；面积形心不可计算时保持 `null`。

## 直接图元的登记、标注与图案填充

默认严格模式下，在创建图元前用 `cad_register_semantic_object` 登记真实对象；每次创建
提供 `semantic` 绑定。复制、阵列、插块、图框和视口同样执行此约束，不先画后补。
登记请求见[严格语义模式](../../../docs/configuration.md#严格语义模式)。

直径标注：核对当前样式、圆心、圆上一点和可选文字位置 → 确认写入 →
`cad_draw_diameter_dimension` → 核对 `diameter`、`dimstyle` 和实际实体 → 检查后保存。
该工具没有 `dry_run`，也不与现有圆建立自动更新关联。

图案填充：查询实际边界句柄 → `cad_draw_hatch(dry_run=true)` → 核对图案、面积和孔洞 →
确认后用相同参数及 `dry_run=false` → 检查 `verified`、`readback`、源边界保留和辅助边界
清理 → 查询关键实体 → `cad_check_drawing` → 保存。组合区域使用 `boundary_handles`
与内部 `seed_point`，必须 `associative=false`；边界不闭合时停止，不自动补线。
AutoCAD 非 SOLID 图案的预览不能证明图案可用。详细参数见[工具契约](tool-contracts.md#图案填充与组合闭合区域)。

## 实心填充工作流

健康检查 → 确认目标图纸和已登记对象 → `cad_fill_closed_boundary_solid(dry_run=true)` → 核对边界来源、图层、关联性和保留策略 → 用户确认实际写入 → 原参数改为 `dry_run=false` → 检查 `verified=true` 和完整 `readback` → 查询关键实体 → `cad_check_drawing` → 保存。

AutoCAD 正式写入必须在一个撤销组内完成。创建 Hatch 后立即附加外边界，再 Evaluate、REGEN 和回读；任何一步失败就删除本次对象并停止。不得用普通闭合多段线、宽线或实体面冒充 HATCH 成功。

## 受管更新策略

- `create_only`：只创建不存在的受管对象。
- `update_managed`：更新 MCP 管理且未被人工修改的对象，通常为默认选择。
- `replace_scope`：替换明确范围，可能覆盖人工修改；必须先展示对象范围、冲突和覆盖后果，并获得确认。

DWT profile 另用 `update_existing` 和 `preserve_unmanaged` 控制已有/未受管对象。默认 `update_existing=true, preserve_unmanaged=true`：允许把 profile 声明的对象更新到目标值，但保留配置外对象。具体删除仍需 profile 明确写出 `state: "absent"`，并经过空对象及保护对象检查。

DXF 语义计划的正式应用使用内存图档快照；AutoCAD 图元和布局写操作必须先建立撤销组并优先保存工作副本。撤销组建立失败时不得开始写入；回滚能力不是替代备份，回滚失败时必须停止并报告错误编号。

## 异常分支

- Codex 桌面端检测到 AutoCAD 未运行：不冷启动、不使用 `reconnect=true`，提示用户手动打开并等待加载完成。
- AutoCAD 在启动或连接阶段崩溃：停止写操作和自动重试，保留错误编号与日志，由用户重新手动启动后再检查状态。
- 健康检查失败：不进行写操作。繁忙时返回的缓存诊断不能当作 CAD 已恢复或空闲的证明。
- `WORKER_UNAVAILABLE`：工作进程失效，不重放失败请求。确认 CAD 已运行后重新连接并按完整路径绑定图纸，重新加载配置和预览；原 DXF 未保存内存状态不能自动恢复。
- `EXECUTION_UNKNOWN`：写入结果未知，优先于下文普通失败的回滚处理；不得声称已撤销、自动重试或继续写入、保存、关闭、退出和切模式。保留 CAD 状态，人工核对后重启 MCP；重连或重新绑定不能解除限制。
- `worker_cleanup=unverified`：清理未确认，停止新的执行请求，检查残留工作进程并重启 MCP。
- 模型校验失败：返回校验问题，要求修正模型后重试。
- 预览存在警告：列出警告，不把预览视为完成；涉及工程约束时暂停。
- 模板校验失败：不使用模板，不自行替代图框。
- DWT 预览含冲突或未支持项：不发确认令牌，不进入正式应用。
- DWT 确认令牌缺失、过期或状态不匹配：返回 `CONFIRMATION_REQUIRED` 或 `CONFLICT`，重新预览。
- PC3、介质、CTB/STB、字体或块库资源缺失：停止对应写入；不要用“常见默认名称”替代。
- CTB 创建或 CTB/STB 转换无法由 ActiveX 可靠完成：正式调用返回 `UNSUPPORTED_OPERATION`，保持图纸和文件不变，交由用户使用 AutoCAD 手工流程。
- 关联字段无法写入并读回：回滚，不生成静态 TEXT 冒充；需要时由用户明确选择 `associative=false`。
- DWG 块规格卡缺字段或实际证据不可读：对应项返回“无法验证”，不自动补默认值，也不因此改写源 DWG。
- 实心填充回读不一致：删除本次 Hatch 和临时边界，触发撤销组回滚，不保存图纸。
- dry-run 与正式结果不一致：停止，保留当前文件状态并报告差异。
- 保存或发布失败：不报告完成，验证文件状态并给出恢复建议。
- 结构化错误 `retryable=false`：修正输入、前置状态或环境后再调用，不原样重试。
- 结构化错误 `retryable=true`：先用 `cad_health(reconnect=false)` 核实文档和空闲状态，再决定是否使用相同参数重试。
- `recent_error_id` 变化：用该编号关联 `logs/cad_mcp.log`，不要在日志中寻找或写入完整工程参数。

## 多图纸与写入核验补充

DXF：列出会话图纸 → 按 drawing_id 激活目标 → 查询身份和几何 → 编辑 → 检查 →
按目标图纸显式保存。打开其他图纸保留各自未保存修改，同路径复用内存；关闭时明确
save，切换后端前须关闭全部当前会话图纸。进程失效不能自动恢复未保存 DXF。

AutoCAD：按完整路径打开或复用已打开的目标并绑定，再核对图纸身份。新建图纸不保存
原图，也不自动落盘新DWG。仅切换 CAD 标签不改变 MCP 绑定。

写入后出现 COM 超时 → 等待空闲 → 对受支持的新增实体/变量/属性独立回读 → 证实成功
则正常返回且不重放；部分执行或无法证实则保留图纸、启用写入保护并报告。工作进程失联
不能用局部回读流程假装已经恢复。回滚须核验几何、身份和适用的设置/视图，未核实则停止。

圆角/倒角在严格模式要求选定图元归属一致，并给新增实体独立身份；不以用户看见轮廓
正确替代身份检查。纯文本成功响应与 JSON 的区别、实心填充 null 组合见[完整工具参考](../../../docs/api-reference.md)。
文件核验和模板比例计算可在没有 CAD 时先做，但正式布局/打印仍按 acad 工作流验收。
