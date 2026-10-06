"""Web 服务：登录鉴权、画面推流 WebSocket、输入注入、剪贴板、打开网址、文件上传。"""
import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

import capture
from common import DATA, MAC, WEB

if MAC:
    import macapi as osapi
    import privacy_mac as privacy
else:
    import privacy
    import winapi as osapi

log = logging.getLogger("rd")

COOKIE = "rd_session"
SESSION_DAYS = 30
MAX_INFLIGHT = 2
PRESETS = {"saver": (0.5, 55, 15), "balanced": (0.75, 65, 30), "sharp": (1.0, 75, 30)}


# ====================================================================== 鉴权
class Auth:
    def __init__(self, password):
        self.password = password
        self.path = DATA / "sessions.json"
        self.sessions = {}
        self.fail_ip = {}
        self.fail_all = []
        self.locked_until = 0
        try:
            self.sessions = {k: v for k, v in json.loads(self.path.read_text("utf-8")).items() if v > time.time()}
        except Exception:
            pass

    def _save(self):
        DATA.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self.sessions), "utf-8")

    @staticmethod
    def _h(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def check(self, request):
        tok = request.cookies.get(COOKIE)
        if not tok:
            return False
        exp = self.sessions.get(self._h(tok))
        return bool(exp and exp > time.time())

    def login(self, password, ip, remember):
        now = time.time()
        if now < self.locked_until:
            return None, "尝试次数过多，请 %d 秒后再试" % int(self.locked_until - now)
        fails = [t for t in self.fail_ip.get(ip, []) if now - t < 600]
        if len(fails) >= 5:
            return None, "该地址尝试次数过多，请 10 分钟后再试"
        if not hmac.compare_digest(password.strip().encode(), self.password.encode()):
            fails.append(now)
            self.fail_ip[ip] = fails
            self.fail_all = [t for t in self.fail_all if now - t < 600] + [now]
            if len(self.fail_all) >= 20:
                self.locked_until = now + 600
            log.warning("登录失败 ip=%s", ip)
            return None, "密码错误"
        self.fail_ip.pop(ip, None)
        tok = secrets.token_urlsafe(32)
        self.sessions[self._h(tok)] = now + (SESSION_DAYS * 86400 if remember else 12 * 3600)
        self.sessions = {k: v for k, v in self.sessions.items() if v > now}
        self._save()
        log.info("登录成功 ip=%s", ip)
        return tok, None

    def logout(self, request):
        tok = request.cookies.get(COOKIE)
        if tok and self.sessions.pop(self._h(tok), None):
            self._save()


def client_ip(request):
    h = request.headers
    return (h.get("Cf-Connecting-Ip") or h.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote or "?")


def is_https(request):
    h = request.headers
    return request.secure or h.get("X-Forwarded-Proto") == "https" or '"https"' in h.get("Cf-Visitor", "")


def same_origin(request):
    origin = request.headers.get("Origin")
    if not origin:
        return True
    host = request.headers.get("X-Forwarded-Host") or request.host
    return urlparse(origin).netloc.lower() == host.lower()


# ====================================================================== 远程会话
class Session:
    def __init__(self, ws, app):
        self.ws = ws
        self.app = app
        self.ex = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="cap")
        self.enc = capture.Encoder()
        self.geom = None
        self.fps = 30
        self.inflight = 0
        self.ack_event = asyncio.Event()
        self.wake = asyncio.Event()
        self.pending_cfg = None
        self.last_change = 0.0
        self.last_input = 0.0
        self.frac = [0.0, 0.0]
        self.alive = True

    async def run_ex(self, fn, *a):
        return await asyncio.get_running_loop().run_in_executor(self.ex, fn, *a)

    async def send_json(self, obj):
        if not self.ws.closed:
            await self.ws.send_str(json.dumps(obj, ensure_ascii=False))

    async def send_hello(self):
        await self.send_json({"t": "hello", "geom": self.geom, "fps": self.fps,
                              "quality": self.enc.quality, "monitors": self.app["monitors"],
                              "host": osapi.host_name(), "os": osapi.OS, "privacy": privacy_state(self.app)})

    def apply_settings(self, m):
        cfg = {}
        if m.get("preset") in PRESETS:
            s, q, f = PRESETS[m["preset"]]
            cfg.update(scale=s, quality=q)
            self.fps = f
        for k in ("scale", "quality", "monitor"):
            if m.get(k) is not None:
                cfg[k] = m[k]
        if m.get("fps"):
            self.fps = int(min(max(int(m["fps"]), 2), 60))
        self.pending_cfg = cfg
        self.wake.set()

    # ---- 推流循环
    async def stream(self):
        loop = asyncio.get_running_loop()
        if self.pending_cfg is not None:
            cfg, self.pending_cfg = self.pending_cfg, None
            await self.run_ex(lambda: self.enc.configure(**cfg))
        self.geom = await self.run_ex(self.enc.geometry)
        await self.send_hello()
        while self.alive and not self.ws.closed:
            if self.inflight >= MAX_INFLIGHT:
                self.ack_event.clear()
                try:
                    await asyncio.wait_for(self.ack_event.wait(), 5)
                except asyncio.TimeoutError:
                    self.inflight = 0
                continue
            t0 = loop.time()
            if self.pending_cfg is not None:
                cfg, self.pending_cfg = self.pending_cfg, None
                await self.run_ex(lambda: self.enc.configure(**cfg))
            if self.app["locked"]:
                await self.send_json({"t": "locked"})
                await asyncio.sleep(1)
                self.enc.force_full = True
                continue
            try:
                msg, info = await self.run_ex(self.enc.produce)
            except Exception as e:  # 锁屏/UAC 时截图可能失败
                log.warning("截图失败: %s", e)
                await asyncio.sleep(0.5)
                continue
            if self.enc.geom != self.geom:
                self.geom = self.enc.geom
                await self.send_hello()
            if msg:
                self.inflight += 1
                if info["tiles"] > 3:  # 光标闪烁、时钟跳秒这种小变化不算“忙”
                    self.last_change = loop.time()
                await self.ws.send_bytes(msg)
            # 自适应轮询：有操作/大面积变化时按目标帧率，否则降到 8fps 省 CPU
            busy = loop.time() - max(self.last_change, self.last_input) < 1.5
            interval = 1 / self.fps if busy else 1 / 8
            delay = interval - (loop.time() - t0)
            if delay > 0:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), delay)
                except asyncio.TimeoutError:
                    pass

    async def cursor_loop(self):
        last = None
        last_h = None
        loop = asyncio.get_running_loop()
        while self.alive and not self.ws.closed:
            vis, x, y, h = osapi.cursor_info()
            g = self.geom
            if g:
                lx, ly = osapi.to_local(g, x, y)
                pos = (vis, round(lx), round(ly))
                if pos != last:
                    last = pos
                    await self.send_json({"t": "c", "v": vis, "x": pos[1], "y": pos[2]})
                if h != last_h:
                    last_h = h
                    shape = await loop.run_in_executor(None, osapi.cursor_shape, h)
                    if shape:
                        await self.send_json(dict(t="cs", **shape))
            await asyncio.sleep(0.025)

    # ---- 输入
    def to_screen(self, m):
        return osapi.to_global(self.geom, float(m["x"]), float(m["y"]))

    async def handle(self, m):
        t = m.get("t")
        if t == "ack":
            self.inflight = max(0, self.inflight - 1)
            self.ack_event.set()
            return
        if t == "ping":
            await self.send_json({"t": "pong", "ts": m.get("ts")})
            return
        self.last_input = asyncio.get_running_loop().time()
        self.wake.set()
        if t == "mm":
            osapi.move_to(*self.to_screen(m))
        elif t == "mr":
            self.frac[0] += float(m["dx"])
            self.frac[1] += float(m["dy"])
            ix, iy = int(self.frac[0]), int(self.frac[1])
            self.frac[0] -= ix
            self.frac[1] -= iy
            if ix or iy:
                osapi.move_rel(ix, iy)
        elif t == "mb":
            if "x" in m:
                osapi.move_to(*self.to_screen(m))
            osapi.button(m.get("b", 0), bool(m.get("d")))
        elif t == "click":
            if "x" in m:
                osapi.move_to(*self.to_screen(m))
            osapi.click(m.get("b", 0), m.get("n", 1))
        elif t == "wh":
            osapi.wheel(int(m.get("dx", 0)), int(m.get("dy", 0)))
        elif t == "key":
            osapi.key(str(m.get("code")), bool(m.get("d")))
        elif t == "combo":
            osapi.combo([str(c) for c in m.get("codes", [])][:6])
        elif t == "text":
            osapi.type_text(str(m.get("s", ""))[:5000])
        elif t == "release":
            osapi.release_modifiers()
        elif t == "settings":
            self.apply_settings(m)
        elif t == "refresh":
            self.enc.force_full = True
        elif t == "clip_get":
            text = await asyncio.get_running_loop().run_in_executor(None, osapi.clipboard_get)
            await self.send_json({"t": "clip", "text": text or ""})
        elif t == "clip_set":
            ok = await asyncio.get_running_loop().run_in_executor(None, osapi.clipboard_set, str(m.get("text", "")))
            await self.send_json({"t": "toast", "msg": "已写入电脑剪贴板" if ok else "写入剪贴板失败"})
        elif t == "open":
            await self.send_json({"t": "toast", "msg": open_url(str(m.get("url", "")))})
        elif t == "privacy":
            await set_privacy(self.app, bool(m.get("on")), announce=True)
        elif t == "launch" and hasattr(osapi, "launch_app"):  # 只有 macOS 端的快捷键面板会发
            await self.send_json({"t": "toast", "msg": osapi.launch_app(str(m.get("app", "")))})

    async def close(self):
        self.alive = False
        self.ack_event.set()
        self.wake.set()
        try:
            osapi.release_modifiers()
        except Exception:
            pass
        await self.run_ex(self.enc.close)
        self.ex.shutdown(wait=False)


