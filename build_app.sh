#!/usr/bin/env bash
# 把 qPCR 分析工具打包成 macOS .app（Apple Silicon）。
#
# 用法（在项目根目录执行）：
#     ./build_app.sh
#     ./build_app.sh --skip-install   # 跳过依赖安装
#
# 产物：dist/qPCR_Analyzer.app
#       dist/qPCR_Analyzer_macOS.zip
#
# 以后若要 Developer ID 签名 + 公证，在下面的 PyInstaller 参数里加
# --codesign-identity "Developer ID Application: ..." 即可，不必改打包结构。

set -euo pipefail
cd "$(dirname "$0")"

SKIP_INSTALL=0
for arg in "$@"; do
    case "$arg" in
        --skip-install) SKIP_INSTALL=1 ;;
        *)
            echo "未知参数：$arg" >&2
            exit 1
            ;;
    esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "这个脚本只能在 macOS 上运行。" >&2
    exit 1
fi

VENV_PYTHON=".venv/bin/python"
APP_NAME="qPCR_Analyzer"
BUNDLE_ID="com.jiangwy.qpcr-analyzer"

step() {
    echo
    echo "==> $*"
}

folder_size_mb() {
    python3 -c "import os; print(round(sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk('$1') for f in fs) / 1048576, 1))"
}

# --- 1. 虚拟环境 ---
if [[ ! -x "$VENV_PYTHON" ]]; then
    step "创建虚拟环境 .venv"
    python3 -m venv .venv
    if [[ ! -x "$VENV_PYTHON" ]]; then
        echo "虚拟环境创建失败，请确认已安装 Python 3.10 以上版本。" >&2
        exit 1
    fi
fi

# --- 2. 依赖 ---
if [[ "$SKIP_INSTALL" -eq 0 ]]; then
    step "安装依赖"
    "$VENV_PYTHON" -m pip install --upgrade pip
    "$VENV_PYTHON" -m pip install -r requirements.txt
fi

# --- 3. Dock / Finder 图标 ---
ensure_icns() {
    if [[ -f assets/app.icns ]]; then
        return
    fi
    if [[ ! -f assets/app_icon.png ]]; then
        echo "缺少 assets/app_icon.png，无法生成 icns。" >&2
        exit 1
    fi
    step "生成 assets/app.icns"
    local iconset="assets/app.iconset"
    local src="assets/app_icon.png"
    rm -rf "$iconset"
    mkdir -p "$iconset"
    sips -z 16 16 "$src" --out "$iconset/icon_16x16.png" >/dev/null
    sips -z 32 32 "$src" --out "$iconset/icon_16x16@2x.png" >/dev/null
    sips -z 32 32 "$src" --out "$iconset/icon_32x32.png" >/dev/null
    sips -z 64 64 "$src" --out "$iconset/icon_32x32@2x.png" >/dev/null
    sips -z 128 128 "$src" --out "$iconset/icon_128x128.png" >/dev/null
    sips -z 256 256 "$src" --out "$iconset/icon_128x128@2x.png" >/dev/null
    sips -z 256 256 "$src" --out "$iconset/icon_256x256.png" >/dev/null
    sips -z 512 512 "$src" --out "$iconset/icon_256x256@2x.png" >/dev/null
    sips -z 512 512 "$src" --out "$iconset/icon_512x512.png" >/dev/null
    sips -z 1024 1024 "$src" --out "$iconset/icon_512x512@2x.png" >/dev/null
    iconutil -c icns "$iconset" -o assets/app.icns
    rm -rf "$iconset"
}

ensure_icns

# --- 4. 清理上一次的产物 ---
step "清理旧的构建产物"
rm -rf build dist
rm -f "${APP_NAME}.spec"

# --- 5. 打包 ---
# PySide6 会连带装上 QtWebEngine、Qt3D、Multimedia 等一大堆用不到的模块，
# 全部排除掉能把体积从 200MB 压到 70MB 左右。
excluded=(
    tkinter unittest pydoc_data pytest numpy pandas matplotlib
    PIL Pillow
    PySide6.QtWebEngineCore PySide6.QtWebEngineWidgets PySide6.QtWebEngineQuick
    PySide6.QtWebChannel PySide6.QtWebSockets
    PySide6.QtQuick PySide6.QtQuick3D PySide6.QtQuickWidgets PySide6.QtQml
    PySide6.Qt3DCore PySide6.Qt3DRender PySide6.Qt3DAnimation
    PySide6.Qt3DExtras PySide6.Qt3DInput PySide6.Qt3DLogic
    PySide6.QtMultimedia PySide6.QtMultimediaWidgets
    PySide6.QtCharts PySide6.QtDataVisualization PySide6.QtGraphs
    PySide6.QtBluetooth PySide6.QtNfc PySide6.QtPositioning
    PySide6.QtLocation PySide6.QtSensors PySide6.QtSerialPort
    PySide6.QtRemoteObjects PySide6.QtScxml PySide6.QtSpatialAudio
    PySide6.QtTest PySide6.QtHelp PySide6.QtDesigner PySide6.QtUiTools
    PySide6.QtPdf PySide6.QtPdfWidgets PySide6.QtSql PySide6.QtOpenGL
    PySide6.QtOpenGLWidgets PySide6.QtNetworkAuth PySide6.QtHttpServer
    PySide6.QtTextToSpeech PySide6.QtStateMachine
)

pyi_args=(
    --noconfirm --clean --windowed --onedir
    --name "$APP_NAME"
    --osx-bundle-identifier "$BUNDLE_ID"
    --paths .
    --hidden-import openpyxl.cell._writer
)
for module in "${excluded[@]}"; do
    pyi_args+=(--exclude-module "$module")
done
if [[ -f assets/app.icns ]]; then
    pyi_args+=(--icon assets/app.icns --add-data "assets/app.icns:assets")
fi
if [[ -f assets/app_icon.png ]]; then
    pyi_args+=(--add-data "assets/app_icon.png:assets")
fi
if [[ -f assets/app.ico ]]; then
    pyi_args+=(--add-data "assets/app.ico:assets")
fi
pyi_args+=(main.py)

step "PyInstaller 打包中（首次构建约 1-3 分钟）"
"$VENV_PYTHON" -m PyInstaller "${pyi_args[@]}"

# --- 6. 结果 ---
app_path="dist/${APP_NAME}.app"
if [[ ! -d "$app_path" ]]; then
    echo "打包结束但没有找到 $app_path" >&2
    exit 1
fi

version="$("$VENV_PYTHON" -c "from qpcr_tool import __version__; print(__version__)")"
plutil -replace CFBundleShortVersionString -string "$version" "${app_path}/Contents/Info.plist"
plutil -replace CFBundleVersion -string "$version" "${app_path}/Contents/Info.plist"

zip_path="dist/${APP_NAME}_macOS.zip"
step "压缩发布包 $zip_path"
rm -f "$zip_path"
# 必须用 ditto 保留 Qt .framework 里的符号链接，普通 zip 会把应用打坏。
ditto -c -k --keepParent "$app_path" "$zip_path"

step "打包完成"
app_mb="$(folder_size_mb "$app_path")"
zip_mb="$(python3 -c "import os; print(round(os.path.getsize(r'''${zip_path}''') / 1048576, 1))")"
echo "  产物：${app_path}"
echo "  文件夹：${app_mb} MB"
echo "  发布包：${zip_path} (${zip_mb} MB)"
echo "  双击 .app 即可运行，目标机器无需安装 Python。"
echo "  从网上下载后若提示「已损坏」，在终端执行：xattr -cr ${app_path}"
