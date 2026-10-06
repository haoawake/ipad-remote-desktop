"""隐私屏（macOS，状态窗口进程这一侧）：锁屏画面、本机键鼠拦截、按 Enter 锁屏、解锁后撤掉。

和 Windows 版（privacy.py）同一套规则：
- iPad 一连上（或者点了“隐私屏”按钮），每块显示器上盖一个全屏窗口：时钟 + “此电脑正在被远程使用”。
  窗口在“屏蔽层”（CGShieldingWindowLevel，比菜单栏、程序坞、通知、全屏应用都高）、鼠标穿透、不抢焦点、
  所有桌面空间都显示、按 ⌘H/⌥⌘H 也藏不掉。
- 远程截屏时用“隐私屏窗口下面的所有窗口”模式（见 macapi.Grabber），所以 iPad 上看到的是真实桌面。
- 本机键盘鼠标用事件拦截（CGEventTap）挡掉，只放行本程序注入的事件（带 macapi.MAGIC 标记）。
  鼠标指针的移动在 macOS 上拦不住（系统先挪指针再发事件），所以本机一动就把指针拉回远程最后放的位置。
- 解除：远程断开 2 分钟后，在 Mac 上按 Enter → Mac 锁屏 → 用登录密码解锁 → 隐私屏消失。
  只有知道这台 Mac 登录密码的人能解除。

锁屏时本程序把自己的窗口收起来、停掉拦截，保证系统的锁屏界面能正常输入密码。
"""
import ctypes
import logging
import threading
import time

import objc
import Quartz
from AppKit import (NSBackingStoreBuffered, NSBezierPath, NSColor, NSFont, NSFontAttributeName,
                    NSForegroundColorAttributeName, NSMakeRect, NSMutableParagraphStyle, NSNotificationCenter,
                    NSParagraphStyleAttributeName, NSRectFill, NSScreen, NSView, NSWindow)
from Foundation import (NSAttributedString, NSDistributedNotificationCenter, NSObject, NSRunLoop,
                        NSRunLoopCommonModes, NSTimer)
from PyObjCTools import AppHelper

import macapi

log = logging.getLogger("rd.privacy")

WEEK = "一二三四五六日"


def _rgb(r, g, b):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255, g / 255, b / 255, 1.0)


BG, FG, MUTED, ACCENT = _rgb(11, 13, 18), _rgb(236, 239, 245), _rgb(138, 147, 166), _rgb(79, 140, 255)
# 所有空间都显示 | 不随“调度中心/显示桌面”移动 | 不进 ⌘` 循环 | 也出现在全屏应用的空间里
BEHAVIOR = (1 << 0) | (1 << 4) | (1 << 6) | (1 << 8)
MOUSE_MOVES = (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDragged, Quartz.kCGEventRightMouseDragged,
               Quartz.kCGEventOtherMouseDragged)
TAP_OFF = (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput)


def lock_screen():
    """立即锁屏（和菜单 → 锁定屏幕、⌃⌘Q 一样）。"""
    try:
        login = ctypes.cdll.LoadLibrary("/System/Library/PrivateFrameworks/login.framework/Versions/Current/login")
        fn = login.SACLockScreenImmediate
        fn.restype = ctypes.c_int
        r = fn()
        log.info("已请求锁屏 (SACLockScreenImmediate=%s)", r)
        if r == 0:
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("SACLockScreenImmediate 不可用：%s", e)
    macapi.combo(["ControlLeft", "MetaLeft", "KeyQ"])  # 退路：按 ⌃⌘Q（带本程序标记，拦截会放行）
    log.info("已用 ⌃⌘Q 锁屏")
    return True


class CurtainView(NSView):
    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return False

    def drawRect_(self, rect):
        owner = getattr(self, "owner", None)
        if owner is not None:
            owner.draw(self)


class _Observer(NSObject):
    """接系统通知（锁屏/解锁、显示器变化）的靶子：AppKit 的通知只能投递给 Objective-C 对象。"""

    def screenLocked_(self, note):
        self.owner.on_locked()

    def screenUnlocked_(self, note):
        self.owner.on_unlocked()

    def screensChanged_(self, note):
        self.owner.on_screens_changed()

    def tick_(self, timer):
        self.owner.tick()


