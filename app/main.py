"""入口：加载配置 → 启动 Web 服务 → 拉起穿透通道 → 在控制台显示访问方式。"""
import asyncio
import ctypes
import json
import logging
import logging.handlers
import os
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import winapi  # noqa: E402

winapi.set_dpi_aware()  # 必须最先执行

from aiohttp import web  # noqa: E402

import server  # noqa: E402
import tunnels  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = ROOT / "config.json"
STATUS_FILE = ROOT / "访问地址.txt"
log = logging.getLogger("rd")


def gen_password():
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # 去掉了 i l o 0 1 这类易混字符
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def load_config():
    cfg = {}
    if CONFIG.exists():
        cfg = json.loads(CONFIG.read_text("utf-8-sig"))
    defaults = {
        "password": gen_password(),
        "port": 8765,
        "tailscale": True,
        "cloudflare": True,
        "ntfy_topic": "rd-" + secrets.token_hex(8),
        "keep_display_on": True,
        "privacy": True,
        "privacy_block_input": True,
        "upload_dir": "%USERPROFILE%\\Downloads\\iPad传来的文件",
    }
    changed = False
    for k, v in defaults.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if changed:
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    return cfg


def setup_logging():
    DATA.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(DATA / "run.log", maxBytes=2 << 20, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(ch)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def disable_quick_edit():
    """控制台“快速编辑”模式下鼠标点一下窗口就会把整个程序卡住，必须关掉。"""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)
        k32.SetConsoleTitleW("iPad 远程桌面 - 运行中（关掉此窗口就断开）")
    except Exception:
        pass


def already_running(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=2) as r:
            return r.read() == b"ok"
    except Exception:
        return False


class Dashboard:
    def __init__(self, cfg, ts, cf):
        self.cfg, self.ts, self.cf = cfg, ts, cf
        self.last_cf_pushed = None
        self.last_push_time = 0
        self.dirty = asyncio.Event()

    def changed(self):
        self.dirty.set()

    def render(self):
        port = self.cfg["port"]
        lines = ["", "=" * 64, "  iPad 远程桌面   " + time.strftime("%Y-%m-%d %H:%M:%S"), "=" * 64,
                 "  登录密码：%s" % self.cfg["password"], ""]
        lines.append("  【主通道 Tailscale】状态：%s" % (self.ts.state if self.ts else "已关闭"))
        for u in (self.ts.urls() if self.ts else []):
            lines.append("      %s" % u)
        if self.ts and self.ts.state == "已连接":
            lines.append("      iPad 上先打开 Tailscale App 并连上，再用 Safari 打开上面的地址")
        lines.append("")
        lines.append("  【备用通道 Cloudflare】状态：%s" % (self.cf.state if self.cf else "已关闭"))
        if self.cf and self.cf.url:
            lines.append("      %s" % self.cf.url)
            lines.append("      (重启后地址会变；新地址会推送到下面的 ntfy 频道)")
        if self.cfg.get("ntfy_topic"):
            lines.append("")
            lines.append("  【地址推送】https://ntfy.sh/%s" % self.cfg["ntfy_topic"])
        lines.append("")
        lines.append("  本机测试：http://127.0.0.1:%d" % port)
        lines.append("=" * 64)
        return "\n".join(lines)

    def write_file(self, text):
        try:
            STATUS_FILE.write_text(text.strip() + "\n\n（此文件由程序自动生成，地址变化时会更新）\n", "utf-8")
        except Exception:
            pass

    async def push(self, refresh=False):
        """地址变了就推送；ntfy.sh 只保留 12 小时，所以每 6 小时再静默推一次，保证频道里总有最新地址。"""
        ok = await tunnels.ntfy_publish(
            self.cfg.get("ntfy_topic"), "远程桌面地址" if refresh else "远程桌面新地址",
            "备用地址：%s\nTailscale：%s" % (self.cf.url, " / ".join(self.ts.urls()) if self.ts else "-"),
            click=self.cf.url, priority=1 if refresh else 3)
        if ok:
            self.last_cf_pushed = self.cf.url
            self.last_push_time = time.time()
            log.info("地址已推送到 ntfy%s", "（定时刷新）" if refresh else "")

    async def loop(self):
        while True:
            try:
                await asyncio.wait_for(self.dirty.wait(), 600)
            except asyncio.TimeoutError:
                pass
            if self.dirty.is_set():
                await asyncio.sleep(1.5)  # 合并短时间内的多次变化
                self.dirty.clear()
                text = self.render()
                print(text, flush=True)
                self.write_file(text)
            if self.cf and self.cf.ready and self.cf.url:
                if self.cf.url != self.last_cf_pushed:
                    await self.push()
                elif time.time() - self.last_push_time > 6 * 3600:
                    await self.push(refresh=True)


async def amain(cfg):
    port = cfg["port"]
    app = server.make_app(cfg)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    log.info("Web 服务已启动 127.0.0.1:%d", port)

    dash = Dashboard(cfg, None, None)
    tasks = []
    if cfg.get("tailscale", True):
        dash.ts = tunnels.Tailscale(port, dash.changed)
        tasks.append(asyncio.create_task(dash.ts.loop()))
    if cfg.get("cloudflare", True):
        dash.cf = tunnels.CloudflareQuick(port, ROOT / "bin" / "cloudflared.exe", dash.changed)
        tasks.append(asyncio.create_task(dash.cf.loop()))
    tasks.append(asyncio.create_task(dash.loop()))
    dash.changed()

    # 每 30 秒刷新一次防休眠（某些电源策略会重置）
    while True:
        winapi.keep_awake(cfg.get("keep_display_on", True))
        await asyncio.sleep(30)


PID_FILE = DATA / "pid.txt"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "iPadRemoteDesktop"


def stop_running():
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        print("没有找到正在运行的服务。")
        return False
    out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"], capture_output=True, text=True).stdout
    if "python" not in out.lower():
        print("服务没有在运行。")
        return False
    (DATA / "stop.flag").write_text("stop")  # 告诉启动脚本这是主动停止，不要自动重启
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    print("已停止远程桌面服务 (PID %d)。" % pid)
    return True


def set_autostart(on):
    import winreg
    bat = ROOT / "启动远程桌面.bat"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            cmd = '"%s" /c start "iPad Remote Desktop" /min "%s"' % (
                os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"), bat)
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, cmd)
            print("已开启开机自启：登录 Windows 后会自动在后台（最小化窗口）启动远程桌面。")
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass
            print("已关闭开机自启。")


def new_password():
    cfg = load_config()
    cfg["password"] = gen_password()
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    try:
        (DATA / "sessions.json").unlink()
    except OSError:
        pass
    was_running = stop_running()
    print("\n新密码：%s\n所有已登录的设备都需要重新登录。" % cfg["password"])
    if was_running:
        print("服务已停止，请重新双击“启动远程桌面.bat”。")


def main():
    os.chdir(ROOT)
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--stop":
        stop_running()
        return
    if arg == "--autostart":
        set_autostart(len(sys.argv) > 2 and sys.argv[2] == "on")
        return
    if arg == "--new-password":
        new_password()
        return
    disable_quick_edit()
    setup_logging()
    cfg = load_config()
    if already_running(cfg["port"]):
        print("已经有一个远程桌面服务在运行了（端口 %d），无需重复启动。" % cfg["port"])
        time.sleep(5)
        return
    PID_FILE.write_text(str(os.getpid()))
    try:
        asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        log.error("启动失败：%s", e)
        sys.exit(2)


if __name__ == "__main__":
    main()
