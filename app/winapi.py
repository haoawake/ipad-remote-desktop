"""Windows 底层接口：DPI、鼠标键盘注入、光标形状、剪贴板、防休眠。全部用 ctypes，无需 pywin32。"""
import base64
import ctypes
import io
import time
from ctypes import wintypes as W

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t


# ---------------------------------------------------------------- DPI / 屏幕
def set_dpi_aware():
    """必须在任何截图/窗口操作之前调用，保证拿到的是物理像素坐标。"""
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = W.BOOL
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()


def virtual_screen():
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


# ---------------------------------------------------------------- SendInput
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD),
                ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", W.DWORD), ("wParamL", W.WORD), ("wParamH", W.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", W.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [W.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = W.UINT
user32.MapVirtualKeyW.argtypes = [W.UINT, W.UINT]
user32.MapVirtualKeyW.restype = W.UINT
user32.GetCursorPos.argtypes = [ctypes.POINTER(W.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x4000, 0x8000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4

_BTN = {0: (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        1: (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        2: (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)}


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse(flags, dx=0, dy=0, data=0):
    return INPUT(type=0, u=_INPUTUNION(mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)))


def _key(vk=0, scan=0, flags=0):
    return INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(vk, scan, flags, 0, 0)))


def cursor_pos():
    p = W.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def move_to(x, y):
    """移动到虚拟桌面物理像素坐标 (x, y)。"""
    vx, vy, vw, vh = virtual_screen()
    x = min(max(int(round(x)), vx), vx + vw - 1)
    y = min(max(int(round(y)), vy), vy + vh - 1)
    nx = int(((x - vx) * 65536 + vw // 2) // vw)
    ny = int(((y - vy) * 65536 + vh // 2) // vh)
    _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny))
    if cursor_pos() != (x, y):  # 极少数情况下归一化有 1px 误差，补一刀
        user32.SetCursorPos(x, y)


def move_rel(dx, dy):
    x, y = cursor_pos()
    move_to(x + dx, y + dy)


def button(b, down):
    flags = _BTN.get(int(b), _BTN[0])[0 if down else 1]
    _send(_mouse(flags))


def click(b=0, n=1):
    d, u = _BTN.get(int(b), _BTN[0])
    for _ in range(max(1, min(int(n), 3))):
        _send(_mouse(d), _mouse(u))


def wheel(dx=0, dy=0):
    ins = []
    if dy:
        ins.append(_mouse(MOUSEEVENTF_WHEEL, data=int(dy)))
    if dx:
        ins.append(_mouse(MOUSEEVENTF_HWHEEL, data=int(dx)))
    if ins:
        _send(*ins)


# KeyboardEvent.code -> (VK, 是否扩展键)
_EXT = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D,
        0x6F, 0x90, 0xA3, 0xA5, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}
CODE_VK = {
    "Enter": 0x0D, "NumpadEnter": 0x0D, "Escape": 0x1B, "Backspace": 0x08, "Tab": 0x09, "Space": 0x20,
    "ShiftLeft": 0xA0, "ShiftRight": 0xA1, "ControlLeft": 0xA2, "ControlRight": 0xA3,
    "AltLeft": 0xA4, "AltRight": 0xA5, "MetaLeft": 0x5B, "MetaRight": 0x5C, "OSLeft": 0x5B, "OSRight": 0x5C,
    "ContextMenu": 0x5D, "CapsLock": 0x14, "NumLock": 0x90, "ScrollLock": 0x91, "Pause": 0x13,
    "PrintScreen": 0x2C, "ArrowLeft": 0x25, "ArrowUp": 0x26, "ArrowRight": 0x27, "ArrowDown": 0x28,
    "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
    "Minus": 0xBD, "Equal": 0xBB, "BracketLeft": 0xDB, "BracketRight": 0xDD, "Backslash": 0xDC,
    "Semicolon": 0xBA, "Quote": 0xDE, "Backquote": 0xC0, "Comma": 0xBC, "Period": 0xBE, "Slash": 0xBF,
    "IntlBackslash": 0xE2, "NumpadMultiply": 0x6A, "NumpadAdd": 0x6B, "NumpadSubtract": 0x6D,
    "NumpadDecimal": 0x6E, "NumpadDivide": 0x6F,
    "AudioVolumeMute": 0xAD, "AudioVolumeDown": 0xAE, "AudioVolumeUp": 0xAF,
    "MediaTrackNext": 0xB0, "MediaTrackPrevious": 0xB1, "MediaStop": 0xB2, "MediaPlayPause": 0xB3,
}
for _i in range(26):
    CODE_VK["Key" + chr(65 + _i)] = 0x41 + _i
for _i in range(10):
    CODE_VK["Digit%d" % _i] = 0x30 + _i
    CODE_VK["Numpad%d" % _i] = 0x60 + _i
for _i in range(1, 25):
    CODE_VK["F%d" % _i] = 0x6F + _i


def _vk_input(vk, up, code=""):
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in _EXT or code == "NumpadEnter":
        flags |= KEYEVENTF_EXTENDEDKEY
    return _key(vk, scan, flags)


def key(code, down):
    vk = CODE_VK.get(code)
    if vk is None:
        return False
    _send(_vk_input(vk, not down, code))
    return True


def combo(codes):
    """依次按下再逆序抬起，例如 ["ControlLeft", "KeyC"]。"""
    vks = [(CODE_VK[c], c) for c in codes if c in CODE_VK]
    if not vks:
        return
    seq = [_vk_input(vk, False, c) for vk, c in vks] + [_vk_input(vk, True, c) for vk, c in reversed(vks)]
    _send(*seq)


def type_text(text):
    seq = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\r":
            continue
        if ch == "\n":
            seq += [_vk_input(0x0D, False), _vk_input(0x0D, True)]
        elif ch == "\t":
            seq += [_vk_input(0x09, False), _vk_input(0x09, True)]
        else:
            b = ch.encode("utf-16-le")
            for i in range(0, len(b), 2):
                cu = b[i] | (b[i + 1] << 8)
                seq += [_key(0, cu, KEYEVENTF_UNICODE), _key(0, cu, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    for i in range(0, len(seq), 200):
        _send(*seq[i:i + 200])


def release_modifiers():
    """断线时防止 Ctrl/Shift/Alt/Win 卡在按下状态。"""
    user32.GetAsyncKeyState.restype = ctypes.c_short
    ups = [_vk_input(vk, True) for vk in (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C)
           if user32.GetAsyncKeyState(vk) & 0x8000]
    for vk, (d, u) in ((0x01, _BTN[0]), (0x02, _BTN[2]), (0x04, _BTN[1])):
        if user32.GetAsyncKeyState(vk) & 0x8000:
            ups.append(_mouse(u))
    if ups:
        _send(*ups)


# ---------------------------------------------------------------- 光标
class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("flags", W.DWORD), ("hCursor", W.HANDLE), ("ptScreenPos", W.POINT)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", W.BOOL), ("xHotspot", W.DWORD), ("yHotspot", W.DWORD),
                ("hbmMask", W.HBITMAP), ("hbmColor", W.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", W.LONG), ("bmWidth", W.LONG), ("bmHeight", W.LONG), ("bmWidthBytes", W.LONG),
                ("bmPlanes", W.WORD), ("bmBitsPixel", W.WORD), ("bmBits", ctypes.c_void_p)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                ("biClrImportant", W.DWORD)]


user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
user32.GetIconInfo.argtypes = [W.HANDLE, ctypes.POINTER(ICONINFO)]
user32.DrawIconEx.argtypes = [W.HDC, ctypes.c_int, ctypes.c_int, W.HANDLE, ctypes.c_int, ctypes.c_int,
                              W.UINT, W.HBRUSH, W.UINT]
user32.GetDC.argtypes = [W.HWND]
user32.GetDC.restype = W.HDC
user32.ReleaseDC.argtypes = [W.HWND, W.HDC]
gdi32.CreateCompatibleDC.argtypes = [W.HDC]
gdi32.CreateCompatibleDC.restype = W.HDC
gdi32.CreateDIBSection.argtypes = [W.HDC, ctypes.c_void_p, W.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   W.HANDLE, W.DWORD]
gdi32.CreateDIBSection.restype = W.HBITMAP
gdi32.SelectObject.argtypes = [W.HDC, W.HGDIOBJ]
gdi32.SelectObject.restype = W.HGDIOBJ
gdi32.DeleteObject.argtypes = [W.HGDIOBJ]
gdi32.DeleteDC.argtypes = [W.HDC]
gdi32.GetObjectW.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p]
gdi32.PatBlt.argtypes = [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.DWORD]


def cursor_info():
    """返回 (是否可见, x, y, 句柄)。"""
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    if not user32.GetCursorInfo(ctypes.byref(ci)):
        x, y = cursor_pos()
        return True, x, y, 0
    return bool(ci.flags & 1), ci.ptScreenPos.x, ci.ptScreenPos.y, ci.hCursor or 0


def _render_cursor(hcur, w, h, white):
    hdc_screen = user32.GetDC(None)
    hdc = gdi32.CreateCompatibleDC(hdc_screen)
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth, bmi.biHeight, bmi.biPlanes, bmi.biBitCount = w, -h, 1, 32
    bits = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(hdc, hbm)
    gdi32.PatBlt(hdc, 0, 0, w, h, 0x00FF0062 if white else 0x00000042)  # WHITENESS / BLACKNESS
    user32.DrawIconEx(hdc, 0, 0, hcur, w, h, 0, None, 0x0003)  # DI_NORMAL
    data = ctypes.string_at(bits, w * h * 4)
    gdi32.SelectObject(hdc, old)
    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(None, hdc_screen)
    return data


def cursor_shape(hcur):
    """把光标渲染成带透明度的 PNG，返回 dict(png=base64, hx, hy, w, h)，失败返回 None。"""
    if not hcur:
        return None
    try:
        import numpy as np
        from PIL import Image
        ii = ICONINFO()
        if not user32.GetIconInfo(hcur, ctypes.byref(ii)):
            return None
        bm = BITMAP()
        try:
            src = ii.hbmColor or ii.hbmMask
            gdi32.GetObjectW(src, ctypes.sizeof(BITMAP), ctypes.byref(bm))
            w, h = bm.bmWidth, bm.bmHeight
            if not ii.hbmColor:
                h //= 2  # 单色光标：mask 上下两半
        finally:
            if ii.hbmMask:
                gdi32.DeleteObject(ii.hbmMask)
            if ii.hbmColor:
                gdi32.DeleteObject(ii.hbmColor)
        if w <= 0 or h <= 0 or w > 256 or h > 256:
            return None
        b = np.frombuffer(_render_cursor(hcur, w, h, False), np.uint8).reshape(h, w, 4)[..., :3].astype(np.int16)
        wt = np.frombuffer(_render_cursor(hcur, w, h, True), np.uint8).reshape(h, w, 4)[..., :3].astype(np.int16)
        alpha = 255 - (wt - b).max(axis=2)
        alpha = np.clip(alpha, 0, 255)
        # 反色区域（如 I 形光标）在黑底和白底下结果相反，按白色+黑描边近似处理
        inverted = (b.sum(axis=2) > 600) & (wt.sum(axis=2) < 150)
        a_safe = np.maximum(alpha, 1)[..., None]
        rgb = np.clip(b * 255 // a_safe, 0, 255)
        rgba = np.zeros((h, w, 4), np.uint8)
        rgba[..., :3] = rgb[..., ::-1]  # BGR -> RGB
        rgba[..., 3] = alpha
        if inverted.any():
            ring = np.zeros_like(inverted)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ring |= np.roll(np.roll(inverted, dy, 0), dx, 1)
            ring &= ~inverted & (alpha < 128)
            rgba[ring] = (0, 0, 0, 255)
            rgba[inverted] = (255, 255, 255, 255)
        # 裁掉全透明的空白边，减小体积
        ys, xs = np.nonzero(rgba[..., 3])
        if len(ys) == 0:
            return None
        x0, y0, x1, y1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
        img = Image.fromarray(rgba[y0:y1, x0:x1], "RGBA")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return {"png": base64.b64encode(buf.getvalue()).decode(), "hx": int(ii.xHotspot) - int(x0),
                "hy": int(ii.yHotspot) - int(y0), "w": int(x1 - x0), "h": int(y1 - y0)}
    except Exception:
        return None


# ---------------------------------------------------------------- 剪贴板
CF_UNICODETEXT = 13
user32.OpenClipboard.argtypes = [W.HWND]
user32.GetClipboardData.argtypes = [W.UINT]
user32.GetClipboardData.restype = W.HANDLE
user32.SetClipboardData.argtypes = [W.UINT, W.HANDLE]
user32.SetClipboardData.restype = W.HANDLE
kernel32.GlobalAlloc.argtypes = [W.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = W.HGLOBAL
kernel32.GlobalLock.argtypes = [W.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [W.HGLOBAL]
kernel32.GlobalFree.argtypes = [W.HGLOBAL]


def _open_clipboard():
    for _ in range(20):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def clipboard_get():
    if not _open_clipboard():
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = kernel32.GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def clipboard_set(text):
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            return False
        return True
    finally:
        user32.CloseClipboard()


# ---------------------------------------------------------------- 子进程随主进程退出
class _JOB_BASIC(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", W.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", W.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", W.DWORD), ("SchedulingClass", W.DWORD)]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")]


class _JOB_EXT(ctypes.Structure):
    _fields_ = [("Basic", _JOB_BASIC), ("Io", _IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, W.LPCWSTR]
kernel32.CreateJobObjectW.restype = W.HANDLE
kernel32.SetInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
kernel32.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
kernel32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
kernel32.OpenProcess.restype = W.HANDLE
kernel32.CloseHandle.argtypes = [W.HANDLE]
_job = None


def bind_child(pid):
    """把子进程放进一个 Job：本程序退出（哪怕是直接关窗口）时它会被系统一起结束。"""
    global _job
    try:
        if _job is None:
            _job = kernel32.CreateJobObjectW(None, None)
            info = _JOB_EXT()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            kernel32.SetInformationJobObject(_job, 9, ctypes.byref(info), ctypes.sizeof(info))
        h = kernel32.OpenProcess(0x0001 | 0x0100, False, pid)  # TERMINATE | SET_QUOTA
        if h:
            kernel32.AssignProcessToJobObject(_job, h)
            kernel32.CloseHandle(h)
    except Exception:
        pass


# ---------------------------------------------------------------- 防休眠
def keep_awake(display=True):
    """在调用线程上持续生效：阻止系统睡眠（以及可选的关屏）。"""
    flags = 0x80000000 | 0x00000001 | (0x00000002 if display else 0)
    kernel32.SetThreadExecutionState(flags)


def is_session_locked():
    """锁屏/切到安全桌面时，OpenInputDesktop 拿不到 Default 桌面。"""
    user32.OpenInputDesktop.restype = W.HANDLE
    h = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not h:
        return True
    try:
        buf = ctypes.create_unicode_buffer(256)
        n = W.DWORD()
        user32.GetUserObjectInformationW.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD,
                                                     ctypes.POINTER(W.DWORD)]
        if user32.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(n)):
            return buf.value.lower() != "default"
        return False
    finally:
        user32.CloseDesktop.argtypes = [W.HANDLE]
        user32.CloseDesktop(h)
