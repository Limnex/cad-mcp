# 数据结构与配置

环境工程模型必须提供 `project_id`、`project_name`；`schema_version` 为 `1.0`，单位为 `mm`。设施使用 `facilities`，节点和管段放入 `network_systems[].nodes`、`network_systems[].segments`，表格使用 `schedules`，视图使用 `views`，通过稳定 ID 建立引用关系。顶层 `nodes`、`pipes`、`tables` 不受支持。工程参数应来自用户资料或已确认的计算结果。

制图配置负责单位、图层、文字样式、标注样式、线型和默认比例；规则集负责外部校核规则；模板清单负责源文件哈希、图块、边界、可用区域、字段锚点和表格区域。

最小模型示例见 [environment-model.json](../examples/environment-model.json)，模板清单示例见 [template-manifest.json](../examples/template-manifest.json)。示例仅展示结构，不代表任何工程参数或规范结论。

将最小模型 JSON 作为 `params.model` 传给 `cad_validate_environment_model`，确认 `ok=true`；再传给 `cad_preview_environment_drawing`（可设 `include_operations=true`），确认校核通过且操作数量大于零。这两步均可离线执行，不要求打开图纸，也不写入 CAD。仓库测试直接通过这两个接口校验示例。

## DraftingProfile 2.0

通用 DWT 配置固定包含 `schema_version`、`profile_id`、`name`、`standard_references`、`drawing_settings`、`layer_styles`、`text_styles`、`dimension_styles`、`linetype_settings`、`plot_policy`、`page_setups`、`layouts`、`block_library` 和 `metadata`。所有层级拒绝未知字段，枚举按精确值校验，数值拒绝 NaN 和正负无穷。

配置必须是现有 UTF-8 JSON 的完整绝对路径，加载结果返回配置绝对路径和 SHA-256。外部 DWG 块库条目必须包含绝对 `.dwg` 路径、预期 SHA-256 和非空块名列表；加载时文件缺失或哈希不符即失败。字体、CTB、PC3 和纸张属于本机 AutoCAD 资源，配置加载阶段只列出待核验引用，不把常见名称当作已存在。

### 顶层字段

| 字段 | 内容与约束 |
|:--|:--|
| `schema_version` | 固定为 `"2.0"` |
| `profile_id` / `name` | 非空、与具体工程无关的稳定标识和显示名 |
| `standard_references` | 外部标准/约定的文字引用；它们不是 MCP 自行作出的规范结论 |
| `drawing_settings` | 毫米、公制、十进制单位，线性/角度精度、关联尺寸、镜像文字、线宽显示，以及可选当前层/布局 |
| `layer_styles` | 以图层名为键；支持 present/absent、`rename_from`、颜色、线型、线宽、打印、描述、锁定、冻结、透明度和删除保护 |
| `text_styles` | 以样式名为键；字体/大字体、样式高度、宽度、倾斜角、注释性和当前状态；高度 0 表示可变字高 |
| `dimension_styles` | 以样式名为键；文字、总体比例、`DIMLFAC`、精度、`DIMTSZ`、`DIMASZ`、间距、零抑制、分隔符、颜色和线宽等完整字段；`decimal_separator` 只接受 `.` 或 `,` |
| `linetype_settings` | `ltscale`、`celtscale`、`msltscale`、`psltscale` |
| `plot_policy` | 目标 CTB/STB 模式、可选预期当前模式、所需样式表、首选设备和是否允许转换 |
| `page_setups` | 以页面设置名为键；设备、介质、旋转、样式表、打印范围、纸张单位、比例和打印开关 |
| `layouts` | 以布局名为键；present/absent、`rename_from`、命名页面设置和空布局保护 |
| `block_library` | 零个或多个独立 DWG 块库源；DWT 本体不承载块库文件来源 |
| `metadata` | 仅放 JSON 值的说明信息，不参与工程计算 |

`text_styles.*.model_height` 大于 0 时表示固定字高；等于 0 时表示文字高度由创建文字时决定。后者与图纸级 `TEXTSIZE` 是两个独立概念：把该样式设为当前时保留已有默认字高，不得从 0 推导或写入 `TEXTSIZE=0`。

### 固定单位策略

