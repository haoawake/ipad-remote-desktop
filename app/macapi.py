"""macOS 底层接口：截屏、鼠标键盘注入、光标形状、剪贴板、防休眠、锁屏状态。

和 winapi.py 对外是同一组函数名，server.py / capture.py 不用关心在哪个系统上。

坐标约定（整个文件最容易出错的地方）：
- Quartz 的全局坐标单位是“点”，原点在主屏左上角、y 向下（和 Windows 一样是 y 向下）；
- 截屏拿到的是像素，Retina 屏上 1 点 = 2 像素；
- iPad 那边一律按“当前显示器的像素”算（和 Windows 版一致），换算只发生在 to_global / to_local、
  move_rel、wheel 和光标尺寸这几处，除以/乘以当前显示器的 ps（每点几个像素）。

本程序注入的每个事件都打上 MAGIC 标记（kCGEventSourceUserData）。隐私屏的本机键鼠拦截（mac_overlay.py）
靠它区分“这是远程发来的，放行”和“这是本机键盘鼠标，拦下”。
"""
import base64
import ctypes
import ctypes.util
import io
import logging
import os
import subprocess
import threading
import time
import zlib
from urllib.parse import quote

import objc
import Quartz
from AppKit import NSCursor, NSEvent, NSPasteboard, NSPasteboardTypeString, NSWorkspace
from Foundation import NSURL, NSProcessInfo

OS = "mac"
MAGIC = 0x52445244  # 'RDRD'


# ====================================================================== CoreGraphics（ctypes）
class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


_cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
_cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_ax = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")


def _sig(lib, name, args, res):
    try:
        fn = getattr(lib, name)
    except AttributeError:
        return None
    fn.argtypes = args
    fn.restype = res
    return fn


_u32, _vp, _sz = ctypes.c_uint32, ctypes.c_void_p, ctypes.c_size_t
CGGetActiveDisplayList = _sig(_cg, "CGGetActiveDisplayList", [_u32, ctypes.POINTER(_u32), ctypes.POINTER(_u32)],
                              ctypes.c_int32)
CGMainDisplayID = _sig(_cg, "CGMainDisplayID", [], _u32)
CGDisplayBounds = _sig(_cg, "CGDisplayBounds", [_u32], CGRect)
CGDisplayCopyDisplayMode = _sig(_cg, "CGDisplayCopyDisplayMode", [_u32], _vp)
CGDisplayModeGetPixelWidth = _sig(_cg, "CGDisplayModeGetPixelWidth", [_vp], _sz)
CGDisplayModeGetPixelHeight = _sig(_cg, "CGDisplayModeGetPixelHeight", [_vp], _sz)
CGDisplayModeRelease = _sig(_cg, "CGDisplayModeRelease", [_vp], None)
CGWindowListCreateImage = _sig(_cg, "CGWindowListCreateImage", [CGRect, _u32, _u32, _u32], _vp)
CGImageGetWidth = _sig(_cg, "CGImageGetWidth", [_vp], _sz)
CGImageGetHeight = _sig(_cg, "CGImageGetHeight", [_vp], _sz)
CGImageGetBytesPerRow = _sig(_cg, "CGImageGetBytesPerRow", [_vp], _sz)
CGImageGetBitsPerPixel = _sig(_cg, "CGImageGetBitsPerPixel", [_vp], _sz)
CGImageGetBitmapInfo = _sig(_cg, "CGImageGetBitmapInfo", [_vp], _u32)
CGImageGetDataProvider = _sig(_cg, "CGImageGetDataProvider", [_vp], _vp)
CGDataProviderCopyData = _sig(_cg, "CGDataProviderCopyData", [_vp], _vp)
CGColorSpaceCreateDeviceRGB = _sig(_cg, "CGColorSpaceCreateDeviceRGB", [], _vp)
CGColorSpaceRelease = _sig(_cg, "CGColorSpaceRelease", [_vp], None)
CGBitmapContextCreate = _sig(_cg, "CGBitmapContextCreate", [_vp, _sz, _sz, _sz, _sz, _vp, _u32], _vp)
CGBitmapContextGetData = _sig(_cg, "CGBitmapContextGetData", [_vp], _vp)
CGContextDrawImage = _sig(_cg, "CGContextDrawImage", [_vp, CGRect, _vp], None)
CGContextRelease = _sig(_cg, "CGContextRelease", [_vp], None)
CGPreflightScreenCaptureAccess = _sig(_cg, "CGPreflightScreenCaptureAccess", [], ctypes.c_bool)
CGCursorIsVisible = _sig(_cg, "CGCursorIsVisible", [], ctypes.c_int32)
CFDataGetBytePtr = _sig(_cf, "CFDataGetBytePtr", [_vp], _vp)
CFDataGetLength = _sig(_cf, "CFDataGetLength", [_vp], ctypes.c_long)
CFRelease = _sig(_cf, "CFRelease", [_vp], None)
AXIsProcessTrusted = _sig(_ax, "AXIsProcessTrusted", [], ctypes.c_bool)

