# 开发

## 本地验证

完整验证：

```powershell
pwsh -File .\scripts\verify.ps1
```

只运行测试：

```powershell
uv run pytest
```

测试默认使用仓库内 `.cache/pytest/` 下的临时图纸和日志目录，不读取用户的 CAD 配置。带 `acad` 标记的测试
可能连接本机 AutoCAD，应单独运行并确认当前图纸状态。

验证脚本始终排除 `acad` 实机测试，并把 Python wheel 构建到系统临时目录；构建检查完成后
清理 wheel 以及本次生成的 `build/` 和包元数据。它不会生成发布 ZIP，也不会把构建产物加入 Git。

从仓库根目录运行人工验证工具，例如：

```powershell
uv run python tools/manual/smoke_new_capabilities.py
uv run python tools/manual/smoke_stdio.py
```

这些工具把临时图纸与日志写入 `.cache/manual/`。`.cache/` 可以在测试结束后清理，
下次测试会重建；`.venv/` 保存开发依赖，通常保留。需要重新建立环境时，在根目录运行
`uv sync --locked --dev`，不要直接移动旧位置的虚拟环境。

## 异常模拟与进程隔离验证

以下测试使用独立 Python 进程模拟故障，不连接或启动真实 AutoCAD：

```powershell
uv run pytest tests/integration/test_runtime_fault_simulation.py tests/integration/test_process_isolation.py tests/integration/test_isolated_stdio.py
```

覆盖大图与嵌套块扫描、繁忙与中途断线、永不返回的单次调用、工作进程崩溃、写入后失去响应、排队写入、启动无响应、输入管道阻塞和异常协议状态；同时验证普通与网关 stdio 调用保留图纸状态及参数校验。

通过标准包括：生产监控器能结束永久阻塞的自有工作进程，MCP 诊断仍可响应，失败请求不被重放；写入结果未知时，后续写入、保存、关闭、退出和模式切换均被阻止，重连不能解除限制。永久阻塞用例必须通过，不能以预期失败或外部测试程序代为终止目标进程作为通过证据。测试程序保留超时兜底，仅用于避免测试自身挂起。

上述结果证明 MCP 能处理失效工作进程，不能证明真实 AutoCAD 内部操作已中止或回滚。真实 CAD 的正常往返验证需单独运行下节用例。

## 原生圆角验证

不连接 AutoCAD 的专项测试：`uv run pytest tests/unit/test_fillet.py`，覆盖输入校验、默认零写入、命令选边、圆弧及多段线半径回读、系统变量恢复、失败撤销，以及无变化时不撤销前次操作。

真实圆角往返测试为 `tests/integration/test_fillet_acad.py`。必须先手动打开 AutoCAD，并在用户授权后设置下节开关，再运行 `uv run pytest tests/integration/test_fillet_acad.py -m acad -s`。测试只探测已有实例，在独立临时图纸中检查 Trim/NoTrim、旋转 UCS 下的 WCS 拾取、整体多段线圆角及零半径恢复，关闭临时图纸时不保存。

异常和边界测试位于 `tests/integration/test_fillet_edges_acad.py`，单独运行 `uv run pytest tests/integration/test_fillet_edges_acad.py -m acad -x -s`。覆盖锁层、无效句柄/半径、倾斜及不同标高、短边、平行线原生半径变化、远坐标、同一多段线选边、误选保护、圆/圆弧组合和真实撤销。故障注入必须先记录本次命令完成标记与实际几何变化，再人为报错；恢复几何、设置及实际视图并确认 `rollback_status=succeeded` 后才记录通过。详细结果见[圆角实测记录](validation/native-fillet-2026-10-21.md)。

实测中，将两组新建/关闭临时图纸用例放在同一个测试进程运行时，曾出现公共图层查询的 `<unknown>.Item` 异常，原因尚未定位；独立进程的边界测试已完成整套回归。上述两份实机测试建议分别启动进程，不能把进程隔离视为该公共查询问题已修复。自动测试中的 COM 替身不能代替真实 AutoCAD 结果。

