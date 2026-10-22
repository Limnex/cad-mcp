# 图框与布局的完整工作流

先把物理纸张与固定尺寸图框匹配，再由有效绘图区决定视口的位置，最后确定模型中心和比例。布局纸张中心、整个图框的几何中心和有效绘图区中心不是同一个概念。

## 前置条件

- 使用已打开的 AutoCAD 和明确绑定的工作 DWG。模板源 DWG 只读；当前图纸不能就是模板源文件。
- 工作图纸中已有模板清单指定的原始图框块。图框块不根据模型大小缩放。
- 已有命名页面设置，包含真实存在的设备、纸张和打印样式。可先使用 `cad_get_plot_environment`、`cad_list_page_setups` 和 `cad_create_or_update_page_setup` 检查/创建。新完整工作流不从纸张名称猜尺寸，也不在只读预览时创建临时页面设置。
- 明确指定本次展示的模型范围或模型空间实体句柄，并把需要的尺寸、文字等纳入选择。不默认使用全图范围。

## 模板清单扩展

沿用现有清单及 `fixed_insert_scale`，增加可选字段。旧清单仍可用于原有工具，但缺少新字段时不能进入完整布局流程。

| 字段 | 坐标含义 |
| --- | --- |
| `source_bounds` | 原始块定义中完整图框几何的包围范围，用于插入后回读核对 |
| `paper_reference_bounds` | 原始块定义中对应物理纸张四边的参考矩形，不等于内部边框 |
| `printed_bounds` | 原始块定义中需打印图框线的覆盖范围，必须落在设备可打印区域 |
| `usable_regions` | 从物理纸张左下角起算的毫米坐标，用于承载模型视口 |
| `reserved_regions` | 同样使用纸张毫米坐标的图签、说明栏等预留区域 |

例如，**仅对一个纸张参考矩形为 `[0,0,594,420]` 的示例图框**，可以声明内部打印边界 `[10,10,584,410]`、主绘图区 `x=20,y=20,width=480,height=380`，以及右侧预留区 `x=510,y=10,width=74,height=400`。实际值必须从实际模板确认，不能把本例套用到其他图框。

`fixed_insert_scale` 只完成源块坐标到纸面毫米的固定换算。插入位置考虑实际块基点；不要求基点和原始坐标都是零。

## 三个工具

1. `cad_preview_sheet_layout`：只读生成计划。实际纸张和边距从所选命名页面设置读取；物理尺寸不合、打印边框被裁切或对象归属冲突时不能执行。预览在独立内存 ObjectDBX 数据库中复制页面设置并刷新设备资源，不刷新当前 DWG；随后核对当前页面配置、打印原点和数据库修改标志未变；只在系统临时目录生成用于原生读数的回执文件，不修改图纸、布局、系统变量或页面设置。
2. `cad_apply_sheet_layout`：携带同参数和预览返回的 `confirmation_token` 正式应用。严格语义模式还要提供已登记工程对象的 `semantic`。令牌有效期30分钟，绑定配置、图框定义、目标图纸和目标内容/布局快照；状态变化后重新预览。
3. `cad_validate_sheet_layout`：使用同一请求独立读取实际纸张、图框和视口，返回 `verified` 与差异列表。`ok=true` 只表示查询执行成功，必须再看 `verified`。本检查不以模型空间的 `cad_check_drawing` 代替。

预览参数示意（路径和名称需换成已核实的实际资源）：

```json
{
  "manifest_path": "D:/CAD/templates/frame-manifest.json",
  "sheet_id": "A2-L",
  "layout_name": "G01",
  "layout_id": "project-g01-sheet",
  "region_id": "main",
  "page_setup_name": "PDF-A2-L",
  "rotation": 0,
  "content_bounds": [10000, 20000, 11000, 21000],
  "model_mm_per_unit": 1,
  "scale_denominator": 100,
  "padding_mm": 2
}
```

`content_bounds` 与 `content_handles` 必须且只能提供一项。句柄为模型空间十进制句柄；`model_elevation` 默认0，当前流程支持同一标高、无视图旋转的 WCS XY 平面内容。`model_mm_per_unit` 必须显式提供：毫米模型为1，米模型为1000。模型真实长度与比例计算使用同一单位换算。

## 执行顺序与保护