kCGWindowListOptionOnScreenOnly = 1 << 0
kCGWindowListOptionOnScreenBelowWindow = 1 << 2
kCGWindowImageDefault = 0
kCGWindowImageNominalResolution = 1 << 4
kCGBitmapByteOrder32Little = 2 << 12
kCGImageAlphaPremultipliedFirst = 2


def set_dpi_aware():
    """Windows 才需要；macOS 上截屏和坐标本来就分得清点和像素。"""


def bind_child(pid):
    """Windows 用 Job 让 cloudflared 跟着本进程退出；macOS 上服务进程自成一个进程组，
    状态窗口进程收尾时整组一起结束（见 mac_ui.Worker.stop）。"""


# ====================================================================== 显示器
_mon_cache = {"t": 0.0, "list": None}


def _displays():
    """[{id, left, top, pw, ph(点), width, height(像素), ps}]，主屏排第一。2 秒内复用上次的结果。"""
    now = time.monotonic()
    if _mon_cache["list"] is not None and now - _mon_cache["t"] < 2:
        return _mon_cache["list"]
    ids = (_u32 * 16)()
    n = _u32(0)
    CGGetActiveDisplayList(16, ids, ctypes.byref(n))
    main = CGMainDisplayID()
    order = sorted(ids[:n.value], key=lambda d: d != main)
    out = []
    for did in order:
        b = CGDisplayBounds(did)
        pw, ph = b.size.width, b.size.height
        px = pw
        mode = CGDisplayCopyDisplayMode(did)
        if mode:
            px = CGDisplayModeGetPixelWidth(mode) or pw
            CGDisplayModeRelease(mode)
        ps = max(1.0, round(px / pw * 4) / 4) if pw else 1.0  # 1、1.5、2、3 …
        out.append({"id": did, "left": b.origin.x, "top": b.origin.y, "pw": pw, "ph": ph,
                    "ps": ps, "width": int(round(pw * ps)), "height": int(round(ph * ps))})
    if not out:  # 没有显示器（合盖、无头）时给一个占位，别让上层崩
        out = [{"id": main, "left": 0.0, "top": 0.0, "pw": 1280.0, "ph": 800.0, "ps": 1.0,
                "width": 1280, "height": 800}]
    _mon_cache.update(t=now, list=out)
    return out


def list_monitors():
    return [{"index": i + 1, "left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}
            for i, m in enumerate(_displays())]


def _display_at(x, y):
    for d in _displays():
        if d["left"] <= x < d["left"] + d["pw"] and d["top"] <= y < d["top"] + d["ph"]:
            return d
    return None


def _scale_at(x, y):
    d = _display_at(x, y)
    return d["ps"] if d else _displays()[0]["ps"]


def _clamp(x, y):
    """把点限制在某块显示器里（多屏拼起来不一定是个矩形，不能按外接矩形夹）。"""
    if _display_at(x, y):
        return x, y
    best = None
    for d in _displays():
        cx = min(max(x, d["left"]), d["left"] + d["pw"] - 1)
        cy = min(max(y, d["top"]), d["top"] + d["ph"] - 1)
        dist = (cx - x) ** 2 + (cy - y) ** 2
        if best is None or dist < best[0]:
            best = (dist, cx, cy)
    return best[1], best[2]


def to_global(g, x, y):
    """iPad 发来的坐标（相对当前显示器的像素）→ Quartz 全局坐标（点）。"""
    ps = g.get("ps", 1.0)
    return g["left"] + x / ps, g["top"] + y / ps


def to_local(g, x, y):
    ps = g.get("ps", 1.0)
    return (x - g["left"]) * ps, (y - g["top"]) * ps


# ====================================================================== 截屏
class Shot:
    __slots__ = ("width", "height", "bgra")

    def __init__(self, w, h, arr):
        self.width, self.height, self.bgra = w, h, arr


def _ui_pid():
    try:
        return int(os.environ.get("RD_UI_PID") or 0)
    except ValueError:
        return 0


def overlay_window():
    """隐私屏窗口里在最下面的那个的编号；没有隐私屏窗口返回 0。

    每块显示器一个隐私屏窗口，层级都一样，谁在上谁在下说不准；截“最下面那个之下的所有窗口”，
    就把所有隐私屏窗口都排除在外了。窗口列表是从前往后排的，所以取最后一个。
    """
    pid = _ui_pid()
    if not pid:
        return 0
    found = 0
    with objc.autorelease_pool():
        info = Quartz.CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, 0) or []
        for w in info:
            if w.get("kCGWindowOwnerPID") == pid and int(w.get("kCGWindowLayer", 0)) >= 1000:
                found = int(w.get("kCGWindowNumber", 0))
    return found


