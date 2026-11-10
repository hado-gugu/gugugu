#!/bin/bash
set -euo pipefail

root="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd -- "$root"
guide='使用前请先阅读.txt'

for input in app.py catalog.json assets/app-icon.png samples "$guide"; do
    if [[ ! -e "$input" ]]; then
        printf '缺少构建文件：%s\n' "$input" >&2
        exit 1
    fi
done

/bin/bash "$root/run-macos.command" --setup-only
python="$root/.venv-macos/bin/python"
"$python" -m pip --isolated install --disable-pip-version-check --only-binary=:all: \
    pyinstaller==6.22.3 pyinstaller-hooks-contrib==2026.9

mkdir -p "$root/dist-macos" "$root/build/macos"
output="$(mktemp -d "$root/dist-macos/arm64-beta.XXXXXXXX")"
work="$(mktemp -d "$root/build/macos/build.XXXXXXXX")"
export PYINSTALLER_CONFIG_DIR="$work/cache"

"$python" -X utf8 -m PyInstaller \
    --onedir --windowed --noupx --target-architecture arm64 \
    --name Gugugu --osx-bundle-identifier com.haoxindegezi.gugugu \
    --icon "$root/assets/app-icon.png" \
    --distpath "$output" --workpath "$work/work" --specpath "$work" \
    --paths "$root" \
    --add-data "$root/catalog.json:." \
    --add-data "$root/assets:assets" \
    --add-data "$root/samples:samples" \
    --collect-all rapidocr --copy-metadata rapidocr \
    --collect-binaries onnxruntime --collect-data onnxruntime --copy-metadata onnxruntime \
    --collect-all objc --collect-all Foundation --collect-all AppKit \
    --collect-all Quartz --collect-all CoreMedia --collect-all ScreenCaptureKit \
    --copy-metadata pyobjc-core --copy-metadata pyobjc-framework-Cocoa \
    --copy-metadata pyobjc-framework-Quartz --copy-metadata pyobjc-framework-CoreMedia \
    --copy-metadata pyobjc-framework-ScreenCaptureKit \
    --exclude-module windows_capture --exclude-module qa \
    "$root/app.py"

bundle="$output/Gugugu.app"
test -x "$bundle/Contents/MacOS/Gugugu"
for model in PP-OCRv6_det_small.onnx PP-OCRv6_rec_small.onnx ch_ppocr_mobile_v2.0_cls_mobile.onnx; do
    if [[ -z "$(find "$bundle/Contents" -name "$model" -print -quit)" ]]; then
        printf '应用包中缺少 OCR 模型：%s\n' "$model" >&2
        exit 1
    fi
done

"$python" -X utf8 - "$bundle/Contents/Info.plist" <<'PY'
from pathlib import Path
import plistlib
import sys

from branding import APP_NAME, APP_VERSION

path = Path(sys.argv[1])
info = plistlib.loads(path.read_bytes())
info.update(CFBundleDisplayName=APP_NAME, CFBundleShortVersionString=APP_VERSION,
            CFBundleVersion=APP_VERSION)
path.write_bytes(plistlib.dumps(info, sort_keys=False))
PY
# PyInstaller signs the nested binaries. Refresh the outer seal after editing its plist.
codesign --force --sign - "$bundle"
codesign --verify --deep --strict --verbose=2 "$bundle"

cp "$root/$guide" "$output/$guide"
printf '构建完成：%s\n' "$bundle"
