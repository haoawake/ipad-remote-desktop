"""内网穿透：
1. Tailscale（主通道）：点对点直连、地址固定、全程加密。用 `tailscale serve` 把 tailnet 的 80 端口转到本机服务，
   无需改防火墙，也无需管理员权限。
2. Cloudflare 临时隧道（备用通道）：不需要账号，iPad 不装任何 App 也能用 https 访问；
   缺点是每次重启地址会变，所以变了就通过 ntfy 推送到手机/iPad。
"""
import asyncio
import json
import logging
import os
import re
import socket
import subprocess
import time
import winreg
from pathlib import Path

import aiohttp
from aiohttp.abc import AbstractResolver

import winapi

log = logging.getLogger("rd.tunnel")
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

TS_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale"
TS_CLI = TS_DIR / "tailscale.exe"
TS_GUI = TS_DIR / "tailscale-ipn.exe"


async def run_cmd(*args, timeout=20):
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "timeout"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def system_proxy():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            if winreg.QueryValueEx(k, "ProxyEnable")[0]:
                server = winreg.QueryValueEx(k, "ProxyServer")[0]
                if "=" in server:  # 形如 http=127.0.0.1:7890;https=...
                    server = dict(p.split("=", 1) for p in server.split(";") if "=" in p).get("http", "")
                return "http://" + server if server and "://" not in server else server
    except OSError:
        pass
    return None


# ====================================================================== Tailscale
class Tailscale:
    def __init__(self, port, on_change):
        self.port = port
        self.on_change = on_change
        self.state = "未知"
        self.ip = None
        self.dns = None
        self.serving = False
        self.ipad_online = None

    @property
    def available(self):
        return TS_CLI.exists()

    async def status(self):
        code, out, _ = await run_cmd(str(TS_CLI), "status", "--json")
        if code != 0 and not out.strip().startswith("{"):
            return None
        try:
            return json.loads(out)
        except ValueError:
            return None

    def _gui_running(self):
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq tailscale-ipn.exe", "/NH"],
                             capture_output=True, text=True, creationflags=NO_WINDOW).stdout
        return "tailscale-ipn.exe" in out.lower()

    async def ensure_serve(self):
        # 用 TCP 转发而不是 --http：--http 按 Host 头路由，用 IP 访问会 404；TCP 转发两种都行
        want = "127.0.0.1:%d" % self.port
        code, out, _ = await run_cmd(str(TS_CLI), "serve", "status", "--json")
        try:
            if code == 0 and json.loads(out or "{}").get("TCP", {}).get("80", {}).get("TCPForward") == want:
                return True
        except ValueError:
            pass
        args = [str(TS_CLI), "serve", "--bg", "--yes", "--tcp=80", "tcp://" + want]
        code, out, err = await run_cmd(*args, timeout=30)
        if code != 0:  # 端口 80 上可能残留旧的 http 配置，清掉再试
            await run_cmd(str(TS_CLI), "serve", "--http=80", "off")
            code, out, err = await run_cmd(*args, timeout=30)
        if code != 0:
            log.warning("tailscale serve 失败: %s %s", out.strip(), err.strip())
            return False
        log.info("tailscale serve 已配置：tailnet:80 -> %s", want)
        return True

    async def loop(self):
        if not self.available:
            self.state = "未安装"
            self.on_change()
            return
        started_gui_at = 0
        while True:
            try:
                st = await self.status()
                backend = (st or {}).get("BackendState", "?")
                if backend != "Running":
                    # Windows 上 tailscaled 要等托盘程序连上才会启动；托盘没开就帮它开
                    if not self._gui_running() and time.time() - started_gui_at > 60 and TS_GUI.exists():
                        log.info("Tailscale 未运行(%s)，启动托盘程序", backend)
                        subprocess.Popen([str(TS_GUI)], creationflags=NO_WINDOW)
                        started_gui_at = time.time()
                    new = ("需要登录" if backend == "NeedsLogin" else "未连接(%s)" % backend), None, None, False
                else:
                    me = st.get("Self", {})
                    ips = [i for i in me.get("TailscaleIPs") or [] if "." in i]
                    dns = (me.get("DNSName") or "").rstrip(".")
                    serving = await self.ensure_serve()
                    new = "已连接", (ips[0] if ips else None), dns, serving
                    peers = [p for p in (st.get("Peer") or {}).values() if p.get("OS") == "iOS"]
                    self.ipad_online = any(p.get("Online") for p in peers) if peers else None
                if new != (self.state, self.ip, self.dns, self.serving):
                    self.state, self.ip, self.dns, self.serving = new
                    self.on_change()
            except Exception as e:
                log.warning("Tailscale 检查出错: %s", e)
            await asyncio.sleep(30)

    def urls(self):
        if self.state != "已连接" or not self.ip:
            return []
        if self.serving:
            out = ["http://%s" % self.ip]
            if self.dns:
                out.append("http://%s" % self.dns)
            return out
        return ["http://%s:%d" % (self.ip, self.port)]


