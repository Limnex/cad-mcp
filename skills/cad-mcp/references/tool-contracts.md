# CAD MCP 工具契约

本文记录调用约束。当前运行时版本为 `1.5.3`，注册 105 个 `cad_*` 工具。具体参数名和返回字段以运行时 MCP 工具定义为准；若运行时定义与本文不一致，应以运行时定义为准并报告差异。

## 调用前

- `cad_health`：读取连接状态、当前模式、活动文档、服务端/Python 版本、后端能力和最近错误编号。默认 `reconnect=false`，不主动连接 AutoCAD；`reconnect=true` 只连接已运行的实例，不启动新实例。stdio 服务执行繁忙或尚无工作进程时，默认健康检查可由主进程直接返回缓存诊断；`file`、`last_known_document_open` 不能证明 CAD 当前可用，`ok: true` 只表示诊断成功返回。
- `cad_open_drawing(filename)`：继续接受受控图纸目录内的单个文件名；另接受现有普通文件的本机盘符绝对路径，DXF 模式仅 `.dxf`，acad 模式 `.dwg/.dxf`。不接受 UNC 网络路径、相对子路径或 `..` 回退；路径和扩展名在 AutoCAD 连接前校验。此放宽只适用于打开，不改变新建与另存为的目录限制。打开后不会自动保存；显式保存外部 DWG/DXF 会写回原路径，acad 模式打开 DXF 后保存会在同目录生成 DWG，若目标已存在则拒绝覆盖。
- `cad_switch_mode`：在执行依赖模式的操作前切换到 `dxf` 或 `acad`。
- `cad_get_drawing_info`、`cad_get_drafting_environment`：读取当前图纸和制图环境，不应替代健康检查。
- `cad_get_drawing_settings`：读取 INSUNITS、MEASUREMENT、LUNITS、LUPREC、AUNITS、AUPREC、DIMASSOC、MIRRTEXT、LWDISPLAY、PSTYLEMODE、当前层和当前布局。无法读取的字段必须进入 `unverified_fields`。
- `cad_list_layers_detailed`：只读返回图层颜色、线型、线宽、打印、描述、锁定、冻结、当前状态及数据库实体数量。
- `cad_list_textstyles`：只读返回字体、大字体、样式高度、宽度、倾斜角和当前状态；`model_height=0` 表示可变字高。当前后端无法证实注释性时返回 `annotative: null` 并列入 `unverified_fields`。

## DWT 基线与本机资源

- `cad_list_dimstyles`：返回文字样式、文字高度、总比例、`DIMLFAC`、精度、`DIMTSZ`、`DIMASZ`、文字间隙、零抑制、小数分隔符、界线参数、基线间距、颜色、线宽和当前状态。字段无法可靠读取时进入 `unverified_fields`。
- `cad_list_layouts_detailed`：返回 Model/图纸空间布局、设备、介质、旋转、样式表、打印范围、纸张单位、比例、居中、按线宽/样式打印和视口数。
- `cad_get_plot_environment`：仅在 `acad` 模式从当前 AutoCAD 读取 CTB/STB 模式、打印样式搜索目录、实际样式表、设备和设备介质；不得假设 `monochrome.ctb`、某个 PC3 或纸张必定存在。
- `cad_list_page_setups`：只读返回所有命名页面设置及完整打印参数。

## DWT 配置的三步调用

1. `cad_preview_dwt_profile(profile_path, target_drawing, update_existing=true, preserve_unmanaged=true)`：绝对零写入，返回图纸设置变化、图层增改名删、文字/尺寸样式、线型比例、打印模式、布局、页面设置、块库动作、冲突、未支持项、`plan_hash`、`snapshot_hash`、是否需确认及可用的 `confirmation_token`。
2. 用户核对计划。存在 `conflicts` 或 `unsupported_items` 时停止；不要尝试正式应用。
3. `cad_apply_dwt_profile(..., dry_run=false, confirmation_token=...)`：只支持 `acad`，必须使用同一配置、目标图纸和保护选项。令牌有效期 30 分钟，并绑定配置 SHA-256、目标图纸、图纸快照及计划哈希；任一状态变化都要求重新预览。已有图层仅提交预览 `changes` 中的差异字段，不用完整目标对象覆盖。整个应用只建立一个撤销组，失败时停止后续操作并返回回滚状态，不自动保存。

