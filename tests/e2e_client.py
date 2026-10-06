"""端到端测试：像 iPad 上的网页一样登录、连 WebSocket、收画面、发鼠标键盘。只在 CI 里跑。

    python tests/e2e_client.py --port 8765 --password PW --out out/e2e --os win
    python tests/e2e_client.py --port 8765 --password PW --out out/e2e --os mac --mac-full \
        --target out/target --sup "$HOME/Library/Application Support/iPad 远程桌面" --app "/Applications/iPad 远程桌面.app"

结果写到 <out>/results.json；有“必须通过”的项失败时退出码为 1。
"""
import argparse
import asyncio
import io
import json
import os
import re
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
from PIL import Image, ImageChops, ImageStat


# ====================================================================== 结果记录
class Results:
    def __init__(self, out):
        self.out = out
        self.items = []

    def check(self, name, cond, detail="", required=True):
        cond = bool(cond)
        self.items.append({"name": name, "ok": cond, "required": required, "detail": str(detail)[:600]})
        tag = "PASS" if cond else ("FAIL" if required else "WARN")
        print("%s  %s%s" % (tag, name, ("  —  %s" % detail) if detail != "" else ""), flush=True)
        return cond

    def note(self, name, detail):
        self.items.append({"name": name, "ok": True, "required": False, "detail": str(detail)[:2000], "info": True})
        print("INFO  %s  —  %s" % (name, detail), flush=True)

    def save(self):
        failed = [i["name"] for i in self.items if i["required"] and not i["ok"]]
        warned = [i["name"] for i in self.items if not i["required"] and not i["ok"]]
        (self.out / "results.json").write_text(json.dumps(
            {"failed": failed, "warned": warned, "items": self.items}, ensure_ascii=False, indent=1), "utf-8")
        print("\n通过 %d 项，失败 %d 项，警告 %d 项" % (
            sum(1 for i in self.items if i["ok"] and not i.get("info")), len(failed), len(warned)))
        for n in failed:
            print("  FAILED:", n)
        return not failed


