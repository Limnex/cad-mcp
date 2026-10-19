#Requires -Version 7.0
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

$repoRoot = Split-Path -Parent $PSScriptRoot
$tempPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
$buildOutput = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) ("cad-mcp-build-check-{0}" -f [Guid]::NewGuid().ToString('N'))))
if (-not $buildOutput.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "临时构建目录不在系统临时目录内：$buildOutput"
}

$generatedPaths = @('build', 'src\local_cad_mcp.egg-info') | ForEach-Object {
    $candidate = [IO.Path]::GetFullPath((Join-Path $repoRoot $_))
    if (-not $candidate.StartsWith($repoRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "构建清理路径不在仓库内：$candidate"
    }
    if (-not (Test-Path -LiteralPath $candidate)) { $candidate }
}
$previousBytecodeSetting = [Environment]::GetEnvironmentVariable('PYTHONDONTWRITEBYTECODE', 'Process')
$env:PYTHONDONTWRITEBYTECODE = '1'

Push-Location $repoRoot
try {
    uv sync --locked --dev
    uv run pytest -m 'not acad'
    uv build --wheel --out-dir $buildOutput
} finally {
    Pop-Location
    [Environment]::SetEnvironmentVariable('PYTHONDONTWRITEBYTECODE', $previousBytecodeSetting, 'Process')
    if (Test-Path -LiteralPath $buildOutput) {
        Remove-Item -LiteralPath $buildOutput -Recurse -Force
    }
    foreach ($generatedPath in $generatedPaths) {
        if (Test-Path -LiteralPath $generatedPath) {
            if (((Get-Item -LiteralPath $generatedPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "构建清理路径是链接，拒绝删除：$generatedPath"
            }
            Remove-Item -LiteralPath $generatedPath -Recurse -Force
        }
    }
}

Write-Host '验证完成：测试与临时 Python wheel 构建均通过，构建产物已清理。'