`cad_apply_dwt_profile(dry_run=true)` 与专用 preview 等价地先生成计划；首次调用必须是 dry-run。`cad_validate_dwt_profile` 独立逐项比较当前图纸与配置，返回 `pass`、`errors`、`warnings`、`matched_settings`、`mismatched_settings`、`missing_settings`、`extra_unmanaged_settings` 和 `unverified_settings`。它不能被 `cad_check_drawing` 代替。

`target_drawing` 必须与当前活动图纸一致，可使用完整路径；未保存图纸按后端返回的稳定标识核对。DWT 配置加载只接受现有 UTF-8 JSON 的绝对路径，并返回规范化绝对路径和 SHA-256。

## DWT 写入与保护工具

- `cad_set_drawing_settings`：只开放毫米、公制、十进制单位以及精度、关联尺寸、镜像文字、线宽显示等明确字段；返回 `before`、`after`、`changed`、`unchanged`，AutoCAD 写入后逐项读回。
- `cad_set_layer_props`：只处理调用方明确提供且与当前值不同的属性，相同值不触发 COM setter；实际改变当前图层的冻结状态时，在任何图层属性写入前返回 `CONFLICT`。成功写入的字段必须逐项读回。
- `cad_create_or_update_textstyle`：`model_height` 接受大于等于 0 的有限数值。0 表示可变字高；即使该样式设为当前，也保留已有 `TEXTSIZE`，绝不把 0 写入该系统变量。正值表示固定字高，仅在默认字高确有变化时同步正值并读回。
- `cad_create_or_update_dimstyle`：`decimal_separator` 只接受 `.` 或 `,`。AutoCAD 后端必须向 `DIMDSEP` 传单字符字符串并立即读回校验类型和值；DXF 后端继续把字符转换为整数编码写入 DXF 字段，对外读回仍返回字符。
- `cad_convert_plot_style_mode`：`dry_run=true` 只比较 `target_mode` 与 `expected_current_mode`；期望模式不符即停止。当前 ActiveX 没有可验证的无对话框 CTB/STB 转换路径，正式转换返回 `UNSUPPORTED_OPERATION` 且不改图。
- `cad_create_or_update_page_setup`：创建或更新命名页面设置；设备、纸张或样式表必须出现在 AutoCAD 实际资源中，`replace=false` 不覆盖已有不同设置，完成后返回完整读回值。
- `cad_rename_layer`：禁止 0、Defpoints 和重名，写后读回；原图元的图层引用由 AutoCAD/ezdxf 随命名保留。
- `cad_delete_layer`：默认 `dry_run=true`、`require_empty=true`；禁止 0、Defpoints、当前层。只有显式 `require_empty=false, allow_nonempty=true` 才处理非空层，并返回逐层删除、跳过或失败原因。
- `cad_rename_layout`：禁止 Model、空名和重名，写后读回。
- `cad_delete_layout`：默认 `dry_run=true`、`require_empty=true`；禁止 Model 和最后一个图纸空间布局，返回逐项处理结果。
- `cad_create_or_update_ctb`：先确认目标目录正是 AutoCAD 实际打印样式目录，并检查覆盖策略。`dry_run=true` 返回计划；由于 ActiveX 不能可靠创建/编辑 CTB，正式调用返回 `UNSUPPORTED_OPERATION`，不创建空文件，也不伪造哈希。

DXF 可读写图纸设置、图层、尺寸样式和基础属性块，但不提供真实 CTB/STB 转换、命名页面设置、DWG 块库或关联 AutoCAD 字段。相关正式调用必须返回 `UNSUPPORTED_MODE`；DWT 配置正式应用限定 `acad`。

## 已有实体属性读取与编辑

