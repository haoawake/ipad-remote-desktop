"""macOS 状态窗口进程：双击「iPad 远程桌面.app」打开的就是它。

Windows 上是一个黑色控制台窗口 + 几个 .bat；Mac 上换成一个原生窗口：
- 显示登录密码、Tailscale 地址、备用地址、ntfy 频道、权限状态；
- 拷贝、重置密码、登录后自动启动、打开数据文件夹、停止并退出；
- 把服务进程（同一个可执行文件加 --worker）拉起来，崩溃了 5 秒后自动重启；
- 托管隐私屏窗口和本机键鼠拦截（mac_overlay.py）。

关掉窗口不会停止服务（程序坞里点图标可以再打开），⌘Q 或「停止并退出」才是停止。
"""
import fcntl
import json
import logging
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import objc
from AppKit import (NSAlert, NSApp, NSApplication, NSApplicationActivationPolicyRegular, NSBezelStyleRounded,
                    NSBox, NSButton, NSColor, NSFont, NSImage, NSImageView, NSMakeRect, NSMenu, NSMenuItem,
                    NSPasteboard, NSPasteboardTypeString, NSTextField, NSView, NSWindow, NSWorkspace)
from Foundation import (NSURL, NSBundle, NSDistributedNotificationCenter, NSMakeSize, NSObject, NSProcessInfo,
                        NSRunLoop, NSRunLoopCommonModes, NSTimer)
from PyObjCTools import AppHelper

import common
import macapi
from common import APP_NAME, CODE, CONFIG, DATA, ROOT, gen_password, load_config, save_config

log = logging.getLogger("rd.ui")

BUNDLE_ID = "com.haoawake.ipad-remote-desktop"
SHOW_NOTE = BUNDLE_ID + ".show"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / (BUNDLE_ID + ".plist")
FROZEN = bool(getattr(sys, "frozen", False))
HELP_URL = "https://github.com/haoawake/ipad-remote-desktop/blob/main/README.zh-CN.md"
TS_DOWNLOAD = "https://tailscale.com/download/mac"
PANE_SCREEN = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
PANE_AX = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
W = 560          # 窗口宽度（点）
M = 22           # 左右边距
CW = W - 2 * M   # 内容宽度


# ====================================================================== 小工具
def bundle_path():
    return str(NSBundle.mainBundle().bundlePath())


def translocated():
    """从“下载”里直接双击打开（没拖到「应用程序」）时，系统会把程序挪到一个只读的随机位置运行。"""
    return "/AppTranslocation/" in bundle_path()


def program_args():
    if FROZEN:
        return [sys.executable]
    return [sys.executable, str(CODE / "mac_main.py")]


def open_url(url):
    NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))


def copy_text(s):
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(s, NSPasteboardTypeString)


def request_permissions():
    """第一次打开时请求一次：系统会弹出授权提示，并把本程序加进「隐私与安全性」的列表里，用户只要打开开关。"""
    if not macapi.screen_capture_allowed():
        try:
            fn = macapi._cg.CGRequestScreenCaptureAccess
            fn.restype = macapi.ctypes.c_bool
            fn()
        except Exception as e:  # noqa: BLE001
            log.warning("请求屏幕录制权限失败：%s", e)
    if not macapi.accessibility_allowed():
        try:
            from Foundation import NSDictionary
            fn = macapi._ax.AXIsProcessTrustedWithOptions
            fn.argtypes = [macapi.ctypes.c_void_p]
            fn.restype = macapi.ctypes.c_bool
            opts = NSDictionary.dictionaryWithObject_forKey_(True, "AXTrustedCheckOptionPrompt")
            fn(objc.pyobjc_id(opts))
        except Exception as e:  # noqa: BLE001
            log.warning("请求辅助功能权限失败：%s", e)


# ====================================================================== 开机自启（LaunchAgent）
def autostart_enabled():
    return LAUNCH_AGENT.exists()


def set_autostart(on):
    if not on:
        try:
            LAUNCH_AGENT.unlink()
        except FileNotFoundError:
            pass
        log.info("已关闭登录后自动启动")
        return
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": BUNDLE_ID,
        "ProgramArguments": program_args() + ["--login"],
        "RunAtLoad": True,
        # 崩溃（非正常退出）时由系统再拉起来；⌘Q 正常退出则不会
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
    }
    with open(LAUNCH_AGENT, "wb") as f:
        plistlib.dump(plist, f)
    log.info("已开启登录后自动启动：%s", LAUNCH_AGENT)


def refresh_autostart():
    """程序被挪了位置（比如从“下载”拖进了「应用程序」）：自启项里记的路径跟着更新。"""
    if not autostart_enabled() or translocated():
        return
    try:
        with open(LAUNCH_AGENT, "rb") as f:
            cur = plistlib.load(f).get("ProgramArguments", [])
    except Exception:  # noqa: BLE001
        cur = []
    if cur[:-1] != program_args():
        set_autostart(True)