class Grabber:
    """和 mss.MSS 一个用法：.monitors[1:] 是各个显示器，.grab(m) 截一帧。

    用 CGWindowListCreateImage：隐私屏开着时用“某窗口之下的所有窗口”模式，
    截出来的是真实桌面（包括菜单栏、程序坞、通知），隐私屏本身不在里面。
    """

    def __init__(self):
        self._curtain_since = None

    @property
    def monitors(self):
        ds = _displays()
        return [None] + ds  # 和 mss 一样，0 号位留给“所有显示器”，这里用不到

    def close(self):
        pass

    def _exclude(self):
        """返回 (要不要等, 要排除的窗口)。"""
        import privacy_mac
        c = privacy_mac.CURTAIN
        if c is None or not c.capture_hint():
            self._curtain_since = None
            return False, 0
        wid = overlay_window()
        if c.shown and not wid:
            # 隐私屏该出现了但窗口还没出来：先不截，免得 iPad 上闪一下锁屏画面。最多等 1.5 秒
            if self._curtain_since is None:
                self._curtain_since = time.monotonic()
            if time.monotonic() - self._curtain_since < 1.5:
                return True, 0
        else:
            self._curtain_since = None
        return False, wid

    def grab(self, m, scale=1.0):
        wait, below = self._exclude()
        if wait:
            return None
        rect = CGRect(CGPoint(m["left"], m["top"]), CGSize(m["pw"], m["ph"]))
        # 低分辨率档（比如 Retina 屏选 50%）直接按“点”截，像素少 4 倍，省 CPU
        nominal = m["ps"] > 1 and scale * m["ps"] <= 1.0001
        opt = kCGWindowImageNominalResolution if nominal else kCGWindowImageDefault
        if below:
            img = CGWindowListCreateImage(rect, kCGWindowListOptionOnScreenBelowWindow, below, opt)
        else:
            img = CGWindowListCreateImage(rect, kCGWindowListOptionOnScreenOnly, 0, opt)
        if not img:
            raise OSError("截屏失败（屏幕录制权限没开，或者屏幕已锁定）")
        try:
            return self._read(img)
        finally:
            CFRelease(img)

    @staticmethod
    def _read(img):
        import numpy as np
        w, h = CGImageGetWidth(img), CGImageGetHeight(img)
        bpr, bpp, info = CGImageGetBytesPerRow(img), CGImageGetBitsPerPixel(img), CGImageGetBitmapInfo(img)
        # 内存里是 B G R A 的 32 位格式（绝大多数情况）就直接用：小端 + alpha 在“前”（ARGB 字）
        if bpp == 32 and (info & 0x7000) == kCGBitmapByteOrder32Little and (info & 0x1F) in (2, 4, 6):
            data = CGDataProviderCopyData(CGImageGetDataProvider(img))
            if not data:
                raise OSError("读取截屏数据失败")
            try:
                n = CFDataGetLength(data)
                buf = (ctypes.c_uint8 * n).from_address(CFDataGetBytePtr(data))
                rows = np.frombuffer(buf, np.uint32, count=h * (bpr // 4)).reshape(h, bpr // 4)
                arr = rows[:, :w].copy()  # 必须拷一份（顺带去掉行尾对齐的空白）：下面就要释放这块内存了
            finally:
                CFRelease(data)
            return Shot(w, h, arr)
        # 少见的像素格式（HDR / 10 位色等）：画到一块 BGRA 8 位的画布上再取
        cs = CGColorSpaceCreateDeviceRGB()
        ctx = CGBitmapContextCreate(None, w, h, 8, w * 4, cs, kCGImageAlphaPremultipliedFirst | kCGBitmapByteOrder32Little)
        CGColorSpaceRelease(cs)
        if not ctx:
            raise OSError("不支持的截屏像素格式 bpp=%d info=%#x" % (bpp, info))
        try:
            CGContextDrawImage(ctx, CGRect(CGPoint(0, 0), CGSize(w, h)), img)
            buf = (ctypes.c_uint8 * (w * h * 4)).from_address(CGBitmapContextGetData(ctx))
            arr = np.frombuffer(buf, np.uint32).reshape(h, w).copy()
        finally:
            CGContextRelease(ctx)
        return Shot(w, h, arr)


# ====================================================================== 注入：公共部分
_src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
try:
    Quartz.CGEventSourceSetUserData(_src, MAGIC)
except Exception:  # noqa: BLE001  老版本 PyObjC 没有这个函数也没关系，每个事件上还会再打一次
    pass
_lock = threading.RLock()


def _post(ev):
    Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGEventSourceUserData, MAGIC)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


# 修饰键：虚拟键码 -> (通用标志位, 区分左右的设备标志位)
_MODS = {
    0x38: (0x020000, 0x000002), 0x3C: (0x020000, 0x000004),  # Shift 左/右
    0x3B: (0x040000, 0x000001), 0x3E: (0x040000, 0x002000),  # Control
    0x3A: (0x080000, 0x000020), 0x3D: (0x080000, 0x000040),  # Option
    0x37: (0x100000, 0x000008), 0x36: (0x100000, 0x000010),  # Command
}
_held = set()      # 本程序按下没松的修饰键
_buttons = set()   # 按住没松的鼠标键（iPad 编号：0 左 1 中 2 右）


def _flags():
    f = 0
    for kc in _held:
        g, d = _MODS[kc]
        f |= g | d
    return f


# ====================================================================== 鼠标
_BTN = {0: (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGEventLeftMouseDragged,
            Quartz.kCGMouseButtonLeft),
        1: (Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp, Quartz.kCGEventOtherMouseDragged,
            Quartz.kCGMouseButtonCenter),
        2: (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGEventRightMouseDragged,
            Quartz.kCGMouseButtonRight)}
_pos = {"x": None, "y": None, "t": 0.0}
_clicks = {"b": -1, "t": 0.0, "x": 0.0, "y": 0.0, "n": 0}
_down_n = {}
try:
    _DCLICK = float(NSEvent.doubleClickInterval()) or 0.5
except Exception:  # noqa: BLE001
    _DCLICK = 0.5


def cursor_pos():
    ev = Quartz.CGEventCreate(None)
    p = Quartz.CGEventGetLocation(ev)
    return p.x, p.y


def _cur():
    """当前光标位置。刚注入过移动就用自己记的（系统处理事件是异步的，马上去读可能还是旧位置）。"""
    if _pos["x"] is not None and time.monotonic() - _pos["t"] < 0.3:
        return _pos["x"], _pos["y"]
    return cursor_pos()


def move_to(x, y):
    """移动到全局坐标 (x, y)（点，可以带小数）。"""
    with _lock:
        x, y = _clamp(float(x), float(y))
        ox, oy = _cur()
        held = min(_buttons) if _buttons else None  # 按着键移动在 macOS 上是“拖拽”事件，不是移动
        if held is None:
            t, btn = Quartz.kCGEventMouseMoved, Quartz.kCGMouseButtonLeft
        else:
            t, btn = _BTN[held][2], _BTN[held][3]
        ev = Quartz.CGEventCreateMouseEvent(_src, t, (x, y), btn)
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventDeltaX, int(round(x - ox)))
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventDeltaY, int(round(y - oy)))
        Quartz.CGEventSetFlags(ev, _flags())
        _post(ev)
        _pos.update(x=x, y=y, t=time.monotonic())