- `cad_get_entity_properties(handle)`：只读，句柄为现有查询工具返回的十进制字符串。返回 `type`、`properties`、`true_color` 与 `editable_properties`。仅支持模型空间图形实体，拒绝块定义内部、纸空间及非图形对象；句柄不存在返回 `NOT_FOUND`。
- `cad_edit_entity_properties(handle, properties, dry_run=true)`：白名单部分更新，省略字段表示保留，拒绝空对象、`null`、未知字段和非有限数值。`dry_run=true` 零写入，返回 `changes` 中逐字段的 `before/after`；顶层 `before/after` 是实际实体快照，因此预览时二者相同。`would_write` 表示是否存在差异；正式应用且确有差异时 `applied=true, verified=true`。无差异不执行 setter；`applied=false, verified=true` 表示当前值已满足请求。

| 范围 | properties 字段 | 单位与约束 |
|---|---|---|
| 通用外观 | `layer`, `color`, `linetype`, `lineweight`, `linetype_scale` | 图层及线型须已存在，不自动创建或加载；源和目标图层不得锁定 |
| 索引色 | `color` | 整数 0–256，0=ByBlock，256=ByLayer；显式设置会清除已有真彩色，原 RGB 值在 `true_color` 返回 |
| 线宽 | `lineweight` | 标准 CAD 整数线宽，单位 1/100 mm；-1=ByLayer，-2=ByBlock，-3=Default |
| 线型比例 | `linetype_scale` | 大于 0 |
| TEXT / MTEXT | `text`, `height`, `rotation` | 内容保留首尾空格；字高 mm 且大于 0；旋转角为度，归一到 [0,360)；TEXT 禁止换行，MTEXT 支持原生格式码 |
| LINE | `start`, `end` | WCS 三维坐标 `[x,y,z]`，mm；端点不能重合 |
| CIRCLE | `center`, `radius` | 圆心为 WCS `[x,y,z]`，mm；半径大于 0 |

不适用当前实体的字段返回 `UNSUPPORTED_OPERATION`。已有或新输入文字含关联字段表达式时拒绝覆盖。尺寸测量值、文字样式、三维实体及下述范围外的专有属性不通过本工具编辑；其他图形实体只提供通用外观属性。

### 平面实体扩展

新增几何编辑仅支持法向 +Z 的 XY 平面，不投影或重建倾斜实体。所有新增位置向量为 `[x,y]`，保留原标高；块的 Z 比例保持不变。返回只读 `elevation`，块另返回只读 `z_scale`。原 LINE/CIRCLE 三维字段兼容性保留；CIRCLE 的 `center` 也可用 XY，沿用原 Z 值。

| 实体 | 可编辑专有字段 | 约束 |
|---|---|---|
| ARC | `center`, `radius`, `start_angle`, `end_angle` | 圆心 XY，半径正值，角度为度且起止不能重合 |
| LWPOLYLINE | `vertices`, `closed`, `constant_width` | 顶点为 `{x,y,bulge,start_width,end_width}`；同顶点数时省略的弯曲和宽度保留；开放至少 2 点、闭合至少 3 点；相邻点不重合 |
| INSERT | `position`, `rotation`, `x_scale`, `y_scale`, `block_attributes`, `block_texts` | XY 插入点，旋转为度，正向 XY 比例；变换时同步附属属性文字 |
| HATCH | `pattern`, `pattern_scale`, `pattern_angle` | 预定义图案、正比例（最大 1000）、角度为度；保留原环路、孔洞、关联性；SOLID 比例/角度归为 1/0 |
| ELLIPSE | `center`, `major_axis`, `radius_ratio`, `start_parameter`, `end_parameter` | XY 圆心，长轴半径向量 `[dx,dy]` 非零；短长轴比 `(0,1]`；起止参数为弧度，整椭圆用 `0` 和 `2π` |
| POINT | `position` | XY 坐标，保留原标高 |

多段线最多 1000 顶点。改变含圆弧段或宽度的多段线顶点数时，必须逐点显式指定 `bulge/start_width/end_width`，防止默默丢失。返回的 `constant_width=null` 表示各段实际宽度不同；这只是读取状态，更新时仍禁止传 null。常量线宽与显式非零逐段宽度不可在同一请求混用。DXF 关联边界不能自动同步填充时拒绝改形；渐变填充不转换为普通填充。