# ====================================================================== 隐私屏
# 断开后 2 分钟内仍算“远程在用”（Safari 切到后台会断开，回来会重连）。RD_PRIVACY_GRACE 只给自动化测试用
PRIVACY_GRACE = float(os.environ.get("RD_PRIVACY_GRACE") or 120)


def remote_active(app):
    return bool(app["sessions"]) or time.time() - app["last_disconnect"] < PRIVACY_GRACE


def privacy_state(app):
    c = app.get("privacy")
    return c.shown if c else None


async def set_privacy(app, on, announce=False, reason=None):
    c = app.get("privacy")
    if not c or c.shown == on:
        if c and announce:  # 状态没变也回一下，让按钮和实际一致
            await broadcast(app, {"t": "privacy", "on": c.shown})
        return
    c.show() if on else c.hide()
    msg = reason or ("隐私屏已开启：电脑屏幕显示为锁屏画面，你这边照常操作" if on else "隐私屏已关闭：电脑屏幕恢复正常显示")
    await broadcast(app, {"t": "privacy", "on": on, "msg": msg if (announce or reason) else None})


async def broadcast(app, obj):
    for s in list(app["sessions"]):
        try:
            await s.send_json(obj)
        except Exception:
            pass


def open_url(url):
    url = url.strip()
    if not url:
        return "网址为空"
    # 带了别的协议（javascript:、file:、mailto: …）直接拒绝；“域名:端口”（bilibili.com:443）不算协议
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(?!\d+(?:[/?#]|$))", url)
    if m and m.group(1).lower() not in ("http", "https"):
        return "只支持 http/https 网址"
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    if urlparse(url).scheme.lower() not in ("http", "https"):
        return "只支持 http/https 网址"
    try:
        osapi.open_url(url)
        log.info("打开网址 %s", url)
        return "已在电脑上打开：" + url
    except Exception as e:
        return "打开失败：%s" % e


# ====================================================================== HTTP 路由
def no_cache(resp):
    resp.headers["Cache-Control"] = "no-cache"
    return resp


async def index(request):
    return no_cache(web.FileResponse(WEB / "index.html"))


async def health(request):
    return web.Response(text="ok")


async def api_me(request):
    if not request.app["auth"].check(request):
        return web.json_response({"ok": False}, status=401)
    return web.json_response({"ok": True, "host": osapi.host_name(), "os": osapi.OS})


async def api_login(request):
    if not same_origin(request):
        return web.json_response({"ok": False, "error": "来源不匹配"}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    auth = request.app["auth"]
    await asyncio.sleep(0.4)  # 拖慢暴力尝试
    tok, err = auth.login(str(body.get("password", "")), client_ip(request), bool(body.get("remember", True)))
    if err:
        return web.json_response({"ok": False, "error": err}, status=403)
    resp = web.json_response({"ok": True})
    resp.set_cookie(COOKIE, tok, max_age=SESSION_DAYS * 86400 if body.get("remember", True) else None,
                    httponly=True, samesite="Lax", secure=is_https(request), path="/")
    return resp


async def api_logout(request):
    request.app["auth"].logout(request)
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


async def api_upload(request):
    if not request.app["auth"].check(request) or not same_origin(request):
        return web.json_response({"ok": False, "error": "未登录"}, status=401)
    target = Path(os.path.expandvars(os.path.expanduser(request.app["config"]["upload_dir"])))
    target.mkdir(parents=True, exist_ok=True)
    reader = await request.multipart()
    saved = []
    while True:
        part = await reader.next()
        if part is None:
            break
        if not part.filename:
            continue
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", os.path.basename(part.filename)).strip() or "file"
        dest = target / name
        stem, suf, i = dest.stem, dest.suffix, 1
        while dest.exists():
            dest = target / ("%s (%d)%s" % (stem, i, suf))
            i += 1
        with open(dest, "wb") as f:
            while True:
                chunk = await part.read_chunk(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        saved.append(str(dest))
        log.info("收到文件 %s", dest)
    return web.json_response({"ok": True, "saved": saved, "dir": str(target)})


async def ws_handler(request):
    app = request.app
    if not app["auth"].check(request):
        return web.Response(status=401, text="unauthorized")
    if not same_origin(request):
        return web.Response(status=403, text="bad origin")
    ws = web.WebSocketResponse(heartbeat=20, compress=False, max_msg_size=8 << 20)
    await ws.prepare(request)
    sess = Session(ws, app)
    init = {}
    for k in ("scale", "quality", "fps", "monitor"):
        try:
            init[k] = float(request.query[k])
        except (KeyError, ValueError):
            pass
    if init:
        sess.apply_settings(init)
    app["sessions"].add(sess)
    log.info("远程会话开始 ip=%s", client_ip(request))
    if request.query.get("privacy") in ("0", "1"):  # iPad 端记住的偏好（默认开）
        await set_privacy(app, request.query["privacy"] == "1")
    for warn in getattr(osapi, "permission_warnings", list)():  # macOS：还没授权屏幕录制/辅助功能
        await sess.send_json({"t": "toast", "msg": warn, "ms": 9000})
    tasks = [asyncio.create_task(sess.stream()), asyncio.create_task(sess.cursor_loop())]
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    await sess.handle(json.loads(msg.data))
                except Exception as e:
                    log.warning("处理消息出错: %s %r", e, msg.data[:200])
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        for t in tasks:
            t.cancel()
        await sess.close()
        app["sessions"].discard(sess)
        app["last_disconnect"] = time.time()
        log.info("远程会话结束 ip=%s", client_ip(request))
    return ws


@web.middleware
async def security_headers(request, handler):
    resp = await handler(request)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


async def lock_watch(app):
    while True:
        was = app["locked"]
        try:
            app["locked"] = osapi.is_session_locked()
        except Exception:
            app["locked"] = False
        # 有人在电脑前用 Windows 密码/PIN（Mac 登录密码）解了锁 → 一定是主人，撤掉隐私屏
        if was and not app["locked"] and privacy_state(app):
            who = "Mac 登录密码" if MAC else "Windows 密码"
            log.info("本地用 %s解锁，隐私屏自动关闭", who)
            await set_privacy(app, False, reason="有人在电脑上用 %s解锁了，隐私屏已自动关闭" % who)
        await asyncio.sleep(1)


async def on_startup(app):
    app["lock_task"] = asyncio.create_task(lock_watch(app))
    c = app.get("privacy")
    if c and hasattr(c, "set_listener"):
        # macOS：隐私屏由状态窗口进程托管，它自己撤掉时（比如 Mac 解锁了）要告诉所有 iPad
        loop = asyncio.get_running_loop()

        def changed(on, reason):
            msg = {"t": "privacy", "on": on, "msg": reason}
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(broadcast(app, msg)))
        c.set_listener(changed)


def make_app(config):
    app = web.Application(middlewares=[security_headers], client_max_size=4 << 20)
    app["config"] = config
    app["auth"] = Auth(config["password"])
    app["sessions"] = set()
    app["locked"] = False
    app["last_disconnect"] = 0.0
    app["monitors"] = capture.list_monitors()
    app["privacy"] = None
    if config.get("privacy", True):
        curtain = privacy.PrivacyCurtain(block_input=config.get("privacy_block_input", True),
                                         remote_active=lambda: remote_active(app))
        if curtain.start():
            app["privacy"] = curtain
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/me", api_me)
    app.router.add_post("/api/login", api_login)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_post("/api/upload", api_upload)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static/", WEB, append_version=False)
    app.on_startup.append(on_startup)
    return app
