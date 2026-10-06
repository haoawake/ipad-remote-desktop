"""打包 macOS 版：dist/iPad-Remote-Desktop-mac-arm64.zip，里面只有「iPad 远程桌面.app」。

    用法（必须在 Mac 上，Apple 芯片）：python3 scripts/build_mac.py 1.1.0

发版时由 .github/workflows/release.yml 在 macOS 上调用。做的事：
1. 画图标（scripts/make_mac_icon.py）→ iconutil 合成 .icns；
2. PyInstaller 按 scripts/mac.spec 打出 .app（自带 Python 和 aiohttp、numpy、Pillow、PyObjC，用户不用装任何东西）；
3. 加 zh-Hans.lproj，让系统对话框、菜单里自动出来的项目显示中文；
4. 扫一遍包里所有二进制，按其中要求最高的那个写 LSMinimumSystemVersion（不靠猜）；
5. ad-hoc 签名（Apple 芯片上不签名的程序根本跑不起来）并校验；
6. 用 ditto 打 zip（保留 .app 里的符号链接和扩展属性，普通 zip 打出来的 .app 打不开）。

依赖：pip install -r requirements-mac.txt pyinstaller
"""
import hashlib
import json
import plistlib
import shutil
import struct
import subprocess
import sys
from pathlib import Path

NAME = "iPad-Remote-Desktop"
APP_NAME = "iPad 远程桌面"
ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
CPU_ARM64 = 0x0100000C


def run(*cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, cwd=ROOT, **kw)


# ---------------------------------------------------------------- Mach-O：读最低系统版本
def _thin_minos(f, base):
    f.seek(base)
    hdr = f.read(32)
    if len(hdr) < 32 or struct.unpack_from("<I", hdr)[0] != 0xFEEDFACF:
        return None
    ncmds, sizeofcmds = struct.unpack_from("<II", hdr, 16)
    cmds = f.read(sizeofcmds)
    off = 0
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", cmds, off)
        if cmd == 0x32:    # LC_BUILD_VERSION
            platform, minos = struct.unpack_from("<II", cmds, off + 8)
            if platform == 1:  # macOS
                return minos
        elif cmd == 0x24:  # LC_VERSION_MIN_MACOSX（老工具链编出来的）
            return struct.unpack_from("<I", cmds, off + 8)[0]
        off += size
    return None


def macho_minos(path):
    """返回 Mach-O 文件（arm64 部分）要求的最低 macOS 版本（编码成 0xXXXXYYZZ），不是 Mach-O 返回 None。"""
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic == b"\xcf\xfa\xed\xfe":
                return _thin_minos(f, 0)
            if magic == b"\xca\xfe\xba\xbe":  # 通用二进制：挑出 arm64 那一份
                n = struct.unpack(">I", f.read(4))[0]
                arches = [struct.unpack(">5I", f.read(20)) for _ in range(min(n, 16))]
                for cputype, _, offset, _, _ in arches:
                    if cputype == CPU_ARM64:
                        return _thin_minos(f, offset)
    except OSError:
        pass
    return None


def fmt(v):
    major, minor, patch = v >> 16, (v >> 8) & 0xFF, v & 0xFF
    return "%d.%d" % (major, minor) + (".%d" % patch if patch else "")


# ---------------------------------------------------------------- 主流程
def main():
    version = sys.argv[1] if len(sys.argv) > 1 else "0.0.0"
    shutil.rmtree(BUILD, ignore_errors=True)
    shutil.rmtree(DIST, ignore_errors=True)
    BUILD.mkdir()
    DIST.mkdir()

    # 1. 图标
    run(sys.executable, "scripts/make_mac_icon.py", BUILD / "icon.iconset")
    run("iconutil", "-c", "icns", BUILD / "icon.iconset", "-o", BUILD / "icon.icns")

    # 2. PyInstaller
    import os
    env = dict(os.environ, RD_VERSION=version)
    run(sys.executable, "-m", "PyInstaller", "scripts/mac.spec", "--noconfirm", "--clean",
        "--distpath", DIST, "--workpath", BUILD / "pyinstaller", env=env)
    app = DIST / (APP_NAME + ".app")
    if not app.is_dir():
        sys.exit("没有生成 %s" % app)
    # PyInstaller 顺带留下的 onedir 目录不要（.app 里已经有一份）
    shutil.rmtree(DIST / "iPadRemoteDesktop", ignore_errors=True)

    # 3. 中文
    lproj = app / "Contents" / "Resources" / "zh-Hans.lproj"
    lproj.mkdir(exist_ok=True)
    (lproj / "InfoPlist.strings").write_text(
        'CFBundleName = "%s";\nCFBundleDisplayName = "%s";\n' % (APP_NAME, APP_NAME), "utf-8")

    # 4. 最低系统版本：包里所有二进制里要求最高的那个
    mins = {}
    for p in app.rglob("*"):
        if p.is_file() and not p.is_symlink():
            v = macho_minos(p)
            if v:
                mins[str(p.relative_to(app))] = v
    top = max(mins.values())
    worst = sorted(mins.items(), key=lambda kv: -kv[1])[:8]
    print("最低系统版本：macOS %s（由 %s 决定）" % (fmt(top), worst[0][0]))
    plist_path = app / "Contents" / "Info.plist"
    with open(plist_path, "rb") as f:
        info = plistlib.load(f)
    info["LSMinimumSystemVersion"] = fmt(top)
    with open(plist_path, "wb") as f:
        plistlib.dump(info, f)

    # 5. 签名（ad-hoc）。改过 Info.plist、加过文件，必须整个重签
    run("codesign", "--force", "--deep", "--sign", "-", app)
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", app)

    # 6. 打包
    out = DIST / ("%s-mac-arm64.zip" % NAME)
    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, out)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    app_size = sum(p.stat().st_size for p in app.rglob("*") if p.is_file() and not p.is_symlink())
    summary = {"zip": out.name, "zip_bytes": out.stat().st_size, "app_bytes": app_size,
               "min_macos": fmt(top), "min_macos_by": [[k, fmt(v)] for k, v in worst], "sha256": digest,
               "version": version, "binaries": len(mins)}
    (BUILD / "mac-build.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\n完成：%s（%.1f MB，解压后 %.1f MB）" % (out, out.stat().st_size / 1048576, app_size / 1048576))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