# ====================================================================== 服务进程
class Worker:
    """拉起服务进程、转发消息、崩溃自动重启（相当于 Windows 启动脚本里的那个循环）。"""

    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.proc = None
        self.stopping = False
        self._wlock = threading.Lock()

    def start(self):
        self.stop()
        self.stopping = False
        env = dict(os.environ)
        env["RD_UI_PID"] = str(os.getpid())
        env["RD_CURTAIN_ON"] = "1" if self.ctrl.curtain.shown else "0"
        env.setdefault("LANG", "zh_CN.UTF-8")
        DATA.mkdir(parents=True, exist_ok=True)
        errlog = DATA / "worker-stderr.log"
        try:
            if errlog.stat().st_size > 1 << 20:  # 只留最近的
                errlog.replace(DATA / "worker-stderr.old.log")
        except OSError:
            pass
        err = open(errlog, "ab")
        try:
            self.proc = subprocess.Popen(program_args() + ["--worker"], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=err, env=env, cwd=str(ROOT),
                                         start_new_session=True)  # 自成进程组：收尾时连 cloudflared 一起结束
        finally:
            err.close()
        p = self.proc
        log.info("服务进程已启动 pid=%d", p.pid)
        threading.Thread(target=self._read, args=(p,), name="worker-out", daemon=True).start()
        threading.Thread(target=self._wait, args=(p,), name="worker-wait", daemon=True).start()

    def _read(self, p):
        for line in p.stdout:
            try:
                msg = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            AppHelper.callAfter(self.ctrl.on_worker_msg, p, msg)

    def _wait(self, p):
        code = p.wait()
        AppHelper.callAfter(self.ctrl.on_worker_exit, p, code)

    def send(self, obj):
        p = self.proc
        if p is None or p.poll() is not None:
            return
        with self._wlock:
            try:
                p.stdin.write((json.dumps(obj) + "\n").encode())
                p.stdin.flush()
            except (OSError, ValueError):
                pass

    def stop(self, timeout=4.0):
        p, self.proc = self.proc, None
        if p is None or p.poll() is not None:
            return
        self.stopping = True
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            p.wait(timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
        log.info("服务进程已停止 pid=%d", p.pid)

    @staticmethod
    def kill_stale():
        """上次没收干净的服务进程（比如状态窗口进程被强制结束了）先清掉，免得占着端口。"""
        try:
            pid = int((DATA / "pid.txt").read_text().strip())
        except (OSError, ValueError):
            return
        try:
            cmd = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True,
                                 timeout=3).stdout.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return
        if "--worker" in cmd:
            log.info("结束上次遗留的服务进程 pid=%d", pid)
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pid, sig)
                except OSError:
                    try:
                        os.kill(pid, sig)
                    except OSError:
                        break
                time.sleep(1)


# ====================================================================== 界面零件
class Flipped(NSView):
    def isFlipped(self):
        return True


def _font(size, weight=0.0, mono=False):
    return NSFont.monospacedSystemFontOfSize_weight_(size, weight) if mono else NSFont.systemFontOfSize_weight_(size, weight)


def _text_width(s, font):
    from AppKit import NSFontAttributeName
    from Foundation import NSAttributedString
    return NSAttributedString.alloc().initWithString_attributes_(s, {NSFontAttributeName: font}).size().width