# ====================================================================== WebSocket 客户端（相当于 web/app.js 的连接部分）
class Remote:
    def __init__(self, ws):
        self.ws = ws
        self.hello = None
        self.canvas = None
        self.frames = self.bytes = self.full_frames = 0
        self.cursor = None
        self.shape = None
        self.toasts, self.privacy, self.clips = [], [], []
        self.locked = 0
        self.closed = False
        self.task = asyncio.ensure_future(self._run())

    async def _run(self):
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    self._frame(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    self._json(json.loads(msg.data))
                else:
                    break
        finally:
            self.closed = True

    def _frame(self, data):
        kind, fid, n = struct.unpack_from("<BIH", data)
        off = 7
        g = self.hello["geom"] if self.hello else None
        for _ in range(n):
            x, y, w, h, ln = struct.unpack_from("<HHHHI", data, off)
            off += 12
            img = Image.open(io.BytesIO(data[off:off + ln]))
            img.load()
            if self.canvas is not None:
                self.canvas.paste(img, (x, y))
            if g and (x, y, w, h) == (0, 0, g["sw"], g["sh"]):
                self.full_frames += 1
            off += ln
        self.frames += 1
        self.bytes += len(data)
        asyncio.ensure_future(self.send({"t": "ack", "id": fid}))

    def _json(self, m):
        t = m.get("t")
        if t == "hello":
            self.hello = m
            g = m["geom"]
            if self.canvas is None or self.canvas.size != (g["sw"], g["sh"]):
                self.canvas = Image.new("RGB", (g["sw"], g["sh"]))
        elif t == "c":
            self.cursor = (m["x"], m["y"], m["v"])
        elif t == "cs":
            self.shape = m
        elif t == "toast":
            self.toasts.append(m.get("msg"))
        elif t == "privacy":
            self.privacy.append(m)
        elif t == "clip":
            self.clips.append(m.get("text"))
        elif t == "locked":
            self.locked += 1

    async def send(self, obj):
        if not self.ws.closed:
            await self.ws.send_str(json.dumps(obj, ensure_ascii=False))

    async def wait(self, pred, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if pred():
                    return True
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.05)
        try:
            return bool(pred())
        except Exception:  # noqa: BLE001
            return False

    async def full(self, timeout=10.0, settle=0.8):
        n = self.full_frames
        await self.send({"t": "refresh"})
        ok = await self.wait(lambda: self.full_frames > n, timeout)
        await asyncio.sleep(settle)
        return self.canvas.copy() if ok else None

    async def toast(self, pred, timeout=5.0):
        n = len(self.toasts)
        ok = await self.wait(lambda: any(pred(t or "") for t in self.toasts[n:]), timeout)
        return ok, self.toasts[n:]

    async def close(self):
        await self.ws.close()
        self.task.cancel()


async def connect(s, base, query="scale=1&quality=65&fps=30&privacy=0"):
    ws = await s.ws_connect(base.replace("http://", "ws://") + "/ws?" + query, headers={"Origin": base},
                            max_msg_size=0, heartbeat=20)
    rem = Remote(ws)
    await rem.wait(lambda: rem.hello is not None, 15)
    return rem


async def login(s, base, password):
    async with s.post(base + "/api/login", json={"password": password, "remember": True},
                      headers={"Origin": base}) as resp:
        return resp.status, await resp.json(content_type=None), resp.headers.getall("Set-Cookie", [])


# ====================================================================== 基本流程（两个系统都跑）
async def basic(a, r, s, base):
    async with s.get(base + "/health") as resp:
        r.check("GET /health", resp.status == 200 and await resp.text() == "ok")
    async with s.get(base + "/") as resp:
        html = await resp.text()
        r.check("首页 index.html", resp.status == 200 and "远程桌面" in html and resp.headers.get("X-Frame-Options") == "DENY")
    async with s.get(base + "/static/app.js") as resp:
        r.check("静态文件 app.js", resp.status == 200 and "applyHostOS" in await resp.text())
    async with s.get(base + "/api/me") as resp:
        r.check("未登录 /api/me 返回 401", resp.status == 401)
    st, j, _ = await login(s, base, "wrong-password")
    r.check("错误密码被拒绝", st == 403 and j.get("error") == "密码错误", j)
    async with s.post(base + "/api/login", json={"password": a.password}, headers={"Origin": "https://evil.example"}) as resp:
        r.check("跨站登录被拒绝（Origin 不匹配）", resp.status == 403)
    try:
        ws = await s.ws_connect(base.replace("http://", "ws://") + "/ws")
        await ws.close()
        r.check("未登录不能连 WebSocket", False)
    except aiohttp.WSServerHandshakeError as e:
        r.check("未登录不能连 WebSocket", e.status == 401, e.status)
    st, j, cookies = await login(s, base, a.password)
    r.check("正确密码登录", st == 200 and j.get("ok"), j)
    r.check("登录凭证是 HttpOnly Cookie", any("rd_session=" in c and "HttpOnly" in c for c in cookies), cookies)
    async with s.get(base + "/api/me") as resp:
        me = await resp.json(content_type=None)
        r.check("登录后 /api/me", resp.status == 200 and me.get("ok"), me)
        r.check("/api/me 报告电脑系统 = %s" % a.os, me.get("os", "win") == a.os, me)
    try:
        ws = await s.ws_connect(base.replace("http://", "ws://") + "/ws", headers={"Origin": "https://evil.example"})
        await ws.close()
        r.check("别的网站不能借用登录连 WebSocket", False)
    except aiohttp.WSServerHandshakeError as e:
        r.check("别的网站不能借用登录连 WebSocket", e.status == 403, e.status)

    rem = await connect(s, base)
    r.check("WebSocket hello", rem.hello is not None, rem.hello and {k: rem.hello[k] for k in ("geom", "host", "os")})
    if not rem.hello:
        return None
    g = rem.hello["geom"]
    r.check("hello.os = %s" % a.os, rem.hello.get("os", "win") == a.os)
    r.note("显示器", json.dumps(rem.hello.get("monitors"), ensure_ascii=False))
    t0 = time.monotonic()
    img = await rem.full(20)
    r.check("收到第一张完整画面", img is not None and img.size == (g["sw"], g["sh"]),
            "%s，用时 %.1f 秒" % (img and img.size, time.monotonic() - t0))
    if img:
        img.save(a.out / "frame-first.png")
        r.check("画面不是纯黑", ImageStat.Stat(img.convert("L")).stddev[0] > 3, ImageStat.Stat(img.convert("L")).stddev)

    need_input = a.input_required
    cx, cy = g["width"] // 2, g["height"] // 2
    await rem.send({"t": "mm", "x": cx, "y": cy})
    ok = await rem.wait(lambda: rem.cursor and abs(rem.cursor[0] - cx) <= 2 and abs(rem.cursor[1] - cy) <= 2, 4)
    r.check("鼠标绝对移动（光标位置回报一致）", ok, "目标 %s，回报 %s" % ((cx, cy), rem.cursor), required=need_input)
    await rem.send({"t": "mr", "dx": 40, "dy": -30})
    ok = await rem.wait(lambda: rem.cursor and abs(rem.cursor[0] - cx - 40) <= 3 and abs(rem.cursor[1] - cy + 30) <= 3, 4)
    r.check("鼠标相对移动", ok, "回报 %s" % (rem.cursor,), required=need_input)
    ok = await rem.wait(lambda: rem.shape is not None, 6)
    if r.check("收到光标形状", ok, rem.shape and {k: rem.shape[k] for k in ("w", "h", "hx", "hy")}):
        import base64
        (a.out / "cursor.png").write_bytes(base64.b64decode(rem.shape["png"]))

    txt = "剪贴板测试 Clipboard ✓ %d" % int(time.time())
    await rem.send({"t": "clip_set", "text": txt})
    ok, ts = await rem.toast(lambda t: "剪贴板" in t)
    r.check("写入电脑剪贴板", ok and any("已写入" in t for t in ts), ts)
    n = len(rem.clips)
    await rem.send({"t": "clip_get"})
    ok = await rem.wait(lambda: len(rem.clips) > n, 4)
    r.check("读取电脑剪贴板（往返一致）", ok and rem.clips[-1] == txt, rem.clips[-1:] if rem.clips else None)

    await rem.send({"t": "open", "url": "javascript:alert(1)"})
    ok, ts = await rem.toast(lambda t: "http" in t)
    r.check("打开网址只允许 http/https", ok and "只支持 http/https 网址" in ts, ts)

    await rem.send({"t": "settings", "scale": 0.5, "quality": 60, "fps": 15})
    ok = await rem.wait(lambda: rem.hello["geom"]["sw"] == round(g["width"] * 0.5), 6)
    img = await rem.full(10)
    r.check("切换到 50% 分辨率", ok and img is not None and img.size == (round(g["width"] * 0.5), round(g["height"] * 0.5)),
            rem.hello["geom"])
    if img:
        img.save(a.out / "frame-50.png")
    await rem.send({"t": "settings", "scale": 1, "quality": 65, "fps": 30})
    await rem.wait(lambda: rem.hello["geom"]["sw"] == g["width"], 6)

    # 上传文件
    name = "测试 上传 %d.txt" % int(time.time())
    body = ("你好，这是从“iPad”传过来的文件 %s\n" % name).encode()
    fd = aiohttp.FormData()
    fd.add_field("file", body, filename=name, content_type="text/plain")
    async with s.post(base + "/api/upload", data=fd, headers={"Origin": base}) as resp:
        j = await resp.json(content_type=None)
    saved = Path(j.get("saved", [""])[0]) if j.get("saved") else None
    r.check("上传文件", resp.status == 200 and j.get("ok") and saved and saved.exists() and saved.read_bytes() == body,
            j)
    if saved and saved.exists():
        r.note("上传保存位置", saved)
        saved.unlink()
    return rem


# ====================================================================== macOS：注入、隐私屏、崩溃重启、状态窗口
class Target:
    def __init__(self, d):
        self.dir = Path(d)
        self.layout = json.loads((self.dir / "layout.json").read_text("utf-8"))
        self.pos = 0

    def all(self):
        try:
            return [json.loads(ln) for ln in (self.dir / "events.jsonl").read_text("utf-8").splitlines() if ln.strip()]
        except (OSError, ValueError):
            return []

    def mark(self):
        self.pos = len(self.all())

    def new(self):
        return self.all()[self.pos:]

    def ns(self, typ):
        return [e for e in self.new() if e.get("ev") == "nsevent" and e.get("type") == typ]

    def last(self, ev):
        e = [x for x in self.new() if x.get("ev") == ev]
        return e[-1] if e else None

    def center(self, name):
        x, y, w, h = self.layout[name]
        return x + w / 2, y + h / 2


def mac_shot(a, tag, wait=6.0):
    """让状态窗口进程把自己的窗口（和隐私屏，如果开着）画成 PNG。"""
    req = Path(a.sup) / "data" / "shot.req"
    req.write_text(tag, "utf-8")
    end = time.time() + wait
    while time.time() < end and req.exists():
        time.sleep(0.2)
    time.sleep(0.5)
    return (Path(a.shots) / ("status-%s.png" % tag)).exists()


def region(img, g, rect, inset=6):
    ps = g.get("ps", 1.0)
    k = g["sw"] / g["width"]
    x, y, w, h = rect
    box = [(x - g["left"]) * ps * k + inset, (y - g["top"]) * ps * k + inset,
           (x - g["left"] + w) * ps * k - inset, (y - g["top"] + h) * ps * k - inset]
    return img.crop([int(v) for v in box])


def mean_diff(a_, b_):
    return sum(ImageStat.Stat(ImageChops.difference(a_.convert("RGB"), b_.convert("RGB"))).mean) / 3


def ui_pid():
    out = subprocess.run(["pgrep", "-f", "MacOS/iPadRemoteDesktop$"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else 0


def worker_pid(a):
    try:
        return int((Path(a.sup) / "data" / "pid.txt").read_text().strip())
    except (OSError, ValueError):
        return 0


def sh(cmd, timeout=30):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return (p.stdout + p.stderr).strip()


async def mac_full(a, r, s, base, rem):
    import mac_helpers as H
    T = Target(a.target)
    g = rem.hello["geom"]
    disp = H.display()
    r.check("画面尺寸 = 主屏像素尺寸（Retina 按像素）", [g["width"], g["height"]] == [int(v) for v in disp["px"]],
            "hello %sx%s，系统 %s" % (g["width"], g["height"], disp))

    def px(pt):
        ps = g.get("ps", 1.0)
        return {"x": round((pt[0] - g["left"]) * ps), "y": round((pt[1] - g["top"]) * ps)}

    async def click(pt, b=0, n=1):
        await rem.send(dict({"t": "click", "b": b, "n": n}, **px(pt)))

    pad, local_ok, upid = T.center("pad"), False, 0

    def dock_apps():
        out = sh("%s %s apps com.haoawake.ipad-remote-desktop" % (sys.executable, Path(__file__).with_name("mac_helpers.py")))
        try:
            return json.loads(out.splitlines()[-1])
        except (ValueError, IndexError):
            return out
    apps = dock_apps()
    r.check("程序坞里只有一个「iPad 远程桌面」（服务进程不另占一个图标）",
            isinstance(apps, list) and len([x for x in apps if x["policy"] == 0]) == 1, apps)
    # 诊断用：屏幕上都有哪些窗口（系统弹窗、崩溃报告之类挡在靶子上面的话，这里看得出来）
    (Path(a.out) / "windows-before-input.json").write_text(json.dumps(H.windows(), ensure_ascii=False, indent=1), "utf-8")
    # ---- 先确认 CI 这边能不能发“本机”事件（后面测隐私屏的拦截要用）
    try:
        T.mark()
        pad = T.center("pad")
        H.local_click(pad[0] + 30, pad[1])
        await asyncio.sleep(0.6)
        local_ok = bool(T.ns(1))
        r.note("CI 能模拟本机鼠标", local_ok)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        r.check("测试段没有出错：先确认 CI 这边能不能发“本机”事件（后面测隐私屏的拦截要用）", False, repr(e))

    # ---- 鼠标
    try:
        T.mark()
        await rem.send(dict({"t": "mm"}, **px(pad)))
        await asyncio.sleep(0.3)
        cur = H.cursor()
        r.check("注入：鼠标移动到指定位置（点坐标）", abs(cur[0] - pad[0]) <= 1 and abs(cur[1] - pad[1]) <= 1,
                "目标 %s，实际 %s" % (pad, cur))
        await click(pad)
        await asyncio.sleep(0.08)
        await click(pad)
        await asyncio.sleep(0.5)
        downs = T.ns(1)
        r.check("注入：左键单击", len(downs) >= 1 and downs[0].get("clicks") == 1 and T.ns(2), downs[:2])
        r.check("注入：连点两下被识别成双击（第二下 clickCount=2）", len(downs) >= 2 and downs[1].get("clicks") == 2,
                [d.get("clicks") for d in downs])
        await asyncio.sleep(0.7)
        T.mark()
        await click(pad, b=0, n=2)
        await asyncio.sleep(0.5)
        r.check("注入：快捷键面板的“双击”", [d.get("clicks") for d in T.ns(1)][:2] == [1, 2], [d.get("clicks") for d in T.ns(1)])
        T.mark()
        await click(pad, b=2)
        await asyncio.sleep(0.3)
        await click(pad, b=1)
        await asyncio.sleep(0.5)
        r.check("注入：右键", T.ns(3) and T.ns(4), len(T.ns(3)))
        r.check("注入：中键", T.ns(25) and T.ns(26), len(T.ns(25)))
        # 拖拽
        T.mark()
        x0, y0, w0, h0 = T.layout["pad"]
        p0 = (x0 + 20, y0 + 20)
        await rem.send(dict({"t": "mb", "b": 0, "d": True}, **px(p0)))
        for i in range(1, 7):
            await rem.send(dict({"t": "mm"}, **px((p0[0] + i * 25, p0[1] + i * 12))))
            await asyncio.sleep(0.03)
        await rem.send(dict({"t": "mb", "b": 0, "d": False}, **px((p0[0] + 150, p0[1] + 72))))
        await asyncio.sleep(0.5)
        r.check("注入：按住拖拽（产生 LeftMouseDragged 事件）", len(T.ns(6)) >= 4 and T.ns(2), "拖拽事件 %d 个" % len(T.ns(6)))
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        r.check("测试段没有出错：鼠标", False, repr(e))

    # ---- 滚动
    try:
        sc = T.center("scroll")
        await rem.send(dict({"t": "mm"}, **px(sc)))
        await asyncio.sleep(0.2)
        T.mark()
        for _ in range(6):
            await rem.send({"t": "wh", "dx": 0, "dy": -60})   # 手指往上推 = 往下滚
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.6)
        pos1 = T.last("scrollpos")
        sev = T.ns(22)
        r.check("注入：竖直滚动（像素级、方向和 Windows 一致）", pos1 and pos1["y"] > 100,
                "滚动后位置 %s" % pos1)
        r.check("注入：滚动是“精确滚动”（触控板那种，不是一格一格）", sev and all(e.get("precise") for e in sev),
                sev[:2])
        T.mark()
        await rem.send({"t": "wh", "dx": 0, "dy": 150})
        await asyncio.sleep(0.5)
        pos2 = T.last("scrollpos")
        r.check("注入：反方向滚动", pos2 and pos1 and pos2["y"] < pos1["y"], "%s → %s" % (pos1, pos2))
        T.mark()
        for _ in range(4):
            await rem.send({"t": "wh", "dx": 60, "dy": 0})    # Windows 语义：dx>0 = 往右滚（内容往左走）
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.5)
        pos3 = T.last("scrollpos")
        r.check("注入：水平滚动方向和 Windows 一致", pos3 and pos3["x"] > 50, pos3)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        r.check("测试段没有出错：滚动", False, repr(e))

    # ---- 键盘、文字
    try:
        await click(T.center("text"))
        await asyncio.sleep(0.4)
        await rem.send({"t": "combo", "codes": ["MetaLeft", "KeyA"]})
        await asyncio.sleep(0.2)
        T.mark()
        text = "Hello 你好 Mac 🍎 ü"
        await rem.send({"t": "text", "s": text})
        await asyncio.sleep(1.0)
        last = T.last("text")
        r.check("注入：输入文字（中文、emoji）", last and last["value"] == text, last)
        T.mark()
        await rem.send({"t": "combo", "codes": ["MetaLeft", "KeyA"]})
        await asyncio.sleep(0.2)
        keys = [e for e in T.ns(10) if e.get("code") == 0]
        r.check("注入：⌘A 带着 Command 标志", keys and keys[0]["flags"] & 0x100000, keys[:1])
        await rem.send({"t": "text", "s": "X"})
        await rem.send({"t": "combo", "codes": ["ShiftLeft", "KeyB"]})
        await rem.send({"t": "key", "code": "Enter", "d": True})
        await rem.send({"t": "key", "code": "Enter", "d": False})
        await rem.send({"t": "text", "s": "第二行"})
        await rem.send({"t": "combo", "codes": ["Backspace"]})
        await asyncio.sleep(0.8)
        last = T.last("text")
        r.check("注入：⌘A 全选后替换、⇧B、回车、退格", last and last["value"] == "XB\n第二", last)
        T.mark()
        await rem.send({"t": "combo", "codes": ["AltLeft", "ArrowLeft"]})
        await asyncio.sleep(0.3)
        arrows = [e for e in T.ns(10) if e.get("code") == 0x7B]
        r.check("注入：方向键带 Option 和 Fn 标志", arrows and arrows[0]["flags"] & 0x880000 == 0x880000, arrows[:1])
        sh("osascript -e 'set volume output volume 50'")
        vol0 = sh("osascript -e 'output volume of (get volume settings)'")
        await rem.send({"t": "combo", "codes": ["AudioVolumeUp"]})
        await asyncio.sleep(1.0)
        vol1 = sh("osascript -e 'output volume of (get volume settings)'")
        r.check("注入：音量键（系统媒体键）", vol0 != vol1, "%s → %s" % (vol0, vol1), required=False)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        r.check("测试段没有出错：键盘、文字", False, repr(e))

    # ---- 输入法：Mac 当前是拼音时，远程打的字也不能进候选框
    try:
        ok_ime = H.select_input_source("com.apple.inputmethod.SCIM.ITABC", "com.apple.inputmethod.SCIM")
        await asyncio.sleep(1.5)
        src0 = H.input_source_fresh()
        r.note("CI 切到拼音输入法", "%s，当前 %s" % (ok_ime, src0))
        if "inputmethod" in src0:
            async def clear():
                H.local_key(0x24)  # 回车：输入法里要是还有没上屏的字母，先让它上屏
                await asyncio.sleep(0.2)
                await rem.send({"t": "combo", "codes": ["MetaLeft", "KeyA"]})
                await rem.send({"t": "combo", "codes": ["Backspace"]})
                await asyncio.sleep(0.5)
            await click(T.center("text"))
            await asyncio.sleep(0.4)
            await clear()
            # 对照 1：本机真的按 a、空格 —— 拼音真在用的话，上屏的是候选的汉字（比如“啊”），不是 a
            T.mark()
            H.local_key(0x00)
            H.local_key(0x31)
            await asyncio.sleep(1.0)
            r.note("对照：拼音在用时本机按 a 空格", T.last("text"))
            await clear()
            # 对照 2：不切输入法、直接发“字写在事件里”的按键（就是没有这项改进时的做法）
            T.mark()
            H.local_text("hi 你 ok")
            await asyncio.sleep(1.0)
            r.note("对照：拼音在用时不切输入法、直接注入 hi 你 ok", "%s；输入法 %s" % (T.last("text"), H.input_source_fresh()))
            await clear()
            T.mark()
            await rem.send({"t": "text", "s": "hello 世界 ok"})
            await asyncio.sleep(1.5)
            last = T.last("text")
            r.check("注入：Mac 当前是拼音输入法时，远程打的英文和中文照样直接上屏", last and last["value"] == "hello 世界 ok",
                    "%s；打完后输入法 %s" % (last, H.input_source_fresh()))
            # 远程断开（CI 里“断开后 2 分钟”是 5 秒）以后，Mac 切回原来的拼音
            await rem.close()
            await asyncio.sleep(8)
            back = H.input_source_fresh()
            r.check("远程断开一会儿后，Mac 切回原来的输入法", back == src0, "%s → %s" % (src0, back))
            r.note("日志里的输入法切换", [ln for ln in (Path(a.sup) / "data" / "app.log").read_text("utf-8").splitlines()
                                     if "输入法" in ln][-4:])
            rem = await connect(s, base)
            await rem.wait(lambda: rem.hello, 5)
        H.select_input_source("com.apple.keylayout.ABC") or H.select_input_source("com.apple.keylayout.US")
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        r.check("测试段没有出错：输入法", False, repr(e))

    # ---- 帧率（右上角色块每帧变色）
    try:
        (Path(a.target) / "anim").write_text("1")
        for scale in (1.0, 0.5):
            await rem.send({"t": "settings", "scale": scale, "quality": 65, "fps": 30})
            await asyncio.sleep(1.5)
            f0, b0, t0 = rem.frames, rem.bytes, time.monotonic()
            await asyncio.sleep(4)
            dt = time.monotonic() - t0
            r.note("帧率 scale=%.2f" % scale, "%.1f fps，%.0f KB/s（只有一小块在动），画面 %sx%s" % (
                (rem.frames - f0) / dt, (rem.bytes - b0) / 1024 / dt, rem.hello["geom"]["sw"], rem.hello["geom"]["sh"]))
        (Path(a.target) / "anim").unlink()
        await rem.send({"t": "settings", "scale": 1, "quality": 65, "fps": 30})
        await rem.wait(lambda: rem.hello["geom"]["sw"] == g["width"], 6)
        wp = worker_pid(a)
        r.note("服务进程 CPU / 内存", sh("ps -o %%cpu=,rss= -p %d" % wp))
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        r.check("测试段没有出错：帧率（右上角色块每帧变色）", False, repr(e))

    # ---- 隐私屏
    try:
        before = await rem.full()
        win = T.layout["window"]
        if before:
            before.save(a.out / "frame-before-privacy.png")
        T.mark()
        n = len(rem.privacy)
        await rem.send({"t": "privacy", "on": True})
        ok = await rem.wait(lambda: any(m.get("on") for m in rem.privacy[n:]), 5)
        r.check("隐私屏：打开（服务端确认）", ok, rem.privacy[n:])
        await asyncio.sleep(2.5)
        upid = ui_pid()
        wins = [w for w in H.windows(upid) if w["layer"] >= 1000]
        r.check("隐私屏：每块屏幕上都有一个最高层级的窗口", wins, wins)
        after = await rem.full()
        if after:
            after.save(a.out / "frame-privacy-on.png")
        H.screencapture(a.out / "screen-privacy-on.png")
        H.cg_capture(a.out / "cgcapture-privacy-on.png")
        if before and after:
            d = mean_diff(region(before, g, win), region(after, g, win))
            lum = ImageStat.Stat(region(after, g, win).convert("L")).mean[0]
            r.check("隐私屏：iPad 收到的画面里没有锁屏画面（还是真实桌面）", d < 14 and lum > 70,
                    "和打开前相差 %.1f，亮度 %.0f" % (d, lum))
        try:
            sc_img = Image.open(a.out / "screen-privacy-on.png").convert("RGB")
            sw_, sh_ = sc_img.size
            mid = sc_img.crop((sw_ * 0.3, sh_ * 0.55, sw_ * 0.7, sh_ * 0.8)).convert("L")
            r.check("隐私屏：Mac 屏幕上确实显示锁屏画面（screencapture）", ImageStat.Stat(mid).mean[0] < 40,
                    "亮度 %.0f" % ImageStat.Stat(mid).mean[0])
        except Exception as e:  # noqa: BLE001
            r.check("隐私屏：Mac 屏幕上确实显示锁屏画面（screencapture）", False, e, required=False)
        mac_shot(a, "privacy")
        # 远程的点击照常生效
        T.mark()
        await click(pad)
        await asyncio.sleep(0.5)
        r.check("隐私屏：远程点击照常生效", T.ns(1), len(T.ns(1)))
        # 本机的点击和按键被拦下
        if local_ok:
            T.mark()
            H.local_click(pad[0] + 10, pad[1] + 10)
            H.local_key(0x07)  # x
            await asyncio.sleep(0.8)
            r.check("隐私屏：本机鼠标键盘被拦下", not T.ns(1) and not T.ns(10), T.new()[:4])
            H.local_key(0x24)  # 远程还在用：按 Enter 不应该锁屏
            await asyncio.sleep(2)
            r.check("隐私屏：远程在用时本机按 Enter 不会锁屏", not H.locked())
        occ = [e for e in T.all() if e.get("ev") == "occlusion"]
        r.note("靶子窗口遮挡状态变化", occ[-4:])
        r.check("隐私屏：下面的窗口没被系统当成“被遮住”（照常刷新）", not occ or occ[-1]["visible"], occ[-2:],
                required=False)
        n = len(rem.privacy)
        await rem.send({"t": "privacy", "on": False})
        ok = await rem.wait(lambda: any(m.get("on") is False for m in rem.privacy[n:]), 5)
        await asyncio.sleep(1.5)
        left = [w for w in H.windows(upid) if w["layer"] >= 1000]
        r.check("隐私屏：关闭后窗口消失", ok and not left, left)
        if local_ok:
            T.mark()
            H.local_click(pad[0] + 10, pad[1] + 10)
            await asyncio.sleep(0.6)
            r.check("隐私屏：关闭后本机鼠标恢复", T.ns(1))
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        r.check("测试段没有出错：隐私屏", False, repr(e))

    # ---- 打开网址、打开程序
    try:
        await rem.send({"t": "launch", "app": "activity"})
        ok, ts = await rem.toast(lambda t: "活动监视器" in t, 8)
        r.check("Mac 快捷键面板：打开活动监视器", ok and any("已在 Mac 上打开" in t for t in ts), ts)
        await rem.send({"t": "open", "url": "http://127.0.0.1:%d/health?q=中文 测试" % a.port})
        ok, ts = await rem.toast(lambda t: "打开" in t, 8)
        r.check("在 Mac 上打开网址（含中文和空格）", ok and any(t.startswith("已在电脑上打开") for t in ts), ts)
        await asyncio.sleep(3)
        H.screencapture(a.out / "screen-after-open.png")
        sh("pkill -x 'Activity Monitor'; pkill -x Safari")
        await rem.close()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        r.check("测试段没有出错：打开网址、打开程序", False, repr(e))

    # ---- 服务进程崩溃 → 5 秒后自动重启
    try:
        old = worker_pid(a)
        if old:
            os.kill(old, signal.SIGKILL)
            await asyncio.sleep(2)
            mac_shot(a, "restarting")
            up = False
            for _ in range(40):
                await asyncio.sleep(0.5)
                try:
                    async with s.get(base + "/health") as resp:
                        up = resp.status == 200
                except aiohttp.ClientError:
                    up = False
                if up and worker_pid(a) != old:
                    break
            r.check("服务进程被强制结束后自动重启", up and worker_pid(a) not in (0, old), "旧 %d 新 %d" % (old, worker_pid(a)))
        await asyncio.sleep(3)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        r.check("测试段没有出错：服务进程崩溃 → 5 秒后自动重启", False, repr(e))

    # ---- 状态窗口：开机自启、重置密码（模拟在 Mac 上点按钮）
    try:
        apps = dock_apps()
        r.check("服务进程重启以后，程序坞里还是只有一个「iPad 远程桌面」", isinstance(apps, list) and len([x for x in apps if x["policy"] == 0]) == 1, apps)
        sh("pkill -f tests/mac_target.py")  # 靶子窗口浮在最上面，会挡住状态窗口下半部分的按钮
        await asyncio.sleep(1)
        sh("open -a '%s'" % a.app)  # 再“打开”一次 = 把已有的窗口叫到前面
        await asyncio.sleep(2)
        mac_shot(a, "running")
        lay = json.loads((Path(a.shots) / "ui-layout.json").read_text("utf-8"))
        r.check("状态窗口：再次打开时窗口在前面", lay.get("visible"), lay)
        front = sh("%s %s front" % (sys.executable, Path(__file__).with_name("mac_helpers.py"))).splitlines()[-1:]
        r.check("再次打开后「iPad 远程桌面」在最前面、菜单栏是它的", front and '"com.haoawake.ipad-remote-desktop"' in front[0]
                and '"menubar": true' in front[0], front)
        H.screencapture(a.out / "screen-status-front.png")
        la = Path.home() / "Library" / "LaunchAgents" / "com.haoawake.ipad-remote-desktop.plist"
        if local_ok and "autostart" in lay:
            x, y, w, h = lay["autostart"]
            H.local_click(x + 9, y + h / 2)
            await asyncio.sleep(1.5)
            r.check("状态窗口：勾选“登录 Mac 后自动启动”→ 写入 LaunchAgent", la.exists(), sh("plutil -p '%s'" % la))
            if la.exists():
                r.check("LaunchAgent 格式正确、指向 .app 里的程序",
                        "OK" in sh("plutil -lint '%s'" % la) and "MacOS/iPadRemoteDesktop" in la.read_text(errors="ignore"))
            mac_shot(a, "autostart")
            lay = json.loads((Path(a.shots) / "ui-layout.json").read_text("utf-8"))
            x, y, w, h = lay["autostart"]
            H.local_click(x + 9, y + h / 2)
            await asyncio.sleep(1.5)
            r.check("状态窗口：取消勾选 → 删掉 LaunchAgent", not la.exists())
        if local_ok and "reset_password" in lay:
            old_pw = json.loads((Path(a.sup) / "config.json").read_text("utf-8"))["password"]
            x, y, w, h = lay["reset_password"]
            H.local_click(x + w / 2, y + h / 2)
            await asyncio.sleep(1.5)
            mac_shot(a, "reset-confirm")
            H.screencapture(a.out / "screen-reset-confirm.png")
            H.local_key(0x24)  # 确认框里按回车 = “重置”
            await asyncio.sleep(4)
            new_pw = json.loads((Path(a.sup) / "config.json").read_text("utf-8"))["password"]
            r.check("状态窗口：重置密码", new_pw != old_pw and re.fullmatch(r"[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}", new_pw),
                    "%s → %s" % (old_pw, new_pw))
            async with s.get(base + "/api/me") as resp:
                r.check("重置密码后旧的登录失效", resp.status == 401, resp.status)
            st, j, _ = await login(s, base, new_pw)
            r.check("新密码可以登录", st == 200, j)
            a.password = new_pw
            mac_shot(a, "after-reset")
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        r.check("测试段没有出错：状态窗口：开机自启、重置密码（模拟在 Mac 上点按钮）", False, repr(e))

    # ---- 最后：Enter 锁屏（锁了就解不开了，所以放最后）
    try:
        rem = await connect(s, base, "scale=0.5&quality=60&fps=15&privacy=1")
        ok = await rem.wait(lambda: rem.hello and rem.hello.get("privacy"), 5)
        r.check("iPad 连上时自动打开隐私屏（privacy=1）", ok, rem.hello and rem.hello.get("privacy"))
        await asyncio.sleep(1.5)
        H.screencapture(a.out / "screen-privacy-connected.png")
        mac_shot(a, "privacy-connected")
        await rem.close()
        await asyncio.sleep(7)  # CI 里把“断开后 2 分钟”调成了 5 秒（RD_PRIVACY_GRACE）
        mac_shot(a, "privacy-idle")
        H.screencapture(a.out / "screen-privacy-idle.png")
        if local_ok:
            H.local_key(0x24)
            ok = False
            for _ in range(20):
                await asyncio.sleep(0.5)
                if H.locked():
                    ok = True
                    break
            r.check("隐私屏：远程断开后本机按 Enter → Mac 锁屏", ok)
            await asyncio.sleep(3)
            H.screencapture(a.out / "screen-locked.png")
            r.note("锁屏后的隐私屏窗口", [w for w in H.windows(ui_pid()) if w["layer"] >= 1000])
            rem = await connect(s, base, "scale=0.5&quality=60&fps=15&privacy=1")
            ok = await rem.wait(lambda: rem.locked > 0, 6)
            r.check("锁屏时 iPad 收到“已锁屏”提示", ok, rem.locked, required=False)
            await rem.close()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        r.check("测试段没有出错：最后：Enter 锁屏（锁了就解不开了，所以放最后）", False, repr(e))


# ====================================================================== 入口
async def amain(a):
    r = Results(a.out)
    base = "http://127.0.0.1:%d" % a.port
    jar = aiohttp.CookieJar(unsafe=True)  # 127.0.0.1 这种 IP 地址的 Cookie 也要收
    async with aiohttp.ClientSession(cookie_jar=jar, timeout=aiohttp.ClientTimeout(total=60)) as s:
        try:
            rem = await basic(a, r, s, base)
            if rem and a.mac_full:
                await mac_full(a, r, s, base, rem)
            elif rem:
                await rem.close()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            r.check("测试脚本本身没有出错", False, repr(e))
    return r.save()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--password", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--os", default="win")
    ap.add_argument("--mac-full", action="store_true")
    ap.add_argument("--input-required", action="store_true")
    ap.add_argument("--target")
    ap.add_argument("--sup")
    ap.add_argument("--app")
    ap.add_argument("--shots")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    ok = asyncio.run(amain(a))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    main()