def move_rel(dx, dy):
    """相对移动，dx/dy 是当前显示器上的像素。"""
    with _lock:
        x, y = _cur()
        s = _scale_at(x, y)
        move_to(x + dx / s, y + dy / s)


def _click_count(b, x, y):
    """macOS 的双击要由发事件的一方自己数：第二下的“点击次数”字段得是 2，系统不会替你判断。"""
    now = time.monotonic()
    c = _clicks
    if c["b"] == b and now - c["t"] < _DCLICK and abs(x - c["x"]) <= 5 and abs(y - c["y"]) <= 5:
        c["n"] = c["n"] % 3 + 1
    else:
        c["n"] = 1
    c.update(b=b, t=now, x=x, y=y)
    return c["n"]


def button(b, down):
    with _lock:
        b = int(b) if int(b) in _BTN else 0
        x, y = _cur()
        if down:
            n = _click_count(b, x, y)
            _down_n[b] = n
            _buttons.add(b)
        else:
            n = _down_n.pop(b, 1)
            _buttons.discard(b)
        d, u, _, btn = _BTN[b]
        ev = Quartz.CGEventCreateMouseEvent(_src, d if down else u, (x, y), btn)
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, n)
        Quartz.CGEventSetFlags(ev, _flags())
        _post(ev)


def click(b=0, n=1):
    for _ in range(max(1, min(int(n), 3))):
        button(b, True)
        button(b, False)


_wfrac = [0.0, 0.0]


def wheel(dx=0, dy=0):
    """滚动。iPad 发来的单位是“像素”（Windows 上直接当滚轮刻度用，1:1），这里换成点、按像素精确滚动。

    方向和 Windows 一致：dy > 0 = 往上滚（内容往下走）；dx > 0 = 往右滚（内容往左走）。
    Quartz 的竖直分量正数是往上，水平分量正数是往左，所以水平取反。
    """
    with _lock:
        s = _scale_at(*_cur())
        vy = dy / s + _wfrac[1]
        vx = -dx / s + _wfrac[0]
        iy, ix = int(vy), int(vx)
        _wfrac[0], _wfrac[1] = vx - ix, vy - iy
        if not (ix or iy):
            return
        ev = Quartz.CGEventCreateScrollWheelEvent(_src, Quartz.kCGScrollEventUnitPixel, 2, iy, ix)
        # 标成“连续”滚动（和触控板一样），网页、文档会按像素平滑滚，而不是一格一格跳
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGScrollWheelEventIsContinuous, 1)
        Quartz.CGEventSetFlags(ev, _flags())
        _post(ev)


