"""CI 用的 macOS 小工具（在 CI 自己的 Python 里跑，不是 .app 里的那个）。

“本机”事件：这里发出去的鼠标键盘事件不带 iPad 远程桌面的标记，对隐私屏的拦截来说就等于有人在摸本机的键盘鼠标。
"""
import ctypes
import json
import subprocess
import sys
import time

import Quartz

_src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)


def local_click(x, y, button=0):
    d, u, b = ((Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft) if button == 0
               else (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGMouseButtonRight))
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(_src, Quartz.kCGEventMouseMoved, (x, y), b))
    time.sleep(0.05)
    for t in (d, u):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(_src, t, (x, y), b))
        time.sleep(0.05)


def local_move(x, y):
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateMouseEvent(_src, Quartz.kCGEventMouseMoved, (x, y), 0))


def local_key(keycode, flags=0):
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(_src, keycode, down)
        Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.03)


def local_text(text):
    """和 iPad 远程桌面一样“把字写在按键事件里”发出去，但不带它的标记、也不切输入法（对照实验用）。"""
    for ch in text:
        n = len(ch.encode("utf-16-le")) // 2
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(_src, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(ev, n, ch)
            Quartz.CGEventSetFlags(ev, 0)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.01)


def cursor():
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return p.x, p.y


def display():
    did = Quartz.CGMainDisplayID()
    b = Quartz.CGDisplayBounds(did)
    mode = Quartz.CGDisplayCopyDisplayMode(did)
    return {"id": int(did), "pt": [b.size.width, b.size.height],
            "px": [Quartz.CGDisplayModeGetPixelWidth(mode), Quartz.CGDisplayModeGetPixelHeight(mode)]}


def locked():
    d = Quartz.CGSessionCopyCurrentDictionary() or {}
    return bool(d.get("CGSSessionScreenIsLocked", 0))


def windows(pid=None):
    out = []
    for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, 0) or []:
        if pid is None or w.get("kCGWindowOwnerPID") == pid:
            b = w.get("kCGWindowBounds") or {}
            out.append({"num": int(w.get("kCGWindowNumber", 0)), "pid": int(w.get("kCGWindowOwnerPID", 0)),
                        "owner": str(w.get("kCGWindowOwnerName", "")), "layer": int(w.get("kCGWindowLayer", 0)),
                        "name": str(w.get("kCGWindowName", "") or ""),
                        "bounds": [b.get("X"), b.get("Y"), b.get("Width"), b.get("Height")],
                        "sharing": int(w.get("kCGWindowSharingState", -1))})
    return out


def _tis():
    import ctypes
    hit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
    vp = ctypes.c_void_p
    for name, args, res in (("TISCreateInputSourceList", [vp, ctypes.c_bool], vp),
                            ("TISEnableInputSource", [vp], ctypes.c_int32),
                            ("TISSelectInputSource", [vp], ctypes.c_int32),
                            ("TISCopyCurrentKeyboardInputSource", [], vp),
                            ("TISGetInputSourceProperty", [vp, vp], vp)):
        fn = getattr(hit, name)
        fn.argtypes, fn.restype = args, res
    return hit, vp.in_dll(hit, "kTISPropertyInputSourceID").value


def input_source():
    import objc
    hit, key = _tis()
    src = hit.TISCopyCurrentKeyboardInputSource()
    v = hit.TISGetInputSourceProperty(src, key) if src else None
    return str(objc.objc_object(c_void_p=ctypes.c_void_p(v))) if v else ""


def _ime_do(action, sid):
    """在本进程里对某个输入法做 enable / select，返回错误码（0 = 成功，None = 没有这个输入法）。"""
    import objc
    from Foundation import NSDictionary
    hit, key = _tis()
    keyobj = objc.objc_object(c_void_p=ctypes.c_void_p(key))
    arr = hit.TISCreateInputSourceList(objc.pyobjc_id(NSDictionary.dictionaryWithObject_forKey_(sid, keyobj)), True)
    lst = objc.objc_object(c_void_p=ctypes.c_void_p(arr)) if arr else []
    if not len(lst):
        return None
    if action == "info":
        out = {}
        for k in ("kTISPropertyInputSourceIsEnabled", "kTISPropertyInputSourceIsSelectCapable",
                  "kTISPropertyInputSourceIsEnableCapable", "kTISPropertyInputSourceType"):
            v = hit.TISGetInputSourceProperty(objc.pyobjc_id(lst[0]), ctypes.c_void_p.in_dll(hit, k).value)
            out[k[19:]] = str(objc.objc_object(c_void_p=ctypes.c_void_p(v))) if v else None
        return json.dumps(out)
    fn = hit.TISEnableInputSource if action == "enable" else hit.TISSelectInputSource
    return fn(objc.pyobjc_id(lst[0]))