命令调用依据：[AutoLISP command-s](https://help.autodesk.com/cloudhelp/2023/ENU/AutoCAD-AutoLISP-Reference/files/GUID-5C9DC003-3DD2-4770-95E7-7E19A4EE19A1.htm)、[FILLET 拾取位置](https://help.autodesk.com/cloudhelp/2023/ENU/AutoCAD-AutoLISP/files/GUID-EF2D73CE-9B75-4532-AF2F-DD6F0C36C3FD.htm)和[二维圆角操作](https://help.autodesk.com/cloudhelp/2026/ENU/AutoCAD-Core/files/GUID-696681C1-4B8B-4995-8903-CCE6686179EA.htm)。

## 真实 AutoCAD 集成测试

默认测试不会连接或启动用户的 AutoCAD。只有明确设置开关后，才运行隔离的真实
AutoCAD 往返测试：

```powershell
$env:CAD_MCP_RUN_ACAD_INTEGRATION = '1'
uv run pytest -m acad
Remove-Item Env:CAD_MCP_RUN_ACAD_INTEGRATION
```

运行前先手动打开 AutoCAD，并关闭或保存重要图纸。作者已经观察到 Codex 桌面端通过 MCP
冷启动 AutoCAD 可能导致崩溃，因此测试和人工验证都不应依赖冷启动。

只验证本轮工作进程隔离的真实 CAD 往返，可在 AutoCAD 已打开时执行：

```powershell
$env:CAD_MCP_RUN_ACAD_INTEGRATION = '1'
try {
    uv run pytest tests/integration/test_runtime_bugfixes_acad.py::test_isolated_worker_roundtrip_in_existing_autocad
} finally {
    Remove-Item Env:CAD_MCP_RUN_ACAD_INTEGRATION
}
```

该用例只连接已有实例，在临时图纸中检查创建、图层、查询、微小间隙和健康状态，结束后不保存临时图纸并恢复原活动图纸；同时比较原有文档列表、路径及保存状态。它不在用户图纸中注入卡死或崩溃。

## 原生延伸验证

不连接真实 CAD 的定向测试：

```powershell
uv run pytest tests/unit/test_native_extend.py
```

`cad_extend` 的实机用例需先手动打开 AutoCAD，再使用上文的集成测试开关运行：

```powershell
$env:CAD_MCP_RUN_ACAD_INTEGRATION = '1'
try {
    uv run pytest tests/integration/test_native_extend_acad.py tests/integration/test_native_extend_limits_acad.py -m acad
} finally {
    Remove-Item Env:CAD_MCP_RUN_ACAD_INTEGRATION
}
```

用例在自建临时图纸中检查直线、圆弧、开放多段线的起点和终点延伸，线/圆/圆弧/多段线边界、
假想延长边界、旋转 UCS、对象捕捉、视图与系统变量恢复和无变化时避免误撤销。边界用例覆盖锁层、不相交、
不同标高、100 个边界、大坐标小间隙、带弧段多段线、0.0001 极短首段、1000/1001 顶点上限和真实撤销回滚；
结束后不保存临时图纸并恢复原活动图纸，
核对原有文档列表和保存状态。默认验证脚本不运行此用例；模拟测试通过不能代表真实 AutoCAD 已验证。

## GitHub Actions

`.github/workflows/ci.yml` 在 push 和 pull request 时使用 Windows 与 Python 3.12 运行
测试并执行 `uv build --wheel`。构建步骤只验证 wheel 完整性，不上传或发布任何构建
产物。

## 文档要求

- README 面向 GitHub 仓库访问者，所有用户文档以仓库根目录为入口。
- 兼容性现象要标注测试环境和证据边界，不把单机实测写成普遍结论。
- MCP 工具参数和返回字段以运行时定义及内置 Skill 的 `references/tool-contracts.md` 为准。
