#!/bin/bash
# 只在 CI 里用：打出来的 .app 长什么样（Info.plist、签名、大小、最低系统版本）。
set -u
cd "$(dirname "$0")/.."
APP="dist/iPad 远程桌面.app"
echo "== Info.plist"; plutil -p "$APP/Contents/Info.plist"
echo "== 签名"; codesign -dvvv "$APP" 2>&1 | head -20
codesign --verify --deep --strict --verbose=2 "$APP" 2>&1
echo "== 主程序"; file "$APP/Contents/MacOS/iPadRemoteDesktop"; otool -L "$APP/Contents/MacOS/iPadRemoteDesktop"
echo "== 大小"; du -sh "$APP" dist/*.zip
du -sh "$APP"/Contents/* "$APP"/Contents/Frameworks/* 2>/dev/null | sort -h | tail -25
echo "== Resources"; ls -la "$APP/Contents/Resources" | head -30
echo "== 最低系统版本"; cat build/mac-build.json
echo "== Mach-O 不该出现在 Resources 里"; find "$APP/Contents/Resources" -type f -exec file {} + | grep -i mach-o || echo "没有"