class Layout:
    """从上往下摆控件的小帮手（窗口内容视图是翻转坐标，y 向下）。"""

    def __init__(self, view, target):
        self.view, self.target = view, target
        self.y = 0.0
        self.controls = {}  # 名字 -> 控件（测试截图时导出位置用）

    def add(self, v, x, y, w=None, h=None):
        f = v.frame()
        v.setFrame_(NSMakeRect(x, y, w if w is not None else f.size.width, h if h is not None else f.size.height))
        self.view.addSubview_(v)
        return v

    def label(self, text, x=M, size=13, weight=0.0, color=None, mono=False, selectable=False, width=None, y=None):
        if width:
            f = NSTextField.wrappingLabelWithString_(text)
        else:
            f = NSTextField.labelWithString_(text)
        f.setFont_(_font(size, weight, mono))
        f.setSelectable_(selectable)
        if color is not None:
            f.setTextColor_(color)
        if width:
            f.setPreferredMaxLayoutWidth_(width)
            sz = f.cell().cellSizeForBounds_(NSMakeRect(0, 0, width, 10000))
            f.setFrame_(NSMakeRect(0, 0, width, sz.height))
        else:
            f.sizeToFit()
        return self.add(f, x, self.y if y is None else y)

    def para(self, text, x=M, width=CW, size=12, color=None, gap=6, **kw):
        """会自动换行的一段字：按它实际的高度往下排（字多了换成两行也不会和下面的叠在一起）。"""
        f = self.label(text, x=x, width=width, size=size, color=color, **kw)
        self.y += f.frame().size.height + gap
        return f

    def button(self, title, action, tag=0, name=None, primary=False):
        b = NSButton.buttonWithTitle_target_action_(title, self.target, action)
        b.setBezelStyle_(NSBezelStyleRounded)
        b.setTag_(tag)
        if primary:
            b.setKeyEquivalent_("\r")
        b.sizeToFit()
        if name:
            self.controls[name] = b
        return b

    def buttons_right(self, btns, y, right=W - M):
        x = right
        for b in reversed(btns):
            w = b.frame().size.width
            x -= w
            self.add(b, x, y)
            x -= 6
        return x

    def separator(self, gap=14):
        self.y += gap
        box = NSBox.alloc().initWithFrame_(NSMakeRect(M, self.y, CW, 1))
        box.setBoxType_(2)  # NSBoxSeparator
        box.setTitlePosition_(0)  # NSNoTitle
        self.view.addSubview_(box)
        self.y += 1 + gap

    def symbol(self, name, color, x, y, size=16):
        img = None
        try:
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        except Exception:  # noqa: BLE001
            pass
        if img is None:
            return None
        iv = NSImageView.imageViewWithImage_(img)
        iv.setContentTintColor_(color)
        return self.add(iv, x, y, size, size)


# ====================================================================== 按钮的动作靶子
class Actions(NSObject):
    def copyValue_(self, sender):
        s = self.ctrl.copy_values.get(sender.tag())
        if s:
            copy_text(s)
            self.ctrl.flash("已拷贝：" + s)

    def resetPassword_(self, sender):
        self.ctrl.ask_reset_password()

    def toggleAutostart_(self, sender):
        self.ctrl.toggle_autostart(sender)

    def openLink_(self, sender):
        url = self.ctrl.links.get(sender.tag())
        if url:
            open_url(url)

    def openData_(self, sender):
        ROOT.mkdir(parents=True, exist_ok=True)
        NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(str(ROOT)))

    def retry_(self, sender):
        self.ctrl.start_worker()

    def quit_(self, sender):
        NSApp.terminate_(None)

    def tick_(self, timer):
        self.ctrl.tick()

    def showNote_(self, note):
        self.ctrl.show_window()

    def windowShouldClose_(self, win):
        win.orderOut_(None)  # 关窗口 = 藏起来，服务照常运行
        return False


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, note):
        self.ctrl.started()

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, visible):
        self.ctrl.show_window()
        return True

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return False

    def applicationShouldTerminate_(self, app):
        self.ctrl.shutdown()
        return 1  # NSTerminateNow

    def showMainWindow_(self, sender):
        self.ctrl.show_window()

    def openHelp_(self, sender):
        open_url(HELP_URL)


