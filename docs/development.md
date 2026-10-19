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

## GitHub Actions

`.github/workflows/ci.yml` 在 push 和 pull request 时使用 Windows 与 Python 3.12 运行
测试并执行 `uv build --wheel`。构建步骤只验证 wheel 完整性，不上传或发布任何构建
产物。

## 文档要求

- README 面向 GitHub 仓库访问者，所有用户文档以仓库根目录为入口。
- 兼容性现象要标注测试环境和证据边界，不把单机实测写成普遍结论。
- MCP 工具参数和返回字段以运行时定义及内置 Skill 的 `references/tool-contracts.md` 为准。