# ====================================================================== 键盘
# KeyboardEvent.code -> macOS 虚拟键码（Carbon 的 kVK_*）
CODE_KC = {
    "Enter": 0x24, "NumpadEnter": 0x4C, "Escape": 0x35, "Backspace": 0x33, "Tab": 0x30, "Space": 0x31,
    "ShiftLeft": 0x38, "ShiftRight": 0x3C, "ControlLeft": 0x3B, "ControlRight": 0x3E,
    "AltLeft": 0x3A, "AltRight": 0x3D, "MetaLeft": 0x37, "MetaRight": 0x36, "OSLeft": 0x37, "OSRight": 0x36,
    "CapsLock": 0x39, "Insert": 0x72, "Help": 0x72, "Delete": 0x75, "Home": 0x73, "End": 0x77,
    "PageUp": 0x74, "PageDown": 0x79, "ArrowLeft": 0x7B, "ArrowRight": 0x7C, "ArrowDown": 0x7D, "ArrowUp": 0x7E,
    "Minus": 0x1B, "Equal": 0x18, "BracketLeft": 0x21, "BracketRight": 0x1E, "Backslash": 0x2A,
    "Semicolon": 0x29, "Quote": 0x27, "Backquote": 0x32, "Comma": 0x2B, "Period": 0x2F, "Slash": 0x2C,
    "IntlBackslash": 0x0A, "IntlYen": 0x5D, "IntlRo": 0x5E, "NumLock": 0x47,
    "NumpadMultiply": 0x43, "NumpadAdd": 0x45, "NumpadSubtract": 0x4E, "NumpadDecimal": 0x41,
    "NumpadDivide": 0x4B, "NumpadEqual": 0x51,
    "KeyA": 0x00, "KeyS": 0x01, "KeyD": 0x02, "KeyF": 0x03, "KeyH": 0x04, "KeyG": 0x05, "KeyZ": 0x06,
    "KeyX": 0x07, "KeyC": 0x08, "KeyV": 0x09, "KeyB": 0x0B, "KeyQ": 0x0C, "KeyW": 0x0D, "KeyE": 0x0E,
    "KeyR": 0x0F, "KeyY": 0x10, "KeyT": 0x11, "KeyO": 0x1F, "KeyU": 0x20, "KeyI": 0x22, "KeyP": 0x23,
    "KeyL": 0x25, "KeyJ": 0x26, "KeyK": 0x28, "KeyN": 0x2D, "KeyM": 0x2E,
    "Digit1": 0x12, "Digit2": 0x13, "Digit3": 0x14, "Digit4": 0x15, "Digit6": 0x16, "Digit5": 0x17,
    "Digit9": 0x19, "Digit7": 0x1A, "Digit8": 0x1C, "Digit0": 0x1D,
    "Numpad0": 0x52, "Numpad1": 0x53, "Numpad2": 0x54, "Numpad3": 0x55, "Numpad4": 0x56,
    "Numpad5": 0x57, "Numpad6": 0x58, "Numpad7": 0x59, "Numpad8": 0x5B, "Numpad9": 0x5C,
    "F1": 0x7A, "F2": 0x78, "F3": 0x63, "F4": 0x76, "F5": 0x60, "F6": 0x61, "F7": 0x62, "F8": 0x64,
    "F9": 0x65, "F10": 0x6D, "F11": 0x67, "F12": 0x6F, "F13": 0x69, "F14": 0x6B, "F15": 0x71,
    "F16": 0x6A, "F17": 0x40, "F18": 0x4F, "F19": 0x50, "F20": 0x5A,
}
_FN, _PAD = 0x800000, 0x200000  # kCGEventFlagMaskSecondaryFn / NumericPad
# 真键盘按这些键时系统会带上 Fn / 小键盘标志。系统快捷键也是按带着这些标志来匹配的：
# 比如“调度中心 = ⌃↑”实际登记的是 Control+Fn+↑，不带 Fn 发 ⌃↑ 不会触发。
_EXTRA = {kc: _FN | _PAD for kc in (0x7B, 0x7C, 0x7D, 0x7E)}
_EXTRA.update({kc: _FN for kc in (0x72, 0x73, 0x74, 0x75, 0x77, 0x79, 0x7A, 0x78, 0x63, 0x76, 0x60, 0x61,
                                   0x62, 0x64, 0x65, 0x6D, 0x67, 0x6F, 0x69, 0x6B, 0x71, 0x6A, 0x40, 0x4F,
                                   0x50, 0x5A)})
_EXTRA.update({kc: _PAD for kc in (0x41, 0x43, 0x45, 0x47, 0x4B, 0x4C, 0x4E, 0x51, 0x52, 0x53, 0x54, 0x55,
                                    0x56, 0x57, 0x58, 0x59, 0x5B, 0x5C)})
# 音量、播放这类“媒体键”在 macOS 上不是普通按键，是 NX_SYSDEFINED 系统事件
MEDIA = {"AudioVolumeUp": 0, "AudioVolumeDown": 1, "AudioVolumeMute": 7, "MediaPlayPause": 16,
         "MediaTrackNext": 17, "MediaTrackPrevious": 18, "MediaFastForward": 19, "MediaRewind": 20}


def _media(nx, down):
    with objc.autorelease_pool():
        ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0, 0), 0xA00 if down else 0xB00, 0, 0, None, 8, (nx << 16) | ((0xA if down else 0xB) << 8), -1)
        _post(ev.CGEvent())


