"""隐私屏：远程控制时，电脑本地屏幕显示“锁屏”画面，iPad 上照常看到真实桌面。

原理：
- 一个盖满整个屏幕的置顶窗口，设置了 WDA_EXCLUDEFROMCAPTURE：它只出现在物理显示器上，
  任何截屏（包括本程序自己的截屏）里都不存在。窗口鼠标穿透、不抢焦点，远程的点击/键盘照常落到下面的程序。
- 本地键盘鼠标用低级钩子屏蔽，只放行程序注入的输入（远程操作），防止家里有人乱动。
- 本地解除：没有远程连接时按 Enter → 进入 Windows 锁屏 → 用开机密码/PIN 解锁后，隐私屏自动消失
  （能解开 Windows 锁屏的人就是电脑主人）。Ctrl+Alt+Del 系统总是可用，也能从那里锁定再解锁。
"""
import ctypes
import logging
import threading
import time
from ctypes import wintypes as W

log = logging.getLogger("rd.privacy")
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT, WPARAM, LPARAM = ctypes.c_ssize_t, ctypes.c_size_t, ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, WPARAM, LPARAM)
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", W.UINT), ("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON), ("hCursor", W.HANDLE),
                ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR),
                ("hIconSm", W.HICON)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", W.HDC), ("fErase", W.BOOL), ("rcPaint", W.RECT), ("fRestore", W.BOOL),
                ("fIncUpdate", W.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", W.DWORD), ("scanCode", W.DWORD), ("flags", W.DWORD), ("time", W.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", W.POINT), ("mouseData", W.DWORD), ("flags", W.DWORD), ("time", W.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


def _sig(fn, args, res=None):
    fn.argtypes = args
    if res is not None:
        fn.restype = res


_sig(user32.DefWindowProcW, [W.HWND, W.UINT, WPARAM, LPARAM], LRESULT)
_sig(user32.RegisterClassExW, [ctypes.POINTER(WNDCLASSEXW)], W.ATOM)
_sig(user32.CreateWindowExW, [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID], W.HWND)
_sig(user32.SetLayeredWindowAttributes, [W.HWND, W.COLORREF, W.BYTE, W.DWORD], W.BOOL)
_sig(user32.SetWindowDisplayAffinity, [W.HWND, W.DWORD], W.BOOL)
_sig(user32.ShowWindow, [W.HWND, ctypes.c_int], W.BOOL)
_sig(user32.SetWindowPos, [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT], W.BOOL)
_sig(user32.PostMessageW, [W.HWND, W.UINT, WPARAM, LPARAM], W.BOOL)
_sig(user32.GetMessageW, [ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT], W.BOOL)
_sig(user32.TranslateMessage, [ctypes.POINTER(W.MSG)], W.BOOL)
_sig(user32.DispatchMessageW, [ctypes.POINTER(W.MSG)], LRESULT)
_sig(user32.SetTimer, [W.HWND, ctypes.c_size_t, W.UINT, ctypes.c_void_p], ctypes.c_size_t)
_sig(user32.BeginPaint, [W.HWND, ctypes.POINTER(PAINTSTRUCT)], W.HDC)
_sig(user32.EndPaint, [W.HWND, ctypes.POINTER(PAINTSTRUCT)], W.BOOL)
_sig(user32.GetClientRect, [W.HWND, ctypes.POINTER(W.RECT)], W.BOOL)
_sig(user32.FillRect, [W.HDC, ctypes.POINTER(W.RECT), W.HBRUSH], ctypes.c_int)
_sig(user32.DrawTextW, [W.HDC, W.LPCWSTR, ctypes.c_int, ctypes.POINTER(W.RECT), W.UINT], ctypes.c_int)
_sig(user32.InvalidateRect, [W.HWND, ctypes.c_void_p, W.BOOL], W.BOOL)
_sig(user32.SetWindowsHookExW, [ctypes.c_int, HOOKPROC, W.HINSTANCE, W.DWORD], W.HHOOK)
_sig(user32.UnhookWindowsHookEx, [W.HHOOK], W.BOOL)
_sig(user32.CallNextHookEx, [W.HHOOK, ctypes.c_int, WPARAM, LPARAM], LRESULT)
_sig(user32.GetSystemMetrics, [ctypes.c_int], ctypes.c_int)
_sig(kernel32.GetModuleHandleW, [W.LPCWSTR], W.HMODULE)
_sig(gdi32.CreateSolidBrush, [W.COLORREF], W.HBRUSH)
_sig(gdi32.CreatePen, [ctypes.c_int, ctypes.c_int, W.COLORREF], W.HPEN)
_sig(gdi32.CreateFontW, [ctypes.c_int] * 5 + [W.DWORD] * 8 + [W.LPCWSTR], W.HFONT)
_sig(gdi32.SelectObject, [W.HDC, W.HGDIOBJ], W.HGDIOBJ)
_sig(gdi32.DeleteObject, [W.HGDIOBJ], W.BOOL)
_sig(gdi32.GetStockObject, [ctypes.c_int], W.HGDIOBJ)
_sig(gdi32.SetTextColor, [W.HDC, W.COLORREF], W.COLORREF)
_sig(gdi32.SetBkMode, [W.HDC, ctypes.c_int], ctypes.c_int)
_sig(gdi32.RoundRect, [W.HDC] + [ctypes.c_int] * 6, W.BOOL)
_sig(gdi32.CreateCompatibleDC, [W.HDC], W.HDC)
_sig(gdi32.CreateCompatibleBitmap, [W.HDC, ctypes.c_int, ctypes.c_int], W.HBITMAP)
_sig(gdi32.BitBlt, [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HDC, ctypes.c_int,
                    ctypes.c_int, W.DWORD], W.BOOL)
_sig(gdi32.DeleteDC, [W.HDC], W.BOOL)

WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x8, 0x80, 0x08000000
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11
HWND_TOPMOST = W.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
WM_PAINT, WM_ERASEBKGND, WM_TIMER, WM_DISPLAYCHANGE, WM_MOUSEACTIVATE = 0xF, 0x14, 0x113, 0x7E, 0x21
WM_APP = 0x8000
WM_SHOW, WM_HIDE, WM_LOCK = WM_APP + 1, WM_APP + 2, WM_APP + 3
LLKHF_INJECTED, LLMHF_INJECTED = 0x10, 0x01


def rgb(r, g, b):
    return r | (g << 8) | (b << 16)


BG, FG, MUTED, ACCENT = rgb(11, 13, 18), rgb(236, 239, 245), rgb(138, 147, 166), rgb(79, 140, 255)
WEEK = "一二三四五六日"


class PrivacyCurtain:
    def __init__(self, block_input=True, remote_active=lambda: False, exclude_from_capture=True):
        self.block_input = block_input
        self.remote_active = remote_active
        self.exclude = exclude_from_capture
        self.hwnd = None
        self.shown = False
        self.available = False
        self._hooks = []
        self._hook_refs = (HOOKPROC(self._kb_proc), HOOKPROC(self._ms_proc))
        self._wndproc_ref = WNDPROC(self._wndproc)
        self._ready = threading.Event()
        self._ticks = 0

    # ---------------------------------------------------------------- 对外接口（任意线程）
    def start(self):
        threading.Thread(target=self._run, name="privacy", daemon=True).start()
        self._ready.wait(5)
        return self.available

    def show(self):
        if self.available and self.hwnd:
            self.shown = True  # 立即反映状态，真正的显示在窗口线程里做
            user32.PostMessageW(self.hwnd, WM_SHOW, 0, 0)

    def hide(self):
        if self.available and self.hwnd:
            self.shown = False
            user32.PostMessageW(self.hwnd, WM_HIDE, 0, 0)

    # ---------------------------------------------------------------- 窗口线程
    def _run(self):
        try:
            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinst
            wc.hbrBackground = gdi32.CreateSolidBrush(BG)
            cls = "RDPrivacyCurtain%x" % id(self)  # 每个实例单独注册，窗口过程才不会串
            wc.lpszClassName = cls
            user32.RegisterClassExW(ctypes.byref(wc))
            ex = WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED | WS_EX_TRANSPARENT
            vx, vy, vw, vh = (user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))
            self.hwnd = user32.CreateWindowExW(ex, cls, "隐私屏", WS_POPUP, vx, vy, vw, vh,
                                               None, None, hinst, None)
            user32.SetLayeredWindowAttributes(self.hwnd, 0, 255, 0x2)  # LWA_ALPHA，完全不透明
            ok = user32.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE if self.exclude else WDA_NONE)
            # 不支持“截屏时隐藏”的系统上不能启用，否则远程也会看到一片黑
            self.available = bool(self.hwnd) and (bool(ok) or not self.exclude)
            if not self.available:
                log.warning("当前系统不支持隐私屏（需要 Windows 10 2004 及以上）")
            user32.SetTimer(self.hwnd, 1, 150, None)   # 保持置顶
            user32.SetTimer(self.hwnd, 2, 1000, None)  # 刷新时钟 / 维护钩子
        finally:
            self._ready.set()
        msg = W.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _place(self):
        vx, vy, vw, vh = (user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, vx, vy, vw, vh, SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def _hook(self):
        self._unhook()
        if not self.block_input:
            return
        hinst = kernel32.GetModuleHandleW(None)
        for kind, proc in ((13, self._hook_refs[0]), (14, self._hook_refs[1])):  # WH_KEYBOARD_LL / WH_MOUSE_LL
            h = user32.SetWindowsHookExW(kind, proc, hinst, 0)
            if h:
                self._hooks.append(h)

    def _unhook(self):
        for h in self._hooks:
            user32.UnhookWindowsHookEx(h)
        self._hooks = []

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_PAINT:
                self._paint(hwnd)
                return 0
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_MOUSEACTIVATE:
                return 3  # MA_NOACTIVATE
            if msg == WM_SHOW:
                self.shown = True
                self._place()
                self._hook()
                user32.InvalidateRect(hwnd, None, False)
                log.info("隐私屏已开启")
                return 0
            if msg == WM_HIDE:
                self.shown = False
                self._unhook()
                user32.ShowWindow(hwnd, 0)
                log.info("隐私屏已关闭")
                return 0
            if msg == WM_LOCK:
                user32.LockWorkStation()
                return 0
            if msg == WM_DISPLAYCHANGE and self.shown:
                self._place()
                return 0
            if msg == WM_TIMER and self.shown:
                if wp == 1:
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                else:
                    user32.InvalidateRect(hwnd, None, False)
                    self._ticks += 1
                    if self._ticks % 15 == 0:  # 钩子回调太慢会被系统悄悄摘掉，定期重挂
                        self._hook()
                return 0
        except Exception as e:
            log.warning("隐私屏窗口出错: %s", e)
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    # ---------------------------------------------------------------- 本地输入拦截
    def _kb_proc(self, n, wp, lp):
        try:
            if n == 0 and self.shown:
                k = ctypes.cast(lp, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if not k.flags & LLKHF_INJECTED:
                    if wp in (0x100, 0x104) and k.vkCode == 0x0D and not self.remote_active():
                        user32.PostMessageW(self.hwnd, WM_LOCK, 0, 0)
                    return 1
        except Exception:
            pass
        return user32.CallNextHookEx(None, n, wp, lp)

    def _ms_proc(self, n, wp, lp):
        try:
            if n == 0 and self.shown:
                m = ctypes.cast(lp, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if not m.flags & LLMHF_INJECTED:
                    return 1
        except Exception:
            pass
        return user32.CallNextHookEx(None, n, wp, lp)

    # ---------------------------------------------------------------- 绘制
    def _font(self, size, weight=400, face="Microsoft YaHei UI"):
        return gdi32.CreateFontW(-int(size), 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, face)

    def _text(self, hdc, text, size, color, top, w, weight=400, face="Microsoft YaHei UI"):
        f = self._font(size, weight, face)
        old = gdi32.SelectObject(hdc, f)
        gdi32.SetTextColor(hdc, color)
        r = W.RECT(0, int(top), w, int(top + size * 1.6))
        user32.DrawTextW(hdc, text, -1, ctypes.byref(r), 0x1 | 0x20 | 0x800)  # CENTER|SINGLELINE|NOPREFIX
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(f)

    def _paint(self, hwnd):
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        rc = W.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rc))
        w, h = rc.right, rc.bottom
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        oldbmp = gdi32.SelectObject(mdc, bmp)
        bg = gdi32.CreateSolidBrush(BG)
        user32.FillRect(mdc, ctypes.byref(rc), bg)
        gdi32.DeleteObject(bg)
        gdi32.SetBkMode(mdc, 1)  # TRANSPARENT

        unit = min(h, w * 0.625)
        cx, y = w // 2, h * 0.24
        # 锁的图标
        s = unit * 0.07
        pen = gdi32.CreatePen(0, max(2, int(s * 0.13)), FG)
        oldpen = gdi32.SelectObject(mdc, pen)
        oldbr = gdi32.SelectObject(mdc, gdi32.GetStockObject(5))  # NULL_BRUSH
        gdi32.RoundRect(mdc, int(cx - s * 0.3), int(y), int(cx + s * 0.3), int(y + s * 0.9), int(s * 0.6), int(s * 0.6))
        body = gdi32.CreateSolidBrush(FG)
        gdi32.SelectObject(mdc, body)
        gdi32.SelectObject(mdc, gdi32.GetStockObject(8))  # NULL_PEN
        gdi32.RoundRect(mdc, int(cx - s * 0.48), int(y + s * 0.45), int(cx + s * 0.48), int(y + s * 1.12),
                        int(s * 0.18), int(s * 0.18))
        gdi32.SelectObject(mdc, oldpen)
        gdi32.SelectObject(mdc, oldbr)
        gdi32.DeleteObject(pen)
        gdi32.DeleteObject(body)

        t = time.localtime()
        y += s * 1.5
        self._text(mdc, time.strftime("%H:%M", t), unit * 0.15, FG, y, w, 300, "Segoe UI Light")
        y += unit * 0.15 * 1.45
        self._text(mdc, "%d月%d日  星期%s" % (t.tm_mon, t.tm_mday, WEEK[t.tm_wday]), unit * 0.032, FG, y, w)
        y += unit * 0.032 * 3.2
        remote = self.remote_active()
        title = "此电脑正在被远程使用" if remote else "此电脑已锁定"
        self._text(mdc, title, unit * 0.03, FG, y, w, 600)
        y += unit * 0.03 * 1.9
        sub = "屏幕内容已隐藏" + (" · 本机键盘和鼠标已暂停" if self.block_input else "")
        self._text(mdc, sub, unit * 0.021, MUTED, y, w)
        if not remote and self.block_input:
            self._text(mdc, "按 Enter 键解锁（需要输入 Windows 开机密码或 PIN）", unit * 0.021, ACCENT, h - unit * 0.12, w)

        gdi32.BitBlt(hdc, 0, 0, w, h, mdc, 0, 0, 0x00CC0020)  # SRCCOPY
        gdi32.SelectObject(mdc, oldbmp)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.EndPaint(hwnd, ctypes.byref(ps))
