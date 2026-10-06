#!/bin/bash
# 只在 CI 里用：直接往 TCC 数据库里写授权记录（相当于用户在「系统设置 → 隐私与安全性」里打开开关）。
# GitHub 的 macOS 机器关了 SIP，所以 sudo 能写；用户的 Mac 上不可能也不需要这么做。
#   bash tests/mac_tcc.sh "/Applications/iPad 远程桌面.app"
set -u
APP="$1"
BID=com.haoawake.ipad-remote-desktop
csrutil status || true
REQ=$(codesign -d -r- "$APP" 2>&1 | sed -n 's/^designated => //p')
echo "designated requirement: $REQ"
HEX=""
if [ -n "$REQ" ] && echo "$REQ" | csreq -r- -b /tmp/rd.csreq 2>/dev/null; then
  HEX=$(xxd -p /tmp/rd.csreq | tr -d '\n')
fi
CSREQ="NULL"; [ -n "$HEX" ] && CSREQ="X'$HEX'"
NOW=$(date +%s)
SYS="/Library/Application Support/com.apple.TCC/TCC.db"
USR="$HOME/Library/Application Support/com.apple.TCC/TCC.db"
for DB in "$SYS" "$USR"; do
  for S in kTCCServiceScreenCapture kTCCServiceAccessibility kTCCServicePostEvent kTCCServiceListenEvent \
           kTCCServiceSystemPolicyDownloadsFolder; do
    if sudo sqlite3 "$DB" "INSERT OR REPLACE INTO access (service, client, client_type, auth_value, auth_reason, auth_version, csreq, flags, last_modified) VALUES ('$S', '$BID', 0, 2, 4, 1, $CSREQ, 0, $NOW);" 2>&1; then
      echo "ok   $S  $DB"
    else
      echo "FAIL $S  $DB"
    fi
  done
  sudo sqlite3 "$DB" "SELECT service, client, auth_value FROM access WHERE client='$BID';" 2>&1
done
echo "--- 机器上已有的屏幕录制授权（参考）"
sudo sqlite3 "$SYS" "SELECT client, auth_value FROM access WHERE service='kTCCServiceScreenCapture';" 2>&1 | head -20
# macOS 15 起，录屏程序开始截屏时（之后大约每月一次）系统会弹“……正在请求绕过系统专用窗口选择器”的确认框。
# 用户在电脑前点「允许」就行；CI 里没人点，先写上“刚刚允许过”的记录（两种格式都写：15.0 是日期，15.1 起是字典）
APPR="$HOME/Library/Group Containers/group.com.apple.replayd/ScreenCaptureApprovals.plist"
echo "--- 录屏确认记录（写之前）"; plutil -p "$APPR" 2>&1 | head -40
python3 - "$APPR" "$APP/Contents/MacOS/iPadRemoteDesktop" "$BID" <<'PY'
import datetime, os, plistlib, sys
path, exe, bid = sys.argv[1:4]
os.makedirs(os.path.dirname(path), exist_ok=True)
try:
    with open(path, "rb") as f:
        d = plistlib.load(f)
except Exception:
    d = {}
now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
entry = {"kScreenCaptureApprovalLastAlerted": now, "kScreenCaptureApprovalLastUsed": now,
         "kScreenCaptureAlertableUsageCount": 1}
for k in (exe, bid):
    old = d.get(k)
    d[k] = dict(old, **entry) if isinstance(old, dict) else (now if old is not None and not isinstance(old, dict) else entry)
with open(path, "wb") as f:
    plistlib.dump(d, f)
PY
echo "--- 录屏确认记录（写之后）"; plutil -p "$APPR" 2>&1 | head -40
killall -9 replayd 2>/dev/null
sudo killall -9 tccd 2>/dev/null; killall -9 tccd 2>/dev/null; true
sleep 2