def _key_event(kc, down):
    if kc in _MODS:
        if down:
            _held.add(kc)
        else:
            _held.discard(kc)
        ev = Quartz.CGEventCreateKeyboardEvent(_src, kc, down)
        Quartz.CGEventSetType(ev, Quartz.kCGEventFlagsChanged)
        Quartz.CGEventSetFlags(ev, _flags())
    else:
        ev = Quartz.CGEventCreateKeyboardEvent(_src, kc, down)
        Quartz.CGEventSetFlags(ev, _flags() | _EXTRA.get(kc, 0))
    _post(ev)


def _press(code, down):
    if code in MEDIA:
        _media(MEDIA[code], down)
        return True
    kc = CODE_KC.get(code)
    if kc is None:
        return False
    _key_event(kc, down)
    return True


def key(code, down):
    with _lock:
        return _press(code, down)


def combo(codes):
    """依次按下再逆序抬起，例如 ["MetaLeft", "KeyC"] = ⌘C。"""
    with _lock:
        items = [c for c in codes if c in CODE_KC or c in MEDIA]
        for c in items:
            _press(c, True)
        for c in reversed(items):
            _press(c, False)


def _unicode(ch):
    n = len(ch.encode("utf-16-le")) // 2
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(_src, 0, down)
        Quartz.CGEventKeyboardSetUnicodeString(ev, n, ch)
        Quartz.CGEventSetFlags(ev, 0)
        _post(ev)


# ---------------------------------------------------------------- 输入法
# Mac 上当前是拼音之类的输入法时，注入的按键会先被输入法截住（英文进了候选框，汉字也可能被吞掉）。
# 打字前临时切到英文键盘（ABC），远程不用了再切回原来的输入法。iPad 那边自己有输入法，Mac 这边用不着。
_hit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
TISCopyCurrentKeyboardInputSource = _sig(_hit, "TISCopyCurrentKeyboardInputSource", [], _vp)
TISCopyCurrentASCIICapableKeyboardLayoutInputSource = _sig(
    _hit, "TISCopyCurrentASCIICapableKeyboardLayoutInputSource", [], _vp)
TISSelectInputSource = _sig(_hit, "TISSelectInputSource", [_vp], ctypes.c_int32)
TISGetInputSourceProperty = _sig(_hit, "TISGetInputSourceProperty", [_vp, _vp], _vp)
CFEqual = _sig(_cf, "CFEqual", [_vp, _vp], ctypes.c_bool)
CFRetain = _sig(_cf, "CFRetain", [_vp], _vp)


def _cfconst(lib, name):
    try:
        return _vp.in_dll(lib, name).value
    except ValueError:
        return None


kTISPropertyInputSourceType = _cfconst(_hit, "kTISPropertyInputSourceType")
kTISPropertyInputSourceID = _cfconst(_hit, "kTISPropertyInputSourceID")
kTISTypeKeyboardLayout = _cfconst(_hit, "kTISTypeKeyboardLayout")
_ime = {"saved": None}   # 切走之前的输入法（TISInputSourceRef，自己持有一份引用）


def input_source_id():
    """当前输入法的编号，比如 com.apple.keylayout.ABC、com.apple.inputmethod.SCIM.ITABC（日志和测试用）。"""
    src = TISCopyCurrentKeyboardInputSource() if TISCopyCurrentKeyboardInputSource else None
    if not src:
        return ""
    try:
        v = TISGetInputSourceProperty(src, kTISPropertyInputSourceID)
        return str(objc.objc_object(c_void_p=ctypes.c_void_p(v))) if v else ""
    finally:
        CFRelease(src)


def _ensure_ascii_input():
    """当前是输入法（不是普通的键盘布局）就切到英文键盘。切了返回 True。"""
    if not (TISCopyCurrentKeyboardInputSource and kTISTypeKeyboardLayout and kTISPropertyInputSourceType):
        return False
    cur = TISCopyCurrentKeyboardInputSource()
    if not cur:
        return False
    try:
        typ = TISGetInputSourceProperty(cur, kTISPropertyInputSourceType)
        if not typ or CFEqual(typ, kTISTypeKeyboardLayout):
            return False
        ascii_ = TISCopyCurrentASCIICapableKeyboardLayoutInputSource()
        if not ascii_:
            return False
        try:
            if TISSelectInputSource(ascii_) != 0:
                return False
        finally:
            CFRelease(ascii_)
        if _ime["saved"] is None:
            _ime["saved"] = CFRetain(cur)
            logging.getLogger("rd.input").info("远程打字：Mac 的输入法临时切到英文键盘（远程不用了再切回来）")
        return True
    finally:
        CFRelease(cur)


def _ascii_for_typing():
    """打字前切英文键盘。有状态窗口进程时交给它做：它跑着 run loop，看到的输入法状态是最新的；
    服务进程自己读会读到旧的（实测 macOS 15：本机换成拼音以后，服务进程还以为是原来的英文键盘）。"""
    import macipc
    if macipc.active():
        rep = macipc.request({"t": "ime_ascii"}, timeout=0.5)
        return bool(rep and rep.get("switched"))
    return _ensure_ascii_input()