# ====================================================================== 主控
class Controller:
    def __init__(self, hidden=False):
        self.hidden_start = hidden
        self.cfg = load_config()
        self.status = None          # 服务进程最近一次报来的状态
        self.remote = {"active": False, "sessions": 0}
        self.worker_state = "starting"  # starting / running / restarting / failed
        self.worker_msg = ""
        self.restart_at = None
        self.perms = (macapi.screen_capture_allowed(), macapi.accessibility_allowed())
        self.copy_values, self.links = {}, {}
        self.flash_text, self.flash_until = "", 0.0
        self._sig = None
        self.layout = None
        self.actions = Actions.alloc().init()
        self.actions.ctrl = self
        from mac_overlay import Curtain
        self.curtain = Curtain(block_input=self.cfg.get("privacy_block_input", True), notify=self._curtain_notify)
        self.worker = Worker(self)
        self.window = None

    # ---------------------------------------------------------------- 启动 / 退出
    def started(self):
        build_menu(NSApp.delegate())
        self._make_window()
        if not self.hidden_start:
            self.show_window()
        dnc = NSDistributedNotificationCenter.defaultCenter()
        dnc.addObserver_selector_name_object_(self.actions, "showNote:", SHOW_NOTE, None)
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self.actions, "tick:", None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        self._timer = timer
        # 不让系统把本进程当成“闲着”而降速：隐私屏的键鼠拦截回调慢了，系统会把它停掉
        self._activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            0x00EFFFFF | 0xFF00000000, "iPad 远程桌面：隐私屏和服务进程")  # 不禁止睡眠（那是服务进程的事）
        refresh_autostart()
        self._touch_upload_dir()
        Worker.kill_stale()
        self.start_worker()
        if not all(self.perms):
            AppHelper.callLater(1.0, request_permissions)

    def shutdown(self):
        log.info("退出")
        macapi.restore_input_source()  # 远程打字时切走的输入法，退出前切回来
        self.worker.stop()
        self.curtain.hide()

    def _touch_upload_dir(self):
        """先建好“下载/iPad传来的文件”：如果系统要问“是否允许访问‘下载’文件夹”，现在问（人还在电脑前），
        别等到在外面传文件的时候才弹出来没人点。"""
        try:
            Path(os.path.expanduser(self.cfg.get("upload_dir") or common.DEFAULT_UPLOAD_DIR)).mkdir(
                parents=True, exist_ok=True)
        except OSError as e:
            log.warning("建上传文件夹失败：%s", e)

    def start_worker(self):
        self.worker_state, self.worker_msg, self.restart_at = "starting", "", None
        try:
            self.worker.start()
        except Exception as e:  # noqa: BLE001
            log.exception("启动服务进程失败")
            self.worker_state, self.worker_msg = "failed", str(e)
        self.refresh()

    # ---------------------------------------------------------------- 服务进程消息
    def on_worker_msg(self, proc, msg):
        if proc is not self.worker.proc:
            return
        t = msg.get("t")
        if t == "ime_ascii":  # 远程要打字了（见 macapi._ascii_for_typing）
            try:
                switched = macapi._ensure_ascii_input()
            except Exception:  # noqa: BLE001
                log.exception("切换输入法失败")
                switched = False
            self.worker.send({"t": "reply", "rid": msg.get("rid"), "switched": switched})
            return
        if t == "ime_restore":
            macapi.restore_input_source()
            return
        if t == "status":
            self.status = msg
            self.worker_state = "running"
            if msg.get("password"):
                self.cfg["password"] = msg["password"]
        elif t == "remote":
            self.remote = {"active": bool(msg.get("active")), "sessions": int(msg.get("sessions") or 0)}
            self.curtain.set_remote(self.remote["active"])
        elif t == "curtain":
            if msg.get("on"):
                self.curtain.show()
            else:
                self.curtain.hide()
        elif t == "fatal":
            self.worker_msg = msg.get("msg") or ""
        self.refresh()

    def on_worker_exit(self, proc, code):
        # 服务进程没了（崩溃、被强制结束），它拉起的 cloudflared 还留在同一个进程组里：一起收掉，免得越攒越多
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        if proc is not self.worker.proc and self.worker.proc is not None:
            return  # 旧进程（已经被替换掉了）
        if self.worker.stopping:
            return
        self.worker.proc = None
        self.remote = {"active": False, "sessions": 0}
        self.curtain.set_remote(False)
        if code in (2, 3):
            self.worker_state = "failed"
            if code == 3:
                self.worker_msg = "端口 %d 已被占用（可能已经有一个远程桌面服务在运行）" % self.cfg.get("port", 8765)
            self.worker_msg = self.worker_msg or self._log_tail()
            log.warning("服务进程启动失败 code=%s %s", code, self.worker_msg)
        else:
            log.warning("服务进程意外退出 code=%s，5 秒后重启", code)
            self.worker_state = "restarting"
            self.restart_at = time.monotonic() + 5
        self.refresh()

    @staticmethod
    def _log_tail():
        try:
            lines = (DATA / "run.log").read_text("utf-8", "replace").strip().splitlines()
            errs = [ln for ln in lines[-30:] if " ERROR " in ln]
            return (errs or lines or [""])[-1].split(" ", 3)[-1]
        except OSError:
            return ""

    def _curtain_notify(self, on, reason):
        self.worker.send({"t": "curtain_state", "on": on, "reason": reason})

    # ---------------------------------------------------------------- 定时
    def tick(self):
        if self.restart_at is not None and time.monotonic() >= self.restart_at:
            self.start_worker()
        perms = (macapi.screen_capture_allowed(), macapi.accessibility_allowed())
        if perms != self.perms:
            log.info("权限变化：屏幕录制=%s 辅助功能=%s", *perms)
            sr_changed = perms[0] != self.perms[0]
            self.perms = perms
            if sr_changed and self.worker.proc is not None:
                self.start_worker()  # 截屏进程要重新开一次，新的屏幕录制授权才生效
        if self.flash_until and time.monotonic() > self.flash_until:
            self.flash_text, self.flash_until = "", 0.0
        self.refresh()
        self._shot_requests()

    def flash(self, text):
        self.flash_text, self.flash_until = text, time.monotonic() + 2.5
        self.refresh(force=True)

    # ---------------------------------------------------------------- 动作
    def ask_reset_password(self):
        alert = NSAlert.alloc().init()
        alert.setMessageText_("重置登录密码？")
        alert.setInformativeText_("会生成一个新密码，所有已经登录的 iPad 都要用新密码重新登录。")
        alert.addButtonWithTitle_("重置")
        alert.addButtonWithTitle_("取消")

        def done(code):
            if code == 1000:  # 第一个按钮
                self.reset_password()
        if self.window is not None and self.window.isVisible():
            alert.beginSheetModalForWindow_completionHandler_(self.window, done)
        else:
            done(alert.runModal())

    def reset_password(self):
        self.worker.stop()
        cfg = load_config()
        cfg["password"] = gen_password()
        save_config(cfg)
        try:
            (DATA / "sessions.json").unlink()
        except OSError:
            pass
        self.cfg = cfg
        if self.status:
            self.status["password"] = cfg["password"]
        log.info("已重置登录密码")
        self.start_worker()
        self.flash("已换成新密码，所有设备需要重新登录")

    def toggle_autostart(self, sender):
        on = sender.state() == 1
        if on and translocated():
            sender.setState_(0)
            alert = NSAlert.alloc().init()
            alert.setMessageText_("请先把「iPad 远程桌面」拖到「应用程序」文件夹")
            alert.setInformativeText_("现在是直接从“下载”里打开的，系统把它放在一个临时位置运行，重启后就找不到了。"
                                      "拖到「应用程序」之后，从那里重新打开，再勾选这一项。")
            alert.runModal()
            return
        try:
            set_autostart(on)
        except OSError as e:
            sender.setState_(0 if on else 1)
            self.flash("设置失败：%s" % e)
            return
        self.flash("已开启：登录 Mac 后会自动在后台启动" if on else "已关闭登录后自动启动")

    def show_window(self):
        if self.window is None:
            self._make_window()
        self.refresh(force=True)
        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    # ---------------------------------------------------------------- 窗口
    def _make_window(self):
        if self.window is not None:
            return
        style = 1 | 2 | 4  # 标题栏 | 可关闭 | 可最小化
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(0, 0, W, 400), style, 2, False)
        win.setTitle_(APP_NAME)
        win.setReleasedWhenClosed_(False)
        win.setDelegate_(self.actions)
        win.setContentView_(Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, W, 400)))
        self.window = win
        self.refresh(force=True)
        win.center()

    def _model(self):
        st = self.status or {}
        return (self.cfg.get("password"), json.dumps(st.get("ts"), sort_keys=True),
                json.dumps(st.get("cf"), sort_keys=True), st.get("ntfy") or self.cfg.get("ntfy_topic"),
                self.worker_state, self.worker_msg, self.restart_at is not None and int(self.restart_at - time.monotonic()),
                self.perms, self.remote["active"], self.remote["sessions"], self.curtain.shown, autostart_enabled(),
                translocated(), self.flash_text)

    def refresh(self, force=False):
        if self.window is None:
            return
        sig = self._model()
        if sig == self._sig and not force:
            return
        self._sig = sig
        view = self.window.contentView()
        for v in list(view.subviews()):
            v.removeFromSuperview()
        self.copy_values, self.links = {}, {}
        lay = Layout(view, self.actions)
        self._build(lay)
        self.layout = lay
        h = lay.y + 18
        top = self.window.frame().origin.y + self.window.frame().size.height
        self.window.setContentSize_(NSMakeSize(W, h))
        f = self.window.frame()
        self.window.setFrameTopLeftPoint_((f.origin.x, top))

    def _copy_btn(self, lay, value, name=None):
        tag = len(self.copy_values) + 1
        self.copy_values[tag] = value
        return lay.button("拷贝", "copyValue:", tag, name=name)

    def _link_btn(self, lay, title, url, name=None):
        tag = len(self.links) + 1
        self.links[tag] = url
        return lay.button(title, "openLink:", tag, name=name)

    def _build(self, lay):
        sec, green, orange, red = (NSColor.secondaryLabelColor(), NSColor.systemGreenColor(),
                                   NSColor.systemOrangeColor(), NSColor.systemRedColor())
        st = self.status or {}
        # ---- 标题
        lay.y = 18
        icon = NSApp.applicationIconImage()
        if icon is not None:
            lay.add(NSImageView.imageViewWithImage_(icon), M - 4, 14, 60, 60)
        lay.label(APP_NAME, x=M + 66, size=20, weight=0.4, y=20)
        ws = self.worker_state
        if ws == "running":
            if self.remote["sessions"]:
                text, color = "服务运行中 · iPad 已连接", green
            else:
                text, color = "服务运行中 · 等待 iPad 连接", green
        elif ws == "starting":
            text, color = "正在启动服务…", sec
        elif ws == "restarting":
            left = max(0, int((self.restart_at or 0) - time.monotonic()) + 1)
            text, color = "服务意外退出，%d 秒后自动重启" % left, orange
        else:
            text, color = "服务启动失败", red
        lay.label("●", x=M + 66, size=11, color=color, y=52)
        lay.label(text, x=M + 80, size=13, color=color, y=50)
        lay.y = 84
        if ws == "failed":
            t = lay.label(self.worker_msg or "详见数据文件夹里的 data/run.log", width=CW - 80, color=red, size=12)
            lay.buttons_right([lay.button("重试", "retry:", name="retry")], lay.y - 4)
            lay.y += max(t.frame().size.height, 24) + 8
        lay.para("在 iPad 的 Safari 里打开下面的地址、输入密码，就能远程看到并操控这台 Mac。", color=sec, gap=14)

        # ---- 提醒：没拖进「应用程序」/ 缺权限
        if translocated():
            self._banner(lay, "exclamationmark.triangle.fill", orange, "请先把本程序拖到「应用程序」文件夹",
                         "现在是直接从“下载”里打开的。拖到「应用程序」后再从那里打开，权限和登录后自动启动才稳定。")
        missing = [p for p, ok in zip(("screen", "ax"), self.perms) if not ok]
        if missing:
            self._perm_banner(lay, missing)

        # ---- 密码
        lay.label("登录密码", size=12, weight=0.23, color=sec)
        lay.y += 18
        pw = self.cfg.get("password") or ""
        lay.label(pw, size=24, weight=0.4, mono=True, selectable=True)
        lay.buttons_right([self._copy_btn(lay, pw, "copy_password"),
                           lay.button("重置密码…", "resetPassword:", name="reset_password")], lay.y + 2)
        lay.y += 34
        lay.separator()

        # ---- Tailscale
        ts = st.get("ts") or {}
        tstate = ts.get("state") or ("检查中" if ws != "failed" else "—")
        self._section(lay, "主通道 · Tailscale", tstate, green if tstate == "已连接" else orange)
        urls = ts.get("urls") or []
        for i, u in enumerate(urls):
            self._url_row(lay, u, "copy_ts%d" % i)
        if tstate == "已连接":
            lay.para("iPad 上先打开 Tailscale App 并连上，再用 Safari 打开上面的地址。", color=sec, gap=4)
        elif tstate == "未安装":
            t = lay.label("这台 Mac 还没装 Tailscale。装好并登录（iPad 上登录同一个账号）后会自动接上。",
                          width=CW - 130, size=12, color=sec)
            lay.buttons_right([self._link_btn(lay, "下载 Tailscale", TS_DOWNLOAD, "ts_download")], lay.y - 3)
            lay.y += max(t.frame().size.height, 26) + 6
        elif tstate == "需要登录":
            lay.para("点菜单栏里的 Tailscale 图标登录（和 iPad 用同一个账号）。", color=sec, gap=4)
        lay.separator()

        # ---- Cloudflare + ntfy
        cf = st.get("cf") or {}
        cstate = cf.get("state") or "检查中"
        self._section(lay, "备用通道 · Cloudflare", cstate, green if cstate == "已连接" else sec)
        if cf.get("url"):
            self._url_row(lay, cf["url"], "copy_cf")
            lay.para("Mac 重启后这个地址会变，新地址会推送到下面的 ntfy 频道。", color=sec, gap=4)
        topic = st.get("ntfy") or self.cfg.get("ntfy_topic")
        if topic:
            lay.y += 8
            lay.label("地址推送 · ntfy", size=13, weight=0.3)
            lay.y += 22
            self._url_row(lay, "https://ntfy.sh/" + topic, "copy_ntfy")
            lay.para("在 iPad 的 ntfy App 里订阅频道 %s（或者把上面的网址加个书签）。" % topic, color=sec, gap=4)
        lay.separator()

        # ---- 权限、隐私屏
        if not missing:
            x = M
            for name in ("屏幕录制", "辅助功能"):
                lay.symbol("checkmark.circle.fill", green, x, lay.y + 1)
                lbl = lay.label(name + " 已允许", x=x + 21, size=12, color=sec)
                x += 21 + lbl.frame().size.width + 18
            if self.curtain.shown:
                lay.symbol("lock.fill", NSColor.systemBlueColor(), x, lay.y + 1)
                lay.label("隐私屏已开启", x=x + 21, size=12, color=sec)
            lay.y += 26

        # ---- 底部
        cb = NSButton.checkboxWithTitle_target_action_("登录 Mac 后自动启动", self.actions, "toggleAutostart:")
        cb.setState_(1 if autostart_enabled() else 0)
        lay.controls["autostart"] = cb
        lay.add(cb, M, lay.y + 2)
        lay.buttons_right([lay.button("打开数据文件夹", "openData:", name="open_data"),
                           lay.button("停止并退出", "quit:", name="quit")], lay.y)
        lay.y += 34
        note = self.flash_text or ("关掉这个窗口不会停止服务（点程序坞里的图标可以再打开）。要停止请点「停止并退出」或按 ⌘Q。")
        lay.para(note, size=11, color=NSColor.systemBlueColor() if self.flash_text else sec, gap=2)

    def _section(self, lay, title, state, color):
        lay.label(title, size=13, weight=0.3)
        lbl = NSTextField.labelWithString_(state)
        lbl.setFont_(_font(12))
        lbl.setTextColor_(color)
        lbl.sizeToFit()
        lay.add(lbl, W - M - lbl.frame().size.width, lay.y + 1)
        lay.y += 24

    def _url_row(self, lay, url, name):
        """一行地址 + 「拷贝」。地址太长就把字缩小到一行放得下（Cloudflare 的地址动不动五六十个字符），
        实在放不下就中间省略（拷贝按钮拷的始终是完整地址）。"""
        avail = CW - 70
        size = 14.0
        while size > 10.5 and _text_width(url, _font(size, mono=True)) > avail:
            size -= 0.5
        f = lay.label(url, size=size, mono=True, selectable=True)
        f.setFrame_(NSMakeRect(M, lay.y + (14 - size) / 2, avail, f.frame().size.height))
        f.cell().setLineBreakMode_(5)  # NSLineBreakByTruncatingMiddle
        f.setToolTip_(url)
        lay.buttons_right([self._copy_btn(lay, url, name)], lay.y - 4)
        lay.y += 26

    @staticmethod
    def _box(lay, color):
        box = NSBox.alloc().initWithFrame_(NSMakeRect(M - 8, lay.y - 8, CW + 16, 10))
        box.setBoxType_(4)  # NSBoxCustom
        box.setTitlePosition_(0)  # NSNoTitle
        box.setBorderWidth_(1)
        box.setFillColor_(color.colorWithAlphaComponent_(0.12))
        box.setBorderColor_(color.colorWithAlphaComponent_(0.4))
        box.setCornerRadius_(8)
        lay.view.addSubview_(box)
        return box, lay.y

    @staticmethod
    def _close_box(lay, box, y0):
        box.setFrame_(NSMakeRect(M - 8, y0 - 8, CW + 16, lay.y - y0 + 8))
        lay.y += 16

    def _banner(self, lay, sym, color, title, text):
        box, y0 = self._box(lay, color)
        lay.symbol(sym, color, M, lay.y + 1)
        lay.label(title, x=M + 22, size=13, weight=0.3)
        lay.y += 20
        t = lay.label(text, x=M + 22, width=CW - 22, size=12)
        lay.y += t.frame().size.height + 6
        self._close_box(lay, box, y0)

    def _perm_banner(self, lay, missing):
        orange = NSColor.systemOrangeColor()
        box, y0 = self._box(lay, orange)
        lay.symbol("exclamationmark.triangle.fill", orange, M, lay.y + 1)
        lay.label("还差 %d 项系统权限" % len(missing), x=M + 22, size=13, weight=0.3)
        lay.y += 24
        rows = {
            "screen": ("屏幕录制", "没有它 iPad 上只能看到桌面背景。", PANE_SCREEN, "open_screen"),
            "ax": ("辅助功能", "没有它 iPad 上点不动、打不了字，隐私屏也拦不住本机键盘鼠标。", PANE_AX, "open_ax"),
        }
        for p in missing:
            name, why, pane, ctl = rows[p]
            lay.label(name, x=M + 22, size=13, weight=0.23)
            lay.buttons_right([self._link_btn(lay, "打开「%s」设置" % name, pane, ctl)], lay.y - 3)
            lay.y += 19
            lay.para(why, x=M + 22, width=CW - 200, color=NSColor.secondaryLabelColor(), gap=9)
        t = lay.label("在「" + macapi.PRIVACY_PATH + "」的列表里找到「iPad 远程桌面」，打开开关（可能要输入 Mac 的登录密码）。"
                      "如果系统提示「退出并重新打开」，点它就行。", x=M + 22, width=CW - 22, size=12)
        lay.y += t.frame().size.height + 6
        self._close_box(lay, box, y0)

    # ---------------------------------------------------------------- 自动化测试截图（只有设置了 RD_SHOT_DIR 才生效）
    def _shot_requests(self):
        out = os.environ.get("RD_SHOT_DIR")
        req = DATA / "shot.req"
        if not out or not req.exists():
            return
        try:
            tags = req.read_text("utf-8").split()
            req.unlink()
        except OSError:
            return
        from mac_overlay import _view_png
        Path(out).mkdir(parents=True, exist_ok=True)
        for tag in tags:
            self.refresh(force=True)
            ok = _view_png(self.window.contentView(), Path(out) / ("status-%s.png" % tag))
            ov = self.curtain.snapshot_png(Path(out) / ("overlay-%s.png" % tag))
            log.info("截图 %s：窗口=%s 隐私屏=%s", tag, ok, ov)
        self._export_layout(Path(out) / "ui-layout.json")

    def _export_layout(self, path):
        """导出各个按钮在屏幕上的位置（Quartz 坐标，点），自动化测试用它来点按钮。"""
        from AppKit import NSScreen
        H = NSScreen.screens()[0].frame().size.height
        out = {}
        for name, c in (self.layout.controls if self.layout else {}).items():
            r = self.window.convertRectToScreen_(c.convertRect_toView_(c.bounds(), None))
            out[name] = [r.origin.x, H - r.origin.y - r.size.height, r.size.width, r.size.height]
        out["visible"] = bool(self.window.isVisible())
        path.write_text(json.dumps(out), "utf-8")