- 模型统一按毫米；`drawing_settings.insertion_units` 只接受 `millimeters`，`measurement_system` 只接受 `metric`。
- 总图尺寸若需显示为米，在对应尺寸样式使用 `linear_scale_factor: 0.001`；这只换算标注文字，不缩放模型。
- 小数分隔符在配置和接口中统一表示为单字符字符串；AutoCAD ActiveX 直接接收该字符串，DXF 后端仅在写入 DXF 字段时转换为整数字符编码。
- 坐标和标高工具的米制显示同样用 `0.001`，并由 `precision` 和尾零策略控制格式。
- CTB + 颜色随层 + 线宽随层属于外部 profile 的策略，不在服务代码中固化某一 CTB 文件名、颜色表或项目图层。

### 删除、改名和覆盖保护

- 图层或布局的 `state: "absent"` 会进入删除计划；默认 `require_empty: true`。图层只有同时明确 `require_empty: false` 与 `allow_nonempty: true` 才允许处理非空实体。
- `rename_from` 只描述一个明确旧名称；目标重名、保护层 0/Defpoints、Model、当前层或最后一个图纸空间布局都会成为冲突。
- `preserve_unmanaged: true` 时，配置未声明的图层/布局/样式只进入额外项报告，不自动删除。
- 页面设置和块库条目分别使用 `replace`、`replace_existing`；默认均为 false。

### 比例与打印资源

`plot_scale.mode` 取 `standard`、`custom` 或 `fit`。标准比例需要 `standard_scale`；自定义比例必须同时给出正数 `numerator`、`denominator`。页面设置中的 PC3、介质和 CTB/STB 文件必须与 `cad_get_plot_environment` 返回的当前 AutoCAD 实际资源匹配；只写进 JSON 不代表已经验证。

### 块库证据

每个 `block_library.sources[]` 至少包含：

```json
{
  "source_path": "C:\\绝对路径\\通用块库.dwg",
  "expected_sha256": "64位十六进制SHA-256",
  "drawing_unit": "millimeters",
  "block_names": ["块名"],
  "import_on_apply": true,
  "replace_existing": false
}
```

加载 profile 时先核对路径、扩展名、文件存在性和 SHA-256；`acad` 预览再只读检查块名、单位及依赖。源 DWG 以只读方式打开，检查或导入完成后关闭且不保存。

### 兼容边界

原有 DraftingProfile 1.0 仍可供环境工程语义工作流加载；`cad_preview_dwt_profile`、`cad_apply_dwt_profile` 和 `cad_validate_dwt_profile` 明确要求 2.0。DXF 模式能用于部分基础字段和静态属性块测试，但不能证明 AutoCAD 打印资源、DWG 块库或关联字段，因此不能正式应用完整 DWT profile。

结构示例见 [dwt-profile-v2.schema-example.json](../examples/dwt-profile-v2.schema-example.json)。该示例不包含未经核验的本机资源，不是正式用户配置。

## 图框模板清单 1.0

[template-manifest.json](../examples/template-manifest.json) 对应
`cad_mcp.engineering.templates.TemplateManifest`；它与下节的块审查规格卡用途不同。

| 顶层字段 | 要求 |
|---|---|
| `schema_version` | 默认 `"1.0"` |
| `template_id` | 必填，稳定且非空的模板标识 |
| `source_dwg` | 必填，源 DWG 路径；相对路径按清单所在目录解析 |
| `source_sha256` | 必填，真实源文件的 64 位十六进制 SHA-256 |
| `source_units` | 默认 `unitless`，应按核验结果填写 |
| `sheets` | 必填，1–128 个图幅配置 |
| `metadata` | 可选说明对象 |

每个 `sheets[]` 至少提供 `id`、`paper_size`、正值 `paper_width/paper_height`、
`block_name`、`source_bounds=[xmin,ymin,xmax,ymax]`、`usable_regions` 和正值
`fixed_insert_scale`；`orientation` 默认 `landscape`，可选 `portrait`。
`source_bounds` 是实际图块范围，`fixed_insert_scale` 是源坐标到布局毫米的固定换算，
不能为了塞入纸张而调整。

完整布局工具还必须显式提供 `paper_reference_bounds`（块定义中的物理纸张参考矩形）
和 `printed_bounds`（需打印图框线范围）。这两个字段在模型结构上允许省略，仅为兼容旧
清单；完整布局遇到缺失即停止。`usable_regions` 和可选 `reserved_regions` 每项为
`{id,x,y,width,height}`，采用从物理纸张左下角起算的毫米坐标，不能与块定义坐标混用。
`fields`、`tables` 按需要声明；所有层级拒绝未知字段，坐标必须有限。

