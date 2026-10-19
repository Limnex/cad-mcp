#Requires -Version 7.0
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

$repoRoot = Split-Path -Parent $PSScriptRoot
$manifest = Get-Content -LiteralPath (Join-Path $repoRoot '.codex-plugin\plugin.json') -Raw -Encoding UTF8 | ConvertFrom-Json

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw '未找到 uv。请先安装 uv，然后重新运行本脚本。'
}

uv tool install --force $repoRoot

if (Get-Command codex -ErrorAction SilentlyContinue) {
    $marketplaces = codex plugin marketplace list --json | ConvertFrom-Json
    if ($marketplaces.marketplaces.name -contains 'cad-mcp') {
        Write-Host 'CAD MCP 插件市场已经登记，跳过重复添加。'
    } else {
        codex plugin marketplace add $repoRoot
    }

    $plugins = codex plugin list --json | ConvertFrom-Json
    $installedPlugin = $plugins.installed | Where-Object { $_.pluginId -eq 'cad-mcp@cad-mcp' }
    if ($installedPlugin -and $installedPlugin.version -eq $manifest.version) {
        Write-Host "CAD MCP 插件 $($manifest.version) 已安装，跳过重复安装。"
    } else {
        codex plugin add cad-mcp@cad-mcp
    }
} else {
    Write-Warning '未找到 Codex CLI，已跳过插件市场登记与插件安装。MCP 命令已经安装。'
}

cad-mcp config show
Write-Host '安装完成。请重启 Codex 或新建任务，以加载 CAD MCP 插件。'