# ====================================================================== 菜单
def _item(menu, title, action, key="", mods=None, target=None):
    it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
    if mods is not None:
        it.setKeyEquivalentModifierMask_(mods)
    if target is not None:
        it.setTarget_(target)
    menu.addItem_(it)
    return it


def build_menu(delegate):
    CMD, OPT, SHIFT = 1 << 20, 1 << 19, 1 << 17
    bar = NSMenu.alloc().init()

    app_menu = NSMenu.alloc().initWithTitle_(APP_NAME)
    _item(app_menu, "关于 " + APP_NAME, "orderFrontStandardAboutPanel:")
    app_menu.addItem_(NSMenuItem.separatorItem())
    _item(app_menu, "隐藏 " + APP_NAME, "hide:", "h")
    _item(app_menu, "隐藏其他", "hideOtherApplications:", "h", CMD | OPT)
    _item(app_menu, "全部显示", "unhideAllApplications:")
    app_menu.addItem_(NSMenuItem.separatorItem())
    _item(app_menu, "退出 " + APP_NAME, "terminate:", "q")

    edit = NSMenu.alloc().initWithTitle_("编辑")
    _item(edit, "撤销", "undo:", "z")
    _item(edit, "重做", "redo:", "z", CMD | SHIFT)
    edit.addItem_(NSMenuItem.separatorItem())
    _item(edit, "剪切", "cut:", "x")
    _item(edit, "拷贝", "copy:", "c")
    _item(edit, "粘贴", "paste:", "v")
    _item(edit, "全选", "selectAll:", "a")

    win = NSMenu.alloc().initWithTitle_("窗口")
    _item(win, "最小化", "performMiniaturize:", "m")
    _item(win, "缩放", "performZoom:")
    _item(win, "关闭窗口", "performClose:", "w")
    win.addItem_(NSMenuItem.separatorItem())
    _item(win, APP_NAME, "showMainWindow:", "0", target=delegate)
    win.addItem_(NSMenuItem.separatorItem())
    _item(win, "前置全部窗口", "arrangeInFront:")

    hlp = NSMenu.alloc().initWithTitle_("帮助")
    _item(hlp, APP_NAME + " 使用说明", "openHelp:", "?", target=delegate)

    for title, sub in ((APP_NAME, app_menu), ("编辑", edit), ("窗口", win), ("帮助", hlp)):
        top = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        top.setSubmenu_(sub)
        bar.addItem_(top)
    NSApp.setMainMenu_(bar)
    NSApp.setWindowsMenu_(win)
    NSApp.setHelpMenu_(hlp)


# ====================================================================== 入口
_lock_fd = None


def _single_instance():
    """同一时间只跑一个。已经有一个在跑了，就让它把窗口显示出来，自己退出。"""
    global _lock_fd
    DATA.mkdir(parents=True, exist_ok=True)
    _lock_fd = open(DATA / "app.lock", "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
            SHOW_NOTE, None, None, True)
        return False


def run():
    common.setup_logging("app.log", stream=sys.stderr)
    if not _single_instance():
        log.info("已经在运行，显示已有的窗口")
        return
    log.info("启动 %s（%s）", APP_NAME, bundle_path())
    app = NSApplication.sharedApplication()
    # Info.plist 里登记的是“不在程序坞里显示”（为了服务进程），状态窗口进程在这里切成普通程序
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    delegate = AppDelegate.alloc().init()
    delegate.ctrl = Controller(hidden="--login" in sys.argv)
    app.setDelegate_(delegate)

    def on_term(*_):
        AppHelper.callAfter(NSApp.terminate_, None)
    signal.signal(signal.SIGTERM, on_term)
    AppHelper.runEventLoop()