示例只用于结构演示：路径、块名、A3 尺寸和区域划分不代表真实图框或规范，
`source_sha256` 的 64 个零是待替换占位值，不能作为哈希核验通过的证据。
先替换为已核验的源文件和范围，再调用 `cad_verify_template_manifest`；只做 JSON 结构
校验不证明文件存在、哈希匹配或布局正确。打印边距与模型比例仍须按
[完整布局工作流](../../../docs/sheet-layouts.md) 独立核验。

## DWG 块规格卡 1.0

指定块深度审查使用文件名以 `_manifest.json` 结尾的 UTF-8 JSON。顶层必须显式包含：

| 字段 | 内容与约束 |
|:--|:--|
| `schema_version` | 固定为 `"1.0"` |
| `source_dwg` | 现有 DWG 的完整绝对路径；不从规格卡文件名自动推断 |
| `source_sha256` | 64 位十六进制 SHA-256；不匹配即停止 |
| `blocks` | 一个或多个互不重名的块规格卡 |

每个块必须提供 `name`、正数 `fingerprint_quantization`、显式 `expected_fingerprint`（未知时写 `null`）、正数 `connection_tolerance`、`symmetry_tests` 和 `checks`。这些字段没有隐含默认值。

`symmetry_tests[]` 支持：

- `reflection`：显式给出 `axis_point`、非零 `axis_direction` 和 `tolerance`。
- `rotation`：显式给出 `center`、大于等于 2 的 `order` 和 `tolerance`。

`checks[]` 由稳定 `id`、JSON Pointer `path`、`operator`、`expected` 和可选 `tolerance` 组成。`operator` 可取 `equals`、`within`、`unordered_equals`、`contains`、`range`；`within` 必须提供容差，`range.expected` 必须且只能包含 `min`、`max`。

审查结果的主要可比较路径包括 `/block`、`/entity_tree`、`/geometry/bounding_box`、`/geometry/center`、`/geometry/symmetry`、`/geometry/connection_points`、`/properties/texts`、`/properties/attdefs`、`/properties/layers`、`/properties/linetypes`、`/properties/lineweights`、`/properties/nested_references` 和 `/fingerprint`。规格卡未声明的期望值不自动生成；预期指纹为 `null` 时仍返回实际指纹，但该比较项为“无法验证”。

结构示例见 [block-library_manifest.json](../examples/block-library_manifest.json)。示例中的路径、哈希和期望指纹均需用真实核验结果替换。

## 环境工程模型的字段与引用

| 层级 | 字段与关系 |
|---|---|
| 项目 | 必填 project_id、project_name；schema_version=1.0、units=mm，revision 为稳定修订字符串 |
| facilities | 每项用稳定 id，声明 facility_type、origin、outline；components 表示同一设施的部件 |
| network_systems | 每项 id/name/medium，内部 nodes 和 segments；不能把旧 nodes/pipes 放在顶层 |
| nodes | id、point；可声明 node_type、elevation、equipment_id、port_id，绑定必须指向真实对象 |
| segments | id、start_node、end_node、path、section；两端节点必须在所属网络内存在，路径端点与节点位置一致 |
| views | id/view_type/scale_denominator；facility_ids、network_system_ids、profile_ids、annotation_ids 引用已声明对象 |
| profiles | 纵断面系列及水平/竖向比例；可按 network_system_id、segment_ids 引用网络和管段 |
| annotations | annotation_type、view_id、anchor，以及按类型要求的文字、值或 target_id |
| schedules | 表格类型、source_type、columns、view_id 等；使用 schedules 而非旧 tables |
| sheets | 纸张、布局和 template_id、view_ids 组合，不能把结构校验当作原生布局已建立 |
| metadata | 说明信息；不参与工程设计判断 |

项目内语义ID须遵守模型校验要求，不靠重复名称连接。字段的可省略性、默认值、长度范围
与联合类型以[完整参数结构](../../../docs/tool-schemas.json)中的 EnvironmentModel 及其
引用定义为准。最小示例已按当前模型改写，并通过公共校验和预览接口回归；只作结构与
编译演示，不证明尺寸合理、资源存在或规范符合。制图配置、模板清单和块规格卡的占位
路径/哈希都须在正式应用前替换，不能为了通过校验编造资源。

## 输入键、空值与成功证据

必填且允许 null 的键仍需出现，例如实心填充的 boundary_handle/points；省略其中一个
不是合法的二选一。多数工具返回 JSON，三个历史查询例外和MCP图片返回见[工具参考](../../../docs/api-reference.md)。
缓存健康结果可能省略 semantic_policy；缺失字段保持未知。DXF首次新建会保存空白文件，
acad新建返回预定路径但文档尚未保存，必须显式检查文件和实际保存状态。