### 块内文字与数值

`properties.block_attributes` 返回可定位的属性标签及当前内容；`properties.block_texts` 返回固定 TEXT/MTEXT 的路径及内容。路径由块内对象序号组成，嵌套时以 `/` 分隔，例如 `"2/1"`，必须使用刚读取的路径，不按文字相似度猜测对象。

更新属性标签不区分大小写；未知或歧义标签、未知文字路径及关联字段在写入前拒绝。值必须为字符串；使用 `"24.00"` 等保存数值格式，不执行公式或改变实际几何尺寸。

```json
{"params":{"handle":"123","properties":{"block_attributes":{"FLOW":"24.00"},"block_texts":{"1":"更新后的设备名称"}},"dry_run":true}}
```

固定文字采用按需复制块定义：仅复制选中实例及被修改的嵌套路径，不覆盖共享原定义，不分解块，选中实例句柄保持不变。`after.block_name` 显示新的独立块名；再次应用相同值不会继续复制。正式失败时回滚新定义和实例绑定。

块内容扫描上限 1000 对象、嵌套深度 8，属性/更新项各最多 256；循环引用、动态/匿名块、外部参照，以及无法保留扩展数据或关联依赖的固定文字克隆会被拒绝。含尺寸、填充或未知对象的块暂不复制固定文字。仅允许统一 XYZ 缩放的块若需要改变 XY 比例，因本工具保留 Z 而拒绝。已有镜像/零比例块、带多行属性的几何变换也明确拒绝；简单内容更新按可验证能力处理。

正式应用先通过全部预检，再建立 DXF 内存快照或 AutoCAD 撤销组。写后回读请求值；不一致或写入失败时尝试回滚，通过统一错误响应的 `error_detail.details.rollback_status` 报告结果；AutoCAD 回滚还会核对实体属性。失败或回滚无法核实时停止，不自动保存图纸。

例如，先只预览修改文字内容和字高：

```json
{"params":{"handle":"123","properties":{"text":"更新后的说明","height":3.5},"dry_run":true}}
```

核对 `changes` 后，以相同句柄和 `properties`、`dry_run=false` 正式应用。修改前实体可能已变化，正式调用会重新预检并返回本次实际差异，预览不锁定图纸。

## 属性块、字段和独立块库

- `cad_create_or_update_attribute_block`：由基础点、已有实体句柄和 ATTDEF 定义创建块；`replace=false` 不覆盖同名定义，属性标签必须唯一。`annotative` 默认 `false` 保持旧行为；设为 `true` 时仅支持 `acad`，复制源图元后使用 AutoCAD 原生命令创建注释性属性块，读回注释性标记、ATTDEF 属性和临时插入引用的可编辑标签；任一验证失败触发撤销组回滚。DXF 模式在写入前返回 `UNSUPPORTED_MODE`。
- `cad_set_block_attributes`：校验块引用与标签，返回 `updated`、`missing_tags`、`unchanged`。
- `cad_bind_block_attribute_field`：当前只接受 `source_type=object`，按源句柄和经过验证的对象属性写入 AutoCAD 对象字段；可用 `linear_scale_factor=0.001` 将毫米显示为米，并可设置精度、前后缀和 REGEN 更新。字段代码无法读回时整体失败，不退化为静态文字。
- `cad_inspect_block_library`：只读打开绝对 DWG 路径，返回文件 SHA-256、单位、块名、基点、实体数、属性标签、依赖和警告，并关闭源图纸且不保存。
- `cad_audit_block_definition`：只支持 `acad`。调用方必须提供现有 DWG、指定块名和显式 `*_manifest.json` 规格卡的绝对路径。源 DWG 路径与 SHA-256 必须和规格卡一致；工具只读打开源图纸，递归输出实体树、可读取的几何参数、包围盒、包围盒中心、对称性、连接点、文字、ATTDEF、图层、线型、线宽、嵌套引用及每块独立指纹。代理实体、循环引用、安全上限或字段读取失败必须列入未验证，不能用近似值补齐。
- `cad_import_block_definitions`：源文件必须是现有绝对 DWG，读取哈希与单位后只复制指定块；默认不覆盖同名块。
- `cad_export_block_library`：默认 `dry_run=true` 且不覆盖；正式导出用临时块引用和 WBLOCK 生成独立 DWG，验证非空文件和 SHA-256，再按替换策略落盘，不保存当前图纸。
- `cad_insert_coordinate_label`、`cad_insert_elevation_label`：毫米模型默认按 `0.001` 显示为米。`associative=true` 必须落为能在 REGEN 后更新的字段；DXF 只能在显式 `associative=false` 时生成静态属性块。关联标高需要正号或特殊零格式、但当前 ActiveX 无法验证条件字段时，返回明确未支持，不以静态文字冒充。

