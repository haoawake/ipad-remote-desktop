"""打包免安装版：自带 Python 运行环境和依赖，解压后双击「启动远程桌面.bat」就能用。

    用法：python scripts/build_portable.py 1.0.1

产物：dist/iPad-Remote-Desktop-win-x64.zip 和 SHA256SUMS.txt。
发版时由 .github/workflows/release.yml 在 Windows 上调用；本机任何 3.10 以上的 Python 都能跑。

做法：下载 python.org 官方的「嵌入式」Python（一个 zip，解开就能用，不写注册表），
用本机的 pip 按目标版本下载依赖的 Windows 轮子装进它的 site-packages，再把程序文件放到旁边。
启动脚本发现 runtime\\python.exe 就用它，没有就用系统里的 python，所以源码和免安装版共用同一套脚本。
"""
import hashlib
import io
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

PY_VERSION = "3.13.16"
PY_TAG = "".join(PY_VERSION.split(".")[:2])  # "313"
NAME = "iPad-Remote-Desktop"

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
STAGE = BUILD / NAME
RUNTIME = STAGE / "runtime"

# 打进包里的程序文件（运行时生成的 config.json、data\、bin\ 不带）
FILES = [
    "app", "web", "requirements.txt", "LICENSE", "README.md", "README.zh-CN.md",
    "启动远程桌面.bat", "停止远程桌面.bat", "开机自启-开启.bat", "开机自启-关闭.bat",
    "重置密码.bat", "安装依赖.bat",
]

CR, LF = b"\r", b"\n"


def download(url):
    print("下载", url)
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else "dev"
    shutil.rmtree(BUILD, ignore_errors=True)
    shutil.rmtree(DIST, ignore_errors=True)
    RUNTIME.mkdir(parents=True)
    DIST.mkdir()

    # 1. 嵌入式 Python
    data = download(f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip")
    zipfile.ZipFile(io.BytesIO(data)).extractall(RUNTIME)
    # ._pth 决定 sys.path：加上 site-packages，并打开 import site（第三方包要靠它加载）
    pth = RUNTIME / f"python{PY_TAG}._pth"
    lines = [ln for ln in pth.read_text("utf-8").splitlines() if ln.strip() and ln.strip() != "#import site"]
    lines += ["Lib\\site-packages", "import site"]
    pth.write_text("\n".join(lines) + "\n", "utf-8")

    # 2. 依赖：只要 Windows 64 位、对应 Python 版本的现成轮子，不在本机编译
    subprocess.run([
        sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
        "--target", str(RUNTIME / "Lib" / "site-packages"),
        "--platform", "win_amd64", "--python-version", PY_VERSION, "--implementation", "cp",
        "--only-binary=:all:", "-r", str(ROOT / "requirements.txt"),
    ], check=True)

    # 3. 程序文件
    for name in FILES:
        src = ROOT / name
        if src.is_dir():
            # macOS 专用的模块（mac_*.py、macapi.py、macipc.py、privacy_mac.py）不放进 Windows 包
            shutil.copytree(src, STAGE / name, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "mac*.py", "privacy_mac.py"))
        elif name.endswith(".bat"):
            # cmd.exe 的 goto 在只有 LF 换行的脚本里会找错标签，统一成 CRLF
            text = src.read_bytes().replace(CR + LF, LF).replace(LF, CR + LF)
            (STAGE / name).write_bytes(text)
        else:
            shutil.copy2(src, STAGE / name)
    (STAGE / "VERSION.txt").write_text(version + "\n", "utf-8")

    # 4. 打包。zipfile 遇到中文文件名会打上 UTF-8 标记，资源管理器解压不会乱码
    out = DIST / f"{NAME}-win-x64.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(STAGE.rglob("*")):
            if p.is_file():
                z.write(p, Path(NAME) / p.relative_to(STAGE))
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    # 换行固定用 LF：Windows 上默认写成 CRLF，sha256sum -c 就读不了了
    (DIST / "SHA256SUMS.txt").write_text(f"{digest}  {out.name}\n", "utf-8", newline="\n")
    print(f"\n完成：{out}（{out.stat().st_size / 1048576:.1f} MB）")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
