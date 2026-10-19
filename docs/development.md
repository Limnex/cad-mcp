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

## GitHub Actions

`.github/workflows/ci.yml` 在 push 和 pull request 时使用 Windows 与 Python 3.12 运行
测试并执行 `uv build --wheel`。构建步骤只验证 wheel 完整性，不上传或发布任何构建
产物。

## 文档要求

- README 面向 GitHub 仓库访问者，所有用户文档以仓库根目录为入口。
- 兼容性现象要标注测试环境和证据边界，不把单机实测写成普遍结论。
- MCP 工具参数和返回字段以运行时定义及内置 Skill 的 `references/tool-contracts.md` 为准。
