"""两个平台共用的小工具：文件放在哪、配置文件、日志。

只用标准库：macOS 的状态窗口进程也要导入它，不想因此把 aiohttp / numpy 全加载一遍。

文件位置：
- Windows：一切都在程序目录里（免安装版解压到哪就在哪），和 v1.0 完全一样。
- macOS：.app 里面是只读的（改了签名就坏；从“下载”里直接打开时系统还会把它挪到只读的临时位置），
  所以配置、会话、日志、cloudflared 都放在 ~/Library/Application Support/iPad 远程桌面/。
"""
import json
import logging
import logging.handlers
import secrets
import sys
from pathlib import Path

MAC = sys.platform == "darwin"
APP_NAME = "iPad 远程桌面"

CODE = Path(__file__).resolve().parent                # app/
RES = Path(getattr(sys, "_MEIPASS", CODE.parent))     # 只读资源：打包后在 .app 里，源码运行时就是仓库根目录
WEB = RES / "web"

ROOT = Path.home() / "Library" / "Application Support" / APP_NAME if MAC else CODE.parent
DATA = ROOT / "data"
BIN = ROOT / "bin"
CONFIG = ROOT / "config.json"
STATUS_FILE = ROOT / "访问地址.txt"
DEFAULT_UPLOAD_DIR = "~/Downloads/iPad传来的文件" if MAC else "%USERPROFILE%\\Downloads\\iPad传来的文件"


def gen_password():
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # 去掉了 i l o 0 1 这类易混字符
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


def save_config(cfg):
    ROOT.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


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
        "upload_dir": DEFAULT_UPLOAD_DIR,
    }
    changed = False
    for k, v in defaults.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if changed:
        save_config(cfg)
    return cfg


def setup_logging(filename="run.log", stream=None):
    DATA.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(DATA / filename, maxBytes=2 << 20, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    stream = stream or sys.stdout
    if stream is not None:  # 打包成 .app 后没有控制台，stdout 可能是 None
        ch = logging.StreamHandler(stream)
        ch.setFormatter(fmt)
        ch.setLevel(logging.INFO)
        root.addHandler(ch)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