def restore_input_source():
    """远程不用了：把打字前切走的输入法切回来（本机的人回来还是熟悉的拼音）。"""
    import macipc
    if macipc.active():
        macipc.send({"t": "ime_restore"})
        return
    with _lock:
        src, _ime["saved"] = _ime["saved"], None
        if src:
            try:
                logging.getLogger("rd.input").info("远程不用了，输入法切回原来的 (%s)", TISSelectInputSource(src))
            finally:
                CFRelease(src)


def type_text(text):
    """直接输入任意文字（包括中文、表情）：每个字符一对按键事件，字符本身写在事件里，不经过键位。"""
    with _lock:
        try:
            if _ascii_for_typing():
                time.sleep(0.06)  # 等输入法切换生效，不然头几个字还会进候选框
        except Exception:  # noqa: BLE001  切不了就照常打
            pass
        for i, ch in enumerate(text.replace("\r\n", "\n")):
            if ch == "\r":
                continue
            if ch == "\n":
                _key_event(0x24, True)
                _key_event(0x24, False)
            elif ch == "\t":
                _key_event(0x30, True)
                _key_event(0x30, False)
            else:
                _unicode(ch)
            if i % 40 == 39:
                time.sleep(0.003)  # 长文本给目标程序一点喘气的时间，免得吞字


def release_modifiers():
    """断线时防止 ⌘/⌥/⌃/⇧ 或者鼠标键卡在按下状态。"""
    with _lock:
        for b in list(_buttons):
            button(b, False)
        for kc in list(_held):
            _key_event(kc, False)
        # 系统那边还记着按下的修饰键（比如断线时正好按着）：补一个“全部松开”
        f = Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateHIDSystemState)
        for kc, (g, _) in ((0x38, (0x020000, 0)), (0x3B, (0x040000, 0)), (0x3A, (0x080000, 0)),
                           (0x37, (0x100000, 0))):
            if f & g:
                ev = Quartz.CGEventCreateKeyboardEvent(_src, kc, False)
                Quartz.CGEventSetType(ev, Quartz.kCGEventFlagsChanged)
                Quartz.CGEventSetFlags(ev, 0)
                _post(ev)


# ====================================================================== 光标
class _CursorWatch:
    """后台每 0.12 秒看一眼当前系统光标长什么样。

    NSCursor.currentSystemCursor() 不便宜（要从窗口服务器取图），不能像位置那样 25 毫秒问一次；
    这里把“形状”换算成一个编号，cursor_info() 只返回编号，形状变了编号才变，server 才会去取图。
    """

    def __init__(self):
        self.key = 0
        self.shapes = {}
        self.used = 0.0
        self.thread = None

    def current(self):
        self.used = time.monotonic()
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, name="cursor", daemon=True)
            self.thread.start()
            try:
                self._poll()
            except Exception:  # noqa: BLE001
                pass
        return self.key

    def _run(self):
        while time.monotonic() - self.used < 10:  # 没人问了（没有 iPad 连着）就停
            try:
                self._poll()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.12)

    def _poll(self):
        x, y = cursor_pos()
        ps = _scale_at(x, y)
        with objc.autorelease_pool():
            c = None
            try:
                c = NSCursor.currentSystemCursor()
            except Exception:  # noqa: BLE001
                pass
            c = c or NSCursor.arrowCursor()
            img, hs = c.image(), c.hotSpot()
            size = img.size()
            tiff = bytes(img.TIFFRepresentation() or b"")
        key = zlib.crc32(tiff) ^ (int(hs.x * 8) << 4) ^ (int(hs.y * 8) << 14) ^ (int(ps * 4) << 26)
        key &= 0x7FFFFFFF
        if key not in self.shapes:
            if len(self.shapes) > 64:
                self.shapes.clear()
            self.shapes[key] = _render_cursor(tiff, size.width, size.height, hs.x, hs.y, ps)
        self.key = key