class InputBlocker:
    """本机键鼠拦截。跑在自己的线程上（有自己的 run loop），主线程忙的时候也不会把全系统输入卡住。"""

    def __init__(self, curtain):
        self.curtain = curtain
        self.thread = None
        self.tap = None
        self.rl = None
        self.ok = False
        self.anchor = None
        self._ready = threading.Event()
        self._last_try = -100.0

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return self.ok
        if time.monotonic() - self._last_try < 10:  # 失败过就别每秒重试、刷屏写日志
            return False
        self._last_try = time.monotonic()
        self._ready.clear()
        self.anchor = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        self.thread = threading.Thread(target=self._run, name="input-block", daemon=True)
        self.thread.start()
        self._ready.wait(3)
        return self.ok

    def stop(self):
        t, rl = self.thread, self.rl
        self.thread = None
        if rl is not None:
            Quartz.CFRunLoopStop(rl)
        if t is not None:
            t.join(2)

    def _run(self):
        tap = None
        try:
            for where in (Quartz.kCGHIDEventTap, Quartz.kCGSessionEventTap):
                tap = Quartz.CGEventTapCreate(where, Quartz.kCGHeadInsertEventTap, Quartz.kCGEventTapOptionDefault,
                                              Quartz.kCGEventMaskForAllEvents, self._cb, None)
                if tap:
                    break
            if not tap:
                log.warning("本机键鼠拦截不可用（需要「辅助功能」权限）")
                self.ok = False
                return
            self.tap = tap
            src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            self.rl = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(self.rl, src, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(tap, True)
            self.ok = True
            log.info("本机键盘鼠标已暂停")
        finally:
            self._ready.set()
        Quartz.CFRunLoopRun()
        Quartz.CGEventTapEnable(tap, False)
        Quartz.CFMachPortInvalidate(tap)
        self.tap = self.rl = None
        self.ok = False
        log.info("本机键盘鼠标已恢复")

    def _cb(self, proxy, etype, event, refcon):
        if etype in TAP_OFF:  # 回调慢了被系统停掉：马上重新打开
            if self.tap is not None:
                Quartz.CGEventTapEnable(self.tap, True)
            return event
        try:
            if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData) == macapi.MAGIC:
                if etype in MOUSE_MOVES or etype in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventRightMouseDown):
                    self.anchor = Quartz.CGEventGetLocation(event)
                return event  # 远程发来的，放行
            if etype == Quartz.kCGEventKeyDown:
                kc = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                rep = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat)
                if kc in (0x24, 0x4C) and not rep:  # Return / 小键盘 Enter
                    AppHelper.callAfter(self.curtain.enter_pressed)
            elif etype in MOUSE_MOVES and self.anchor is not None:
                Quartz.CGWarpMouseCursorPosition(self.anchor)
        except Exception:  # noqa: BLE001
            pass
        return None  # 本机的，丢掉