1. 复制所选页面设置，设置毫米、布局打印、1:1和零打印偏移。读取实际纸张与旋转后的边距，换算纸空间原点。
2. 在物理纸张参考位置插入固定尺寸图框，回读其位置、范围、比例、旋转。失败时不创建模型视口。
3. 在选定有效绘图区内按留白放置视口。模型内容中心对准绘图区中的视口中心，和整张纸的中心无关。
4. 通过实际活动视口的 `ZoomCenter` 设置 WCS 模型中心。独立读取 VIEWPORT 的 DXF 12/16/17/40/41/45/51/68/69/90 与冻结图层数据，核对实际中心、方向、视图高度、比例及锁定状态；不写不存在的 `PViewport.ViewCenter`。
5. 将视口边界置于 `CAD-MCP-VPORT` 不打印图层，完整核验后记录持久归属和状态指纹。归属标记独立于工程语义标记。

`layout_id` 是稳定的业务标识。重复执行使用同一个 ID，更新已有受管图框和视口；人工改动进入冲突，不自动接管未归属图框。既有预设浮动视口可以用 `existing_viewport_handle` 显式指定十进制句柄复用：预览列出校正动作，保留原实体和句柄；系统视口、其他布局对象、锁定图层或已有其他工程身份的对象拒绝接管。未指定时，既有未归属视口与绘图区重叠不会自动删除；只有本次新建布局自动生成的浮动视口可以清理，纸空间系统视口保留。

整个应用使用一个撤销组，不自动保存或发布 PDF。失败时核对图元、布局、页面设置及图层是否恢复；无法确认恢复时阻止后续写入，并报告撤销状态。用户原来的布局选项卡和模型/纸空间上下文会恢复。

## 验证边界

离线单元测试验证计算、严格接口模拟、状态冲突、重复更新和失败路径。只有在真实 AutoCAD 中完成纸框匹配、模型显示、切换布局、保存重开和 PDF 检查，才能宣称完成现场验收。

真实 AutoCAD 测试是显式 opt-in，使用自行创建的隔离测试图纸，不修改已有用户图纸。设置 `CAD_MCP_RUN_ACAD_INTEGRATION=1`，并提供本机已核实的 `CAD_MCP_LAYOUT_TEST_DEVICE`、`CAD_MCP_LAYOUT_TEST_MEDIA`、`CAD_MCP_LAYOUT_TEST_STYLE`（A2横向测试资源）后，运行 `tests/integration/test_sheet_layout_acad.py`。未提供资源时跳过，不猜测打印机名称。

接口依据：Autodesk [PViewport](https://help.autodesk.com/cloudhelp/2022/ENU/AutoCAD-ActiveX-Reference/files/GUID-63DF4686-8A79-4A39-8247-4A3565F31FAA.htm)、[ZoomCenter](https://help.autodesk.com/cloudhelp/2026/ENU/AutoCAD-ActiveX-Reference/files/GUID-16BDEA18-4555-4EA7-A426-9A9E5683AC9B.htm)、[GetPaperMargins](https://help.autodesk.com/cloudhelp/2026/ENU/AutoCAD-ActiveX-Reference/files/GUID-2CF57709-4DBE-4CBE-A4AF-45D90C8F8B54.htm)、[VIEWPORT DXF](https://help.autodesk.com/cloudhelp/2021/ENU/AutoCAD-DXF/files/GUID-2602B0FB-02E4-4B9A-B03C-B1D904753D34.htm)。

## ActiveX 读取兼容

部分 AutoCAD 实例的动态 COM 包装要求显式传入输出参数；生成的包装则直接返回两个值。边界、纸张尺寸/边距和 XData 读取同时支持这两种实际签名，仅在明确的参数数量不匹配时切换调用方式。权限、运行或连接错误立即上报，不吞掉错误或用请求值代替实际读数。

## 打印比例的独立验证

布局及命名页面设置使用 ActiveX `AcPlotScale`，其中 `ac1_1=16`；1到15是英制建筑比例。不要与视口或其他 API 的比例枚举混用。完整流程另外读取布局原生 PLOTSETTINGS 的倍率因子（DXF 147）和标准比例标志，要求实际倍率为1。即使名称回读为“1:1”，倍率不一致仍拒绝验收。最终 PDF 必须渲染核对，不能用参数回读代替出图效果。

复用或新建视口后，先把纸空间显示范围适配到真实图纸，再开启视口、重生成并进入模型空间；防止预设中异常远的缩放使视口无法激活。失败仍回到纸空间，整个应用撤销恢复原视口和设置。

完整布局工具的进程执行时限独立设置：预览和校验各300秒，应用600秒；其他工具的30秒读取、120秒写入及排队时限保持不变。正式应用超时仍视为写入结果未知，阻止后续写入，不自动重试或宣称撤销成功。