块规格卡中的每个 `checks[]` 使用 JSON Pointer 指向审查结果，并显式给出比较运算、期望值和必要容差。逐项状态固定为：实际证据满足规格时“通过”；可读取但不满足时“偏差”；规格缺失、实际不可读或证据不完整时“无法验证”。汇总时偏差优先，其次是无法验证；只有所有声明项都通过时总状态才是通过。指纹算法为 `cad-mcp:block-geometry:v1`，剔除句柄、路径等易变信息，按规格卡声明的量化精度规范化并排序实体；实体树不完整时指纹仍可供诊断，但 `complete=false`，不能作为通过证据。

## 封闭边界实心填充

- `cad_fill_closed_boundary_solid`：DXF 和 AutoCAD 均支持。`boundary_handle` 与 `points` 必须且只能提供一个，`layer`、`associative`、`keep_boundary`、`dry_run` 都必须显式提供；不自动填充任何默认策略。
- 首次调用应使用 `dry_run=true`，只验证边界闭合、面积、是否自交、保留策略和模式能力，保证零写入。
- 正式调用创建 `SOLID` HATCH 后立即回读实体类型、图案名、关联性、图层、边界循环数和包围盒；AutoCAD 另回读面积。任一关键字段不一致时删除本次创建对象并触发撤销组回滚。
- 使用现有边界句柄时强制 `keep_boundary=true`，不会删除用户边界；显式点创建关联填充时也必须保留边界。DXF 当前对已有曲线边界支持闭合直线多段线和圆，含 bulge 的多段线明确停止，不把折线近似当作圆弧。

## 扩展的原有接口

- `cad_make_block`：`annotative` 默认 `false` 保持旧行为。`true` 仅支持 `acad`，使用 AutoCAD 原生命令建块并回读注释性标记、实体数及源实体移除状态；任一回读失败则报错并触发撤销组回滚。DXF 模式在写入前返回 `UNSUPPORTED_MODE`。
- `cad_create_layer` 增加 `plot`、`description`、可选 `locked`、`transparency`；`cad_set_layer_props` 另增加可选 `frozen`。后端无法可靠设置或读回某项时明确报错或列入未验证项。
- `cad_create_or_update_dimstyle` 保留 `linear_scale_factor`/`DIMLFAC` 和 `oblique_stroke_size`/`DIMTSZ`，增加前导/尾零抑制、小数分隔符、界线偏移/伸出、基线间距、三类颜色及两类线宽；`precision=2` 且不抑制尾零时按两位格式校验，例如 `12.50`。
- `cad_configure_layout_plot` 增加打印范围、纸张单位、标准/自定义/适合比例、居中、按线宽/样式打印及命名页面设置，并返回最终完整读回值。
- `cad_insert_block` 增加外部 `source_path`、属性值、图层、XYZ 比例和字段绑定；默认不覆盖已有定义，未知属性标签必须明确返回。

## 语义工作流工具

- `cad_load_drafting_profile`：兼容加载 1.0 环境制图配置和严格的 2.0 通用 DWT 配置；返回绝对路径、SHA-256 和 schema 版本。2.0 中的外部 DWG 块库在加载时校验路径与哈希，字体、CTB 和 PC3 留给后端按实际安装资源核验。
- `cad_load_environment_rule_set`：加载外部校核规则。
- `cad_validate_environment_model`：检查模型结构、引用关系和工程约束；失败后不得直接绘图。
- `cad_preview_environment_drawing`：只生成操作计划、对象统计和警告。
- `cad_apply_environment_drawing`：正式修改工具。先 `dry_run=true`，再根据用户确认使用 `dry_run=false`。
- `cad_list_managed_entities`：检查受管对象、语义 ID、操作哈希和状态。

