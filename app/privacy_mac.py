"""隐私屏（macOS，服务进程这一侧）。对外接口和 Windows 的 privacy.PrivacyCurtain 一样。

真正的锁屏画面、本机键鼠拦截、按 Enter 锁屏都在状态窗口进程（mac_overlay.py）里做：
- 那边有 AppKit 的主线程和 run loop，画全屏窗口、装事件拦截（CGEventTap）都要它；
- 服务进程万一崩溃重启，锁屏画面不会跟着消失（防止你最后停留的页面露出来）。
这里只把“开/关”转告过去，并记住当前状态。

截屏怎么避开锁屏画面：见 macapi.Grabber —— 每帧截“隐私屏窗口下面的所有窗口”。
"""
import logging
import os
import time

import macipc

log = logging.getLogger("rd.privacy")
CURTAIN = None  # 当前实例，截屏时要问它“隐私屏可能在屏幕上吗”


class PrivacyCurtain:
    def __init__(self, block_input=True, remote_active=lambda: False):
        self.block_input = block_input
        self.remote_active = remote_active
        self.shown = False
        self.available = False
        self.changed_at = 0.0
        self._listener = None

    def start(self):
        global CURTAIN
        self.available = macipc.active()  # 只有在状态窗口进程下面运行时才有隐私屏
        if self.available:
            CURTAIN = self
            # 服务进程重启时，锁屏画面可能还在（状态窗口那边一直挂着），接上这个状态
            self.shown = os.environ.get("RD_CURTAIN_ON") == "1"
            self.changed_at = time.monotonic()
            macipc.handlers["curtain_state"] = self._on_state
        return self.available

    def show(self):
        if self.available:
            self.shown = True
            self.changed_at = time.monotonic()
            macipc.send({"t": "curtain", "on": True})
            log.info("隐私屏已开启")

    def hide(self):
        if self.available:
            self.shown = False
            self.changed_at = time.monotonic()
            macipc.send({"t": "curtain", "on": False})
            log.info("隐私屏已关闭")

    def set_listener(self, fn):
        """fn(on, reason)：状态窗口那边自己改了状态时调用（在通信线程里）。"""
        self._listener = fn

    def capture_hint(self):
        """截屏前问一下：隐私屏窗口此刻可能在屏幕上吗？刚开/刚关的几秒内也算，留足窗口出现/消失的时间。"""
        return self.shown or time.monotonic() - self.changed_at < 3

    def _on_state(self, msg):
        on = bool(msg.get("on"))
        if on == self.shown:
            return
        self.shown = on
        self.changed_at = time.monotonic()
        log.info("隐私屏状态由本机改为：%s（%s）", "开" if on else "关", msg.get("reason") or "")
        if self._listener:
            self._listener(on, msg.get("reason"))
