<#
    把 qPCR 分析工具打包成文件夹（onedir），启动时不必每次解压。

    用法（在项目根目录执行）：
        powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
        powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -SkipInstall   # 跳过依赖安装
        powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -OneFile       # 打包成单文件，拷贝方便但启动更慢

    产物：dist\qPCR_Analyzer\qPCR_Analyzer.exe
          dist\qPCR_Analyzer.zip
#>

[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$VenvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$AppName = "qPCR_Analyzer"

function Write-Step($message) {
    Write-Host ""
    Write-Host "==> $message" -ForegroundColor Cyan
}

function Get-FolderSizeMB($path) {
    $bytes = (Get-ChildItem $path -Recurse -File | Measure-Object -Property Length -Sum).Sum
    return [math]::Round($bytes / 1MB, 1)
}

# --- 1. 虚拟环境 ---
if (-not (Test-Path $VenvPython)) {
    Write-Step "创建虚拟环境 .venv"
    python -m venv .venv
    if (-not (Test-Path $VenvPython)) {
        throw "虚拟环境创建失败，请确认已安装 Python 3.10 以上版本并加入 PATH。"
    }
}

# --- 2. 依赖 ---
if (-not $SkipInstall) {
    Write-Step "安装依赖"
    & $VenvPython -m pip install --upgrade pip
    & $VenvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败。" }
}

# --- 3. 清理上一次的产物 ---
Write-Step "清理旧的构建产物"
foreach ($dir in @("build", "dist")) {
    if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
}
if (Test-Path "$AppName.spec") { Remove-Item "$AppName.spec" -Force }

# --- 4. 打包 ---
# PySide6 会连带装上 QtWebEngine、Qt3D、Multimedia 等一大堆用不到的模块，
# 全部排除掉能把体积从 200MB 压到 70MB 左右。
$excluded = @(
    "tkinter", "unittest", "pydoc_data", "pytest", "numpy", "pandas", "matplotlib",
    "PIL", "Pillow",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets", "PySide6.QtQml",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtLocation", "PySide6.QtSensors", "PySide6.QtSerialPort",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSpatialAudio",
    "PySide6.QtTest", "PySide6.QtHelp", "PySide6.QtDesigner", "PySide6.QtUiTools",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSql", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtNetworkAuth", "PySide6.QtHttpServer",
    "PySide6.QtTextToSpeech", "PySide6.QtStateMachine"
)

$pyiArgs = @(
    "--noconfirm", "--clean", "--windowed",
    "--name", $AppName,
    "--paths", ".",
    "--hidden-import", "openpyxl.cell._writer"
)
$pyiArgs += if ($OneFile) { "--onefile" } else { "--onedir" }
foreach ($module in $excluded) { $pyiArgs += @("--exclude-module", $module) }
if (Test-Path "assets\app.ico") {
    $pyiArgs += @("--icon", "assets\app.ico")
    # Windows 上 --add-data 用分号分隔「源;包内相对路径」
    $pyiArgs += @("--add-data", "assets\app.ico;assets")
}
$pyiArgs += "main.py"

Write-Step "PyInstaller 打包中（首次构建约 1-3 分钟）"
& $VenvPython -m PyInstaller @pyiArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败。" }

# --- 5. 结果 ---
$exePath = if ($OneFile) { "dist\$AppName.exe" } else { "dist\$AppName\$AppName.exe" }
if (-not (Test-Path $exePath)) { throw "打包结束但没有找到 $exePath。" }

if (-not $OneFile) {
    $dirPath = "dist\$AppName"
    $zipPath = "dist\$AppName.zip"
    Write-Step "压缩发布包 $zipPath"
    Compress-Archive -Path $dirPath -DestinationPath $zipPath
}

Write-Step "打包完成"
if ($OneFile) {
    $sizeMB = [math]::Round((Get-Item $exePath).Length / 1MB, 1)
    Write-Host "  产物：$exePath" -ForegroundColor Green
    Write-Host "  体积：$sizeMB MB" -ForegroundColor Green
    Write-Host "  双击即可运行，目标机器无需安装 Python。" -ForegroundColor Green
} else {
    $sizeMB = Get-FolderSizeMB $dirPath
    $zipMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
    Write-Host "  产物：$exePath" -ForegroundColor Green
    Write-Host "  文件夹：$sizeMB MB" -ForegroundColor Green
    Write-Host "  发布包：$zipPath（$zipMB MB）" -ForegroundColor Green
    Write-Host "  请整个文件夹一起拷贝，不要只拿走 exe。目标机器无需安装 Python。" -ForegroundColor Green
}