class Curtain:
    """隐私屏窗口的管理者。所有方法都在主线程上调用。"""

    def __init__(self, block_input=True, notify=None):
        self.block_input = block_input
        self.notify = notify or (lambda on, reason: None)  # 告诉服务进程：本机这边改了状态
        self.shown = False
        self.remote_active = False
        self.windows = []
        self.views = []
        self.locked = False
        self.blocker = InputBlocker(self)
        self.blocking = False
        self._obs = _Observer.alloc().init()
        self._obs.owner = self
        dnc = NSDistributedNotificationCenter.defaultCenter()
        dnc.addObserver_selector_name_object_(self._obs, "screenLocked:", "com.apple.screenIsLocked", None)
        dnc.addObserver_selector_name_object_(self._obs, "screenUnlocked:", "com.apple.screenIsUnlocked", None)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self._obs, "screensChanged:", "NSApplicationDidChangeScreenParametersNotification", None)
        # 加到 common 模式：弹着对话框（比如“重置密码”确认框）的时候时钟也照走
        self._timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self._obs, "tick:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self._timer, NSRunLoopCommonModes)

    # ---------------------------------------------------------------- 开 / 关
    def show(self):
        if self.shown:
            return
        self.shown = True
        self.locked = macapi.is_session_locked()
        if not self.locked:
            self._open_windows()
            self._start_blocking()
        log.info("隐私屏已显示")

    def hide(self, reason=None, tell_worker=False):
        if not self.shown:
            return
        self.shown = False
        self._stop_blocking()
        self._close_windows()
        log.info("隐私屏已撤掉%s", "（%s）" % reason if reason else "")
        if tell_worker:
            self.notify(False, reason)

    def set_remote(self, active):
        if active != self.remote_active:
            self.remote_active = active
            self._redraw()

    def set_block_input(self, on):
        self.block_input = on
        if self.shown and not self.locked:
            self._stop_blocking()
            self._start_blocking()

    # ---------------------------------------------------------------- 窗口
    def _open_windows(self):
        self._close_windows()
        level = Quartz.CGShieldingWindowLevel()
        for scr in NSScreen.screens():
            frame = scr.frame()
            win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(frame, 0, NSBackingStoreBuffered, False)
            win.setReleasedWhenClosed_(False)
            win.setLevel_(level)
            # 不标成“不透明”：系统就不会认为下面的窗口被完全挡住，那些程序照常刷新画面（远程才看得到动的内容）
            win.setOpaque_(False)
            win.setBackgroundColor_(NSColor.clearColor())
            win.setHasShadow_(False)
            win.setIgnoresMouseEvents_(True)
            win.setCollectionBehavior_(BEHAVIOR)
            win.setCanHide_(False)
            win.setExcludedFromWindowsMenu_(True)
            win.setTitle_("iPad 远程桌面 隐私屏")
            view = CurtainView.alloc().initWithFrame_(NSMakeRect(0, 0, frame.size.width, frame.size.height))
            view.owner = self
            win.setContentView_(view)
            win.setFrame_display_(frame, True)
            win.orderFrontRegardless()
            self.windows.append(win)
            self.views.append(view)

    def _close_windows(self):
        for w in self.windows:
            w.orderOut_(None)
            w.close()
        self.windows, self.views = [], []

    def _redraw(self):
        for v in self.views:
            v.setNeedsDisplay_(True)

    def _start_blocking(self):
        self.blocking = bool(self.block_input and macapi.accessibility_allowed() and self.blocker.start())
        self._redraw()

    def _stop_blocking(self):
        self.blocker.stop()
        self.blocking = False

    # ---------------------------------------------------------------- 事件
    def tick(self):
        if not self.shown:
            return
        locked = macapi.is_session_locked()
        if locked != self.locked:  # 通知没收到（或者先于状态变化到）时，轮询兜底
            self.on_locked() if locked else self.on_unlocked()
        if self.locked:
            return
        for w in self.windows:  # 有程序把自己的窗口提得很高时，再压回去
            w.orderFrontRegardless()
        if self.block_input and not self.blocking and macapi.accessibility_allowed():
            self._start_blocking()  # 刚刚才授权了辅助功能
        self._redraw()

    def enter_pressed(self):
        if self.shown and not self.remote_active and not self.locked:
            log.info("本机按了 Enter（远程已断开超过 2 分钟），锁定 Mac")
            lock_screen()

    def on_locked(self):
        if self.locked:
            return
        self.locked = True
        if not self.shown:
            return
        log.info("Mac 已锁屏：收起隐私屏窗口，让系统锁屏界面正常输入密码")
        self._stop_blocking()

        def later():  # 等锁屏界面完全盖住屏幕再收，免得露出一瞬间桌面
            if self.locked and self.shown:
                self._close_windows()
        AppHelper.callLater(1.5, later)

    def on_unlocked(self):
        if not self.locked or macapi.is_session_locked():
            return  # 通知比状态先到：等下一次 tick 再判断
        self.locked = False
        if self.shown:
            # 能解开 Mac 锁屏的就是主人：撤掉隐私屏（和 Windows 上“解锁后自动消失”一样）
            self.hide(reason="有人在电脑上用 Mac 登录密码解锁了，隐私屏已自动关闭", tell_worker=True)

    def on_screens_changed(self):
        if self.shown and not self.locked:
            self._open_windows()

    # ---------------------------------------------------------------- 绘制
    def _text(self, s, size, color, top, w, weight=0.0, mono=False):
        font = (NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight) if mono
                else NSFont.systemFontOfSize_weight_(size, weight))
        para = NSMutableParagraphStyle.alloc().init()
        para.setAlignment_(1)  # 居中
        attrs = {NSFontAttributeName: font, NSForegroundColorAttributeName: color, NSParagraphStyleAttributeName: para}
        NSAttributedString.alloc().initWithString_attributes_(s, attrs).drawInRect_(
            NSMakeRect(0, top, w, size * 1.6))

    def draw(self, view):
        with objc.autorelease_pool():
            b = view.bounds()
            w, h = b.size.width, b.size.height
            BG.setFill()
            NSRectFill(b)
            unit = min(h, w * 0.625)
            cx, y = w / 2, h * 0.24
            # 锁的图标
            s = unit * 0.07
            FG.setStroke()
            shackle = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(cx - s * 0.3, y, s * 0.6, s * 0.9), s * 0.3, s * 0.3)
            shackle.setLineWidth_(max(2.0, s * 0.13))
            shackle.stroke()
            FG.setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(cx - s * 0.48, y + s * 0.45, s * 0.96, s * 0.67), s * 0.09, s * 0.09).fill()

            t = time.localtime()
            y += s * 1.5
            self._text(time.strftime("%H:%M", t), unit * 0.15, FG, y, w, weight=-0.4, mono=True)
            y += unit * 0.15 * 1.45
            self._text("%d月%d日  星期%s" % (t.tm_mon, t.tm_mday, WEEK[t.tm_wday]), unit * 0.032, FG, y, w)
            y += unit * 0.032 * 3.2
            remote = self.remote_active
            self._text("此电脑正在被远程使用" if remote else "此电脑已锁定", unit * 0.03, FG, y, w, weight=0.3)
            y += unit * 0.03 * 1.9
            sub = "屏幕内容已隐藏" + (" · 本机键盘和鼠标已暂停" if self.blocking else "")
            self._text(sub, unit * 0.021, MUTED, y, w)
            if not remote:
                hint = ("按 Enter 键解锁（需要输入这台 Mac 的登录密码）" if self.blocking
                        else "按 ⌃⌘Q 锁定屏幕，再用这台 Mac 的登录密码解锁")
                self._text(hint, unit * 0.021, ACCENT, h - unit * 0.12, w)

    def snapshot_png(self, path):
        """把第一块屏幕上的隐私屏画面存成 PNG（自动化测试截图用，不需要屏幕录制权限）。"""
        if not self.views:
            return False
        return _view_png(self.views[0], path)


def _view_png(view, path):
    rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
    if rep is None:
        return False
    view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
    data = rep.representationUsingType_properties_(4, {})  # NSBitmapImageFileTypePNG
    return bool(data and data.writeToFile_atomically_(str(path), True))