def _arrow_png():
    """取不到系统光标时用的箭头（保证 iPad 上总有个指针看）。"""
    from PIL import Image, ImageDraw
    k = 4
    im = Image.new("RGBA", (17 * k, 25 * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    pts = [(1, 1), (1, 18), (5.5, 14), (8.5, 21), (11, 20), (8, 13.5), (14, 13.5)]
    d.polygon([(x * k, y * k) for x, y in pts], fill=(0, 0, 0, 255), outline=(255, 255, 255, 255))
    d.line([(x * k, y * k) for x, y in pts + pts[:1]], fill=(255, 255, 255, 255), width=k)
    return im, 1 * k, 1 * k, k


def _render_cursor(tiff, w_pt, h_pt, hx_pt, hy_pt, ps):
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(tiff))
        best, n = None, getattr(im, "n_frames", 1)
        for i in range(n):  # TIFF 里可能同时有 1x 和 2x 两张，取最大的
            im.seek(i)
            if best is None or im.width > best.width:
                best = im.convert("RGBA")
        im = best
        f = im.width / w_pt if w_pt else 1.0  # 图里每点几个像素
        hx, hy = hx_pt * f, hy_pt * f
    except Exception:  # noqa: BLE001
        im, hx, hy, f = _arrow_png()
    box = im.getchannel("A").getbbox()
    if not box:
        return None
    x0, y0, x1, y1 = box
    im = im.crop(box)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    k = ps / f  # 图片像素 → 当前显示器像素
    return {"png": base64.b64encode(buf.getvalue()).decode(), "hx": (hx - x0) * k, "hy": (hy - y0) * k,
            "w": (x1 - x0) * k, "h": (y1 - y0) * k}


_cursor = _CursorWatch()


def cursor_info():
    """返回 (是否可见, x, y, 形状编号)，坐标是 Quartz 全局坐标（点）。"""
    x, y = cursor_pos()
    vis = True
    if CGCursorIsVisible is not None:
        try:
            vis = bool(CGCursorIsVisible())
        except Exception:  # noqa: BLE001
            pass
    return vis, x, y, _cursor.current()


def cursor_shape(h):
    return _cursor.shapes.get(h)


# ====================================================================== 剪贴板 / 打开网址 / 打开程序
def clipboard_get():
    with objc.autorelease_pool():
        s = NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
        return str(s) if s is not None else ""


def clipboard_set(text):
    with objc.autorelease_pool():
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        return bool(pb.setString_forType_(text, NSPasteboardTypeString))


def open_url(url):
    # 网址里有中文或空格时 NSURL 会直接拒绝，先按 URL 规则转义（已经转义过的 % 原样保留）
    with objc.autorelease_pool():
        u = NSURL.URLWithString_(quote(url, safe=":/?#[]@!$&'()*+,;=%~"))
        if u is None or not NSWorkspace.sharedWorkspace().openURL_(u):
            raise OSError("系统没能打开这个网址")


APPS = {"activity": ("com.apple.ActivityMonitor", "活动监视器"), "finder": ("com.apple.finder", "访达")}


def launch_app(name):
    if name not in APPS:
        return "不支持打开这个程序"
    bid, label = APPS[name]
    r = subprocess.run(["/usr/bin/open", "-b", bid], capture_output=True)
    return ("已在 Mac 上打开" if r.returncode == 0 else "没能打开") + label


_host = None


def host_name():
    """“电脑名称”（系统设置 → 通用 → 关于本机 里的那个，比如“小明的 MacBook Air”）。"""
    global _host
    if _host is None:
        try:
            _host = subprocess.run(["/usr/sbin/scutil", "--get", "ComputerName"], capture_output=True,
                                   timeout=3).stdout.decode("utf-8").strip()
        except Exception:  # noqa: BLE001
            _host = ""
        if not _host:
            import socket
            _host = socket.gethostname()
    return _host


# ====================================================================== 权限 / 防休眠 / 锁屏
def _macos_major():
    import platform
    try:
        return int(platform.mac_ver()[0].split(".")[0])
    except (ValueError, IndexError):
        return 15


# macOS 13 起叫「系统设置 → 隐私与安全性」，12 及以前叫「系统偏好设置 → 安全性与隐私 → 隐私」
PRIVACY_PATH = "系统设置 → 隐私与安全性" if _macos_major() >= 13 else "系统偏好设置 → 安全性与隐私 → 隐私"


def screen_capture_allowed():
    try:
        return bool(CGPreflightScreenCaptureAccess())
    except Exception:  # noqa: BLE001
        return True


def accessibility_allowed():
    try:
        return bool(AXIsProcessTrusted())
    except Exception:  # noqa: BLE001
        return True


def permission_warnings():
    out = []
    if not screen_capture_allowed():
        out.append("这台 Mac 还没给「iPad 远程桌面」开「屏幕录制」权限，只能看到桌面背景。"
                   "请在 Mac 上打开：" + PRIVACY_PATH + " → 屏幕录制")
    if not accessibility_allowed():
        out.append("这台 Mac 还没给「iPad 远程桌面」开「辅助功能」权限，点击和打字不会生效。"
                   "请在 Mac 上打开：" + PRIVACY_PATH + " → 辅助功能")
    return out


_activity = {"token": None, "display": None}


def keep_awake(display=True):
    """阻止系统睡眠（以及可选的关屏），顺带让系统别把本进程当成“闲着”而降速（App Nap）。"""
    if _activity["token"] is not None and _activity["display"] == display:
        return
    info = NSProcessInfo.processInfo()
    if _activity["token"] is not None:
        info.endActivity_(_activity["token"])
    opts = 0x00FFFFFF | (1 << 20) | 0xFF00000000  # UserInitiated + 禁止闲置睡眠 + LatencyCritical
    if display:
        opts |= 1 << 40  # 禁止闲置关屏
    _activity["token"] = info.beginActivityWithOptions_reason_(opts, "iPad 远程桌面正在运行")
    _activity["display"] = display


def is_session_locked():
    """锁屏、或者切到了别的用户（快速用户切换）。"""
    with objc.autorelease_pool():
        d = Quartz.CGSessionCopyCurrentDictionary()
        if not d:
            return False
        return bool(d.get("CGSSessionScreenIsLocked", 0)) or not d.get("kCGSSessionOnConsoleKey", True)
