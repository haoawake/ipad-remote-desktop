"""macOS 服务进程 ↔ 状态窗口进程的通信：一行一条 JSON，服务进程从 stdout 发、从 stdin 收。

服务进程 → 状态窗口：
    {"t": "status", ...}          访问地址、通道状态（Dashboard.snapshot）
    {"t": "remote", "active": b, "sessions": n}
    {"t": "curtain", "on": b}     请求显示/撤掉隐私屏
    {"t": "fatal", "msg": "..."}  启动失败的原因
    {"t": "ime_ascii", "rid": n}  远程要打字了：Mac 当前是输入法的话切到英文键盘（要回音）
    {"t": "ime_restore"}          远程不用了：切回原来的输入法
状态窗口 → 服务进程：
    {"t": "curtain_state", "on": b, "reason": "..."}   状态窗口那边自己改了隐私屏（比如 Mac 解锁后撤掉）
    {"t": "reply", "rid": n, ...}                     对带 rid 的请求的回音

状态窗口进程没了（stdin 读到 EOF）服务进程也跟着退出，不会留下没人管的后台进程。
"""
import json
import os
import signal
import sys
import threading

_out = None
_lock = threading.Lock()
handlers = {}
_pending = {}   # rid -> [Event, 回音]
_rid = [0]


def active():
    return _out is not None


def init():
    """接管 stdout 作为通信通道。之后任何 print 都会落到 stderr，不会把通道写乱。"""
    global _out
    if os.environ.get("RD_UI_PID") is None:
        return  # 不是被状态窗口拉起来的（比如直接用命令行调试），不建通道
    _out = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    threading.Thread(target=_reader, name="ipc", daemon=True).start()


def send(obj):
    if _out is None:
        return
    line = json.dumps(obj) + "\n"  # 全 ASCII（中文转义），对端用什么编码读都不会乱
    with _lock:
        try:
            _out.write(line)
            _out.flush()
        except (OSError, ValueError):
            pass


def request(obj, timeout=0.5):
    """发一条消息并等对端回音（{"t": "reply", "rid": …}）。没有通道或超时返回 None。"""
    if _out is None:
        return None
    with _lock:
        _rid[0] += 1
        rid = _rid[0]
    slot = _pending[rid] = [threading.Event(), None]
    send(dict(obj, rid=rid))
    slot[0].wait(timeout)
    _pending.pop(rid, None)
    return slot[1]


def _reader():
    for line in sys.stdin.buffer:  # 按字节读、自己按 UTF-8 解：开机自启时进程的 locale 不一定是 UTF-8
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if msg.get("t") == "reply":
            slot = _pending.get(msg.get("rid"))
            if slot:
                slot[1] = msg
                slot[0].set()
            continue
        fn = handlers.get(msg.get("t"))
        if fn:
            try:
                fn(msg)
            except Exception as e:  # noqa: BLE001
                print("ipc handler error:", e, file=sys.stderr)
    # 状态窗口进程退出（或崩溃）了：自己也退出，顺带收掉 cloudflared
    os.kill(os.getpid(), signal.SIGTERM)
