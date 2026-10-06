#!/bin/bash
# macOS CI 端到端测试：像用户一样把 zip 里的 .app 放进「应用程序」、打开它，然后测试、截图。
# 在 GitHub Actions 的 macos-15 上跑（.github/workflows/mac-dev.yml），本机不要跑：它会改系统授权、锁屏。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUT="$ROOT/out"
SHOTS="$OUT/shots"
mkdir -p "$SHOTS" "$OUT/target" "$OUT/e2e-a" "$OUT/e2e"
ZIP="dist/iPad-Remote-Desktop-mac-arm64.zip"
APP="/Applications/iPad 远程桌面.app"
SUP="$HOME/Library/Application Support/iPad 远程桌面"
RC=0
step() { echo; echo "======== $*"; }
wait_health() { for _ in $(seq 1 60); do curl -sf http://127.0.0.1:8765/health >/dev/null && return 0; sleep 1; done; return 1; }
shot() {
  echo "$1" > "$SUP/data/shot.req"
  for _ in $(seq 1 25); do [ -f "$SUP/data/shot.req" ] || break; sleep 0.3; done
  sleep 0.5
}
pw() { python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['password'])" "$SUP/config.json"; }
procs() {
  ps -axo pid,ppid,pgid,%cpu,rss,command | grep -i -e iPadRemote -e cloudflared | grep -v grep
  lsappinfo list 2>/dev/null | grep -i -A4 "ipad-remote-desktop" | head -30
}
quit_app() {
  pkill -TERM -f "MacOS/iPadRemoteDesktop$"
  for _ in $(seq 1 30); do pgrep -f "MacOS/iPadRemoteDesktop" >/dev/null || return 0; sleep 0.5; done
  echo "没在 15 秒内退出，强制结束"; pkill -9 -f "MacOS/iPadRemoteDesktop"
}

step "解压到「应用程序」（和用户一样：双击 zip = ditto 解压）"
rm -rf "$APP"
FRESH=$(mktemp -d)
ditto -x -k "$ZIP" "$FRESH" && ls -la "$FRESH"
ditto "$FRESH/iPad 远程桌面.app" "$APP" || { echo "复制失败"; exit 1; }
codesign --verify --deep --strict --verbose=2 "$APP" 2>&1 || RC=1
spctl --assess --type execute -vv "$APP" 2>&1 || echo "（ad-hoc 签名，Gatekeeper 不认是预期的：用户第一次要在「隐私与安全性」里点「仍要打开」）"

step "测试专用的环境变量（截图目录、把“断开后 2 分钟”缩成 5 秒）"
launchctl setenv RD_SHOT_DIR "$SHOTS"
launchctl setenv RD_PRIVACY_GRACE 5

step "A. 第一次打开（还没授权）"
open "$APP"
if wait_health; then echo "服务已启动"; else echo "服务没起来"; RC=1; fi
sleep 5
shot first-run
screencapture -x "$SHOTS/screen-first-run.png" || echo "screencapture 失败"
procs
python3 tests/mac_helpers.py windows > "$OUT/windows-first-run.json"
python3 tests/e2e_client.py --port 8765 --password "$(pw)" --out "$OUT/e2e-a" --os mac \
  || echo "（没授权时注入、截屏本来就不能用，这一轮只看能不能启动、登录）"
mkdir -p "$OUT/logs-a"; cp "$SUP"/data/*.log "$OUT/logs-a/" 2>/dev/null
quit_app
procs || echo "已全部退出（包括 cloudflared）"
python3 tests/mac_helpers.py windows > "$OUT/windows-after-a.json"
# 第一次打开时系统弹的授权提示，程序退出后还挂在屏幕上：关掉，免得挡住后面的测试
killall UserNotificationCenter universalAccessAuthWarn 2>/dev/null; true

step "B. 授权"
bash tests/mac_tcc.sh "$APP" 2>&1

step "B. 再次打开（已授权）"
open "$APP"
wait_health || RC=1
sleep 4
shot granted
python3 tests/mac_target.py "$OUT/target" > "$OUT/target.log" 2>&1 &
TPID=$!
for _ in $(seq 1 30); do [ -f "$OUT/target/layout.json" ] && break; sleep 0.5; done
sleep 1.5
screencapture -x "$SHOTS/screen-granted.png"
procs
step "B. iPad 网页端（WebKit 模拟 iPad）"
python3 tests/web_shot.py --port 8765 --password "$(pw)" --out "$OUT/web" --os mac || RC=1
python3 tests/e2e_client.py --port 8765 --password "$(pw)" --out "$OUT/e2e" --os mac --mac-full --input-required \
  --target "$OUT/target" --sup "$SUP" --app "$APP" --shots "$SHOTS" || RC=1

step "收尾"
echo "--- 录屏确认记录（系统写的）"; plutil -p "$HOME/Library/Group Containers/group.com.apple.replayd/ScreenCaptureApprovals.plist" 2>&1 | head -40
python3 tests/mac_helpers.py windows > "$OUT/windows-end.json"
pmset -g assertions > "$OUT/assertions.txt" 2>&1
grep -i -e "ipad" -e "PreventUserIdle" "$OUT/assertions.txt" | head
procs
cp -R "$SUP/data" "$OUT/appdata" 2>/dev/null
cp "$SUP/访问地址.txt" "$OUT/" 2>/dev/null
rm -f "$OUT"/appdata/sessions.json
kill $TPID 2>/dev/null
log show --last 15m --style compact --predicate 'eventMessage CONTAINS[c] "ipad-remote-desktop" OR eventMessage CONTAINS[c] "iPadRemoteDesktop"' \
  > "$OUT/system.log" 2>&1 || true
exit $RC