# ====================================================================== Cloudflare 临时隧道
class DohResolver(AbstractResolver):
    """健康检查专用：绕过系统 DNS（Windows 会把刚创建域名的 NXDOMAIN 缓存很久）。"""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        last = None
        for url in ("https://1.1.1.1/dns-query", "https://8.8.8.8/resolve"):
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
                    async with s.get(url, params={"name": host, "type": "A"},
                                     headers={"accept": "application/dns-json"}) as r:
                        j = await r.json(content_type=None)
                ips = [a["data"] for a in j.get("Answer", []) if a.get("type") == 1]
                if ips:
                    return [{"hostname": host, "host": ip, "port": port, "family": socket.AF_INET,
                             "proto": 0, "flags": socket.AI_NUMERICHOST} for ip in ips]
                last = OSError("NXDOMAIN " + host)
            except Exception as e:
                last = e
        raise OSError(str(last))

    async def close(self):
        pass


CLOUDFLARED_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"


async def download_cloudflared(dest):
    """仓库里不带 50MB 的 exe，第一次运行时自动从 Cloudflare 官方 GitHub Release 下载。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".download")
    for proxy in (None, system_proxy()):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=900)) as s:
                async with s.get(CLOUDFLARED_URL, proxy=proxy) as r:
                    if r.status != 200:
                        raise OSError("HTTP %d" % r.status)
                    with open(tmp, "wb") as f:
                        async for chunk in r.content.iter_chunked(1 << 16):
                            f.write(chunk)
            if tmp.stat().st_size < 10 << 20:
                raise OSError("下载的文件不完整")
            os.replace(tmp, dest)
            log.info("cloudflared 下载完成：%s", dest)
            return True
        except Exception as e:
            log.warning("下载 cloudflared 失败(%s): %s", "代理" if proxy else "直连", e)
    try:
        tmp.unlink()
    except OSError:
        pass
    return False


class CloudflareQuick:
    URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

    def __init__(self, port, exe, on_change):
        self.port = port
        self.exe = Path(exe)
        self.on_change = on_change
        self.url = None
        self.ready = False
        self.proc = None
        self.state = "启动中"

    async def _read(self, stream):
        async for raw in stream:
            line = raw.decode("utf-8", "replace").rstrip()
            m = self.URL_RE.search(line)
            if m and not self.url:
                self.url = m.group(0)
                log.info("Cloudflare 隧道地址: %s", self.url)
            if "Registered tunnel connection" in line and not self.ready:
                self.ready = True
                self.state = "已连接"
                self.on_change()
            if " ERR " in line:
                log.debug("cloudflared: %s", line)

    async def _health(self):
        await asyncio.sleep(25)
        fails = 0
        conn = aiohttp.TCPConnector(resolver=DohResolver(), ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=15)) as s:
            while self.proc and self.proc.returncode is None:
                ok = False
                if self.url:
                    try:
                        async with s.get(self.url + "/health") as r:
                            ok = r.status == 200 and (await r.text()) == "ok"
                    except Exception as e:
                        log.debug("隧道健康检查失败: %s", e)
                fails = 0 if ok else fails + 1
                if ok and self.state != "已连接":
                    self.state = "已连接"
                    self.on_change()
                if fails >= 4:
                    log.warning("Cloudflare 隧道连续 %d 次不通，重启隧道", fails)
                    self.proc.kill()
                    return
                await asyncio.sleep(60 if ok else 30)

    async def loop(self):
        while not self.exe.exists():
            self.state = "正在下载 cloudflared（约 50MB，只需一次）"
            self.on_change()
            if await download_cloudflared(self.exe):
                break
            self.state = "cloudflared 下载失败，5 分钟后重试"
            self.on_change()
            await asyncio.sleep(300)
        backoff = 5
        while True:
            self.url, self.ready, self.state = None, False, "启动中"
            started = time.time()
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    str(self.exe), "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:%d" % self.port,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
                winapi.bind_child(self.proc.pid)
                readers = [asyncio.create_task(self._read(self.proc.stdout)),
                           asyncio.create_task(self._read(self.proc.stderr))]
                health = asyncio.create_task(self._health())
                await self.proc.wait()
                health.cancel()
                for r in readers:
                    r.cancel()
            except Exception as e:
                log.warning("cloudflared 启动失败: %s", e)
            log.warning("cloudflared 已退出，%d 秒后重启", backoff)
            self.url, self.ready, self.state = None, False, "重连中"
            self.on_change()
            await asyncio.sleep(backoff)
            backoff = 5 if time.time() - started > 300 else min(backoff * 2, 300)

    def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()


# ====================================================================== ntfy 推送
async def ntfy_publish(topic, title, message, click=None, priority=3):
    if not topic:
        return False
    body = {"topic": topic, "title": title, "message": message, "tags": ["computer"], "priority": priority}
    if click:
        body["click"] = click
        body["actions"] = [{"action": "view", "label": "打开远程桌面", "url": click}]
    for proxy in (None, system_proxy()):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.post("https://ntfy.sh/", json=body, proxy=proxy) as r:
                    if r.status == 200:
                        return True
                    log.warning("ntfy 推送失败 HTTP %s", r.status)
        except Exception as e:
            log.warning("ntfy 推送失败(%s): %s", "代理" if proxy else "直连", e)
    return False