def _here(*args):
    """另起一个进程跑本文件：输入法列表在一个进程里会缓存，刚打开的输入法要新进程才看得到。"""
    p = subprocess.run([sys.executable, __file__] + list(args), capture_output=True, text=True, timeout=30)
    return (p.stdout + p.stderr).strip()


def input_source_fresh():
    out = _here("ime-current").splitlines()
    return out[-1] if out else ""


def select_input_source(sid, parent=None):
    """打开并切换到某个输入法（比如拼音 com.apple.inputmethod.SCIM.ITABC，parent 是它所属的输入法程序）。"""
    for attempt in range(2):
        for x in ([parent] if parent else []) + [sid]:
            print("输入法", x, "enable=%s" % _here("ime", "enable", x), _here("ime", "info", x))
        time.sleep(2.0)
        r = _here("ime", "select", sid)
        print("输入法", sid, "select=%s" % r, _here("ime", "info", sid))
        if r.splitlines()[-1:] == ["0"]:
            return True
        if attempt == 0 and parent:
            # 第二次：照“系统设置 → 键盘 → 输入法 → +”的样子写进系统的输入法列表，让系统重新读一遍
            for item in ('<dict><key>Bundle ID</key><string>%s</string><key>InputSourceKind</key>'
                         '<string>Keyboard Input Method</string></dict>' % parent,
                         '<dict><key>Bundle ID</key><string>%s</string><key>Input Mode</key><string>%s</string>'
                         '<key>InputSourceKind</key><string>Input Mode</string></dict>' % (parent, sid)):
                subprocess.run(["defaults", "write", "com.apple.HIToolbox", "AppleEnabledInputSources", "-array-add", item])
            subprocess.run(["killall", "TextInputMenuAgent", "TextInputSwitcher"], capture_output=True)
            time.sleep(3.0)
    return False


def screencapture(path):
    try:
        r = subprocess.run(["screencapture", "-x", str(path)], capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001
        print("screencapture:", e)
        return False


def cg_capture(path):
    """用 CGWindowListCreateImage(整个屏幕上的所有窗口) 截一张，看看别的程序（不避开隐私屏）截到的是什么。"""
    try:
        from AppKit import NSBitmapImageRep
        img = Quartz.CGWindowListCreateImage(Quartz.CGRectInfinite, Quartz.kCGWindowListOptionOnScreenOnly, 0, 0)
        if img is None:
            return False
        rep = NSBitmapImageRep.alloc().initWithCGImage_(img)
        data = rep.representationUsingType_properties_(4, {})
        return bool(data.writeToFile_atomically_(str(path), True))
    except Exception as e:  # noqa: BLE001
        print("cg_capture:", e)
        return False


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "click":
        local_click(float(sys.argv[2]), float(sys.argv[3]))
    elif cmd == "key":
        local_key(int(sys.argv[2], 0), int(sys.argv[3], 0) if len(sys.argv) > 3 else 0)
    elif cmd == "info":
        print(json.dumps({"display": display(), "locked": locked(), "cursor": cursor()}))
    elif cmd == "ime":
        print(_ime_do(sys.argv[2], sys.argv[3]))
    elif cmd == "ime-current":
        print(input_source())
    elif cmd == "front":
        from AppKit import NSWorkspace
        f = NSWorkspace.sharedWorkspace().frontmostApplication()
        print(json.dumps({"bid": str(f.bundleIdentifier() or ""), "policy": int(f.activationPolicy()),
                          "menubar": bool(f.ownsMenuBar())} if f else {}))
    elif cmd == "apps":
        from AppKit import NSWorkspace
        print(json.dumps([{"pid": int(x.processIdentifier()), "policy": int(x.activationPolicy()),
                           "path": str(x.executableURL().path() if x.executableURL() else "")}
                          for x in NSWorkspace.sharedWorkspace().runningApplications()
                          if x.bundleIdentifier() == sys.argv[2]]))
    elif cmd == "windows":
        print(json.dumps(windows(int(sys.argv[2]) if len(sys.argv) > 2 else None), ensure_ascii=False, indent=1))
    elif cmd == "cgcapture":
        print(cg_capture(sys.argv[2]))
