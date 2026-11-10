#!/bin/bash
set -euo pipefail

if [[ $# -ne 1 || -z "$1" ]]; then
    printf 'Usage: bash %s OUTPUT_DIRECTORY\n' "$0" >&2
    exit 2
fi
if [[ "$(uname -s)" != Darwin ]]; then
    printf '%s\n' 'Building GuguguCaptureCheck requires macOS and the Xcode command-line tools.' >&2
    exit 1
fi

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
mkdir -p -- "$1"
output="$(cd -- "$1" && pwd -P)"
app_name=GuguguCaptureCheck
destination="$output/$app_name.app"
if [[ -e "$destination" || -L "$destination" ]]; then
    printf 'Output already exists: %s\n' "$destination" >&2
    exit 1
fi

stage="$(mktemp -d "$output/.capture-check.XXXXXXXX")"
trap 'rm -rf -- "$stage"' EXIT
bundle="$stage/$app_name.app"
mkdir -p -- "$bundle/Contents/MacOS"
cp -- "$source_dir/Info.plist" "$bundle/Contents/Info.plist"
plutil -lint "$bundle/Contents/Info.plist"

xcrun --sdk macosx swiftc -O -target arm64-apple-macosx14.4 -module-name "$app_name" \
    -framework AppKit -framework CoreGraphics -framework ScreenCaptureKit \
    "$source_dir/$app_name.swift" -o "$bundle/Contents/MacOS/$app_name"
xcrun lipo -verify_arch arm64 "$bundle/Contents/MacOS/$app_name"
codesign --force --sign - "$bundle"
codesign --verify --deep --strict "$bundle"

mv -- "$bundle" "$destination"
printf '%s\n' "$destination"