## 模板、布局和发布

模板、布局、视口和正式打印工具仅适用于 `acad` 模式：`cad_clone_template_drawing`、`cad_inspect_template_drawing`、`cad_load_template_manifest`、`cad_verify_template_manifest`、`cad_evaluate_template_sheet_scales`、`cad_create_layout`、`cad_insert_template_sheet`、`cad_fill_template_fields`、`cad_create_viewport`、`cad_configure_layout_plot`、`cad_publish_layout_pdf`。

模板哈希不匹配、字段锚点缺失、可用区域不足或发布失败时，应停止当前步骤，不得自行重画或替换模板。

## 查询、检查和保存

大图查询使用分页和过滤条件。修改后依次查询关键实体、执行 `cad_check_drawing`、`cad_save_drawing` 或 `cad_save_as`，再验证目标文件存在且可重新打开。

## 输入与返回

- 所有工具输入拒绝未知字段，数值字段拒绝 `NaN`、正无穷和负无穷；参数校验失败时不得猜测字段或继续写入。
- 成功响应继续使用原有 JSON 字段，并包含 `ok: true`。
- 工具函数捕获的运行错误返回 `ok: false`、`error` 和 `error_detail`。`error_detail` 包含 `code`、`operation`、`retryable`、`hint`、`request_id`。
- MCP SDK 在调用工具前发现的参数结构错误可能表现为协议级校验错误，而不是工具函数的 `error_detail`；两种情况都视为未执行写入。

## 尺寸终止符

- `cad_create_or_update_dimstyle` 的 `arrow_size` 对应 `DIMASZ`，控制箭头大小。
- `oblique_stroke_size` 对应 `DIMTSZ`，单位为图纸毫米：`0` 使用箭头，正数使用斜短线；终止符、文字等标注外观尺寸仍由 `DIMSCALE` 统一缩放。
- `oblique_stroke_size` 非零时，`arrow_size` 仍会保存在样式中，但不控制当前斜短线的显示大小。
- 需要区分普通线性尺寸与半径、直径、角度尺寸时，应建立不同的命名样式；具体名称、数值和采用的标准版本由外置制图配置决定，不写死在 MCP 中。
- `cad_get_drafting_environment` 会返回当前尺寸样式的 `oblique_stroke_size` 和 `uses_oblique_stroke`，用于检查实际活动样式。

## 尺寸测量单位换算

- `cad_create_or_update_dimstyle` 的 `linear_scale_factor` 对应 `DIMLFAC`，默认值为 `1.0`；传入 `0.001` 可将以毫米建模的线性尺寸显示为米。
- 该系数只换算尺寸测量文字，不缩放模型实体，也不替代控制标注外观比例的 `DIMSCALE`。
- `DIMLFAC` 会作用于线性、半径、直径和坐标尺寸，不作用于角度尺寸；通用接口只接受正数，避免模型空间与图纸空间采用不同换算规则。
- 换算结果的小数位数仍由 `precision`（`DIMDEC`）控制；`linear_scale_factor` 不会自动添加 `m` 等单位后缀。
- `cad_get_drafting_environment` 会返回当前尺寸样式的 `linear_scale_factor`，用于核对实际活动样式。

## 失败处理

错误报告至少包含：失败工具、输入对象或文件、错误原文或 `request_id`、已完成的写操作、是否可以回滚、建议的下一步。仅当 `retryable=true` 时才可在确认当前文件状态后重试；不要把工具未返回的结果当作成功。

stdio 服务的工作进程超时和崩溃另遵循以下约束，不能套用普通工具错误的回滚承诺：

