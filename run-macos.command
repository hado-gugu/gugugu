#!/bin/bash
set -euo pipefail

root="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd -- "$root"

if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 ]]; then
    printf '%s\n' '请在 Apple Silicon Mac 的原生终端中运行。' >&2
    exit 1
fi

setup_only=false
if [[ $# -eq 1 && "$1" == --setup-only ]]; then
    setup_only=true
elif [[ $# -ne 0 ]]; then
    printf '%s\n' '用法：bash run-macos.command [--setup-only]' >&2
    exit 2
fi

venv_python="$root/.venv-macos/bin/python"
if [[ ! -x "$venv_python" ]]; then
    bootstrap_python="${GUGUGU_PYTHON:-python3.12}"
    if ! command -v "$bootstrap_python" >/dev/null; then
        printf '%s\n' '请先安装 Apple Silicon 可用的 Python 3.12，再打开终端重试。' >&2
        exit 1
    fi
    check_python="$bootstrap_python"
else
    check_python="$venv_python"
fi

"$check_python" - <<'PY'
import platform
import sys

if platform.python_implementation() != "CPython" or sys.version_info[:2] != (3, 12):
    raise SystemExit("需要 CPython 3.12。")
if platform.machine() != "arm64":
    raise SystemExit("当前 Python 在 Rosetta 下运行。请使用原生 arm64 Python 3.12。")
version = tuple(map(int, platform.mac_ver()[0].split(".")[:2]))
if version < (14, 4):
    raise SystemExit("需要 macOS 14.4 或更新版本。")
PY

if [[ ! -x "$venv_python" ]]; then
    "$bootstrap_python" -m venv "$root/.venv-macos"
fi

if ! "$venv_python" - <<'PY'
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

for line in Path("requirements-macos.txt").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#"):
        name, expected = line.split("==", 1)
        try:
            if version(name) != expected:
                raise SystemExit(1)
        except PackageNotFoundError:
            raise SystemExit(1)
PY
then
    printf '%s\n' '正在下载并安装咕顾谷依赖到本目录的 .venv-macos……'
    "$venv_python" -m pip --isolated install --disable-pip-version-check \
        --only-binary=:all: -r "$root/requirements-macos.txt"
fi

"$venv_python" - <<'PY'
from importlib.metadata import distribution

rapidocr = distribution("rapidocr")
for name in ("PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx",
             "ch_ppocr_mobile_v2.0_cls_mobile.onnx"):
    path = rapidocr.locate_file(f"rapidocr/models/{name}")
    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit(f"本地 OCR 模型缺失：{name}。请重新创建 .venv-macos 后重试。")
PY

if [[ "$setup_only" == true ]]; then
    exit 0
fi

data_dir="$HOME/Library/Application Support/Gugugu"
exec "$venv_python" -X utf8 "$root/app.py" --data-dir "$data_dir"