- `WORKER_UNAVAILABLE`：工作进程失效或请求尚未发送便失败，`retryable=false`。确认 CAD 已运行后重新连接并按完整路径绑定图纸，重新加载配置和获取预览令牌；也可按用户要求新建图纸。失败请求不会自动重放，DXF 未保存内存状态不能自动恢复。
- `EXECUTION_UNKNOWN`：写请求发送后失去响应，`retryable=false`；`error_detail.details.rollback_status=not_verified` 表示无法证实回滚。禁止后续写入、保存、关闭、退出和切换模式，人工核对 CAD 后重启 MCP；重新连接或绑定图纸不能解除限制。
- `error_detail.details.worker_cleanup=unverified`：工作进程清理未确认，阻止新的执行请求；保留 CAD 状态，检查残留工作进程并重启 MCP。默认本地健康诊断仍可返回。
- 健康结果中的 `worker.running`、`pid`、`busy`、`generation` 反映 Python 工作进程状态；`rebind_required` 表示必须重新绑定图纸，`write_result_unknown` 表示写入结果未知。这些字段不证明 AutoCAD 已空闲或内部操作已中止。

运行日志默认写入 `~/.cad-mcp/logs/cad_mcp.log`，可用 `CAD_LOG_DIR` 覆盖；单文件最多 2 MB，保留 2 个轮换备份。日志只记录操作名、错误分类和错误编号，不应记录完整工程模型或绘图参数。

## 局部图像预览

`cad_preview_region` 支持 `dxf` / `acad`，预览当前已打开图纸的模型空间 WCS XY 范围。

| 参数 | 约束 |
| --- | --- |
| `x_min`, `y_min` | 必填，左下角，图纸单位 |
| `x_max`, `y_max` | 必填，右上角，各轴跨度至少 `1e-6`；坐标有限且绝对值不超过 `1e12` |
| `width`, `height` | PNG 像素宽高，整数 64–2048，默认 1200×800 |
| `background` | `white` / `black`，默认 `white` |
| `include_image` | 默认 `true`；`false` 只返回文字和已保存图片路径 |

调用示例：

```json
{"params":{"x_min":0,"y_min":0,"x_max":10000,"y_max":6000,"width":1200,"height":800,"background":"white"}}
```

成功返回原生 MCP `CallToolResult`：第一项 `TextContent` 内是 JSON，默认第二项是含完整 PNG
数据的 `ImageContent`，不把 Base64 当普通文字返回。精简网关也透传这些内容块。

文字字段：`ok`、`mode`、`image_path`（本机绝对路径）、`mime_type`、`width`、`height`、
`bounds`（四个范围坐标）、`coordinate_system=WCS_XY`、`space=model`、`background`、
`renderer=ezdxf_matplotlib`、`source`（`live_dxf` / `autocad_dxf_export`）、`byte_size`、
`created_at`、`warnings` 和 `image_included`。内联读取/编码失败或 PNG 超过 8 MiB 时，
仍以 `ok=true` 返回保存成功的路径，`image_included=false`，附 `image_error`。
图片生成或落盘失败则返回现有 `ok=false` 结构化错误，不报告预览成功。

存储根目录为解析后的实际 `log_dir` 的父目录下 `images/`，按本地日期分为 `YYYY-MM-DD/`；
文件名为 `preview_HHMMSS_微秒_随机标识.png`，使用独占创建，不覆盖已有文件，不自动删除。
路径文本和图片同时返回只提供两种读取途径；若客户端拒绝整个响应，路径也可能未收到，
可使用 `include_image=false` 重新获取纯文字响应（会生成新图片）。

DXF 使用内存文档；AutoCAD 使用 `Document.Export(..., "DXF", SelectionSet)` 临时导出，
结束删除自有选择集和临时文件，不调用 Save/SaveAs，不缩放视图，不修改打印配置。
输出保持几何比例、裁切超出矩形的内容，宽高比不符时留边。渲染不是 AutoCAD 原生截图：
字体、代理对象和外部参照可能不同，SHX 大字体可能以系统中文字体预览替换，外部栅格图像
仅绘制边框。具体限制与跳过对象在 `warnings` 说明。模型空间实体上限 100,000。

该工具会落盘图片，注解为 `readOnlyHint=false`、`destructiveHint=false`、
`idempotentHint=false`；遵循现有锁和工作进程超时策略，但不进入实体修改撤销组。
