# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：打出「iPad 远程桌面.app」。由 scripts/build_mac.py 调用，不要直接跑。

.app 里只有一个可执行文件 Contents/MacOS/iPadRemoteDesktop（入口 app/mac_main.py）：
不带参数是状态窗口进程，带 --worker 是服务进程。两个进程都是这个 .app 自己，
所以「屏幕录制」「辅助功能」的授权都记在「iPad 远程桌面」名下。

可执行文件名用 ASCII：很多命令行工具（ps、launchctl、日志）对中文和空格不友好；
用户看到的名字来自 Info.plist 的 CFBundleName / CFBundleDisplayName。
"""
import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent          # noqa: F821  （PyInstaller 注入）
VERSION = os.environ.get("RD_VERSION", "0.0.0")
ICON = str(ROOT / "build" / "icon.icns")

a = Analysis(                                   # noqa: F821
    [str(ROOT / "app" / "mac_main.py")],
    pathex=[str(ROOT / "app")],
    datas=[(str(ROOT / "web"), "web")],
    hiddenimports=[
        # 这些都是在函数里按需 import 的，写明白免得漏掉
        "main", "mac_ui", "mac_overlay", "macapi", "macipc", "privacy_mac", "server", "capture", "tunnels",
        "common", "objc", "Foundation", "AppKit", "Quartz", "PyObjCTools.AppHelper",
        "PIL.JpegImagePlugin", "PIL.PngImagePlugin", "PIL.TiffImagePlugin",
    ],
    # Windows 专用的模块和用不到的大件都不要
    excludes=["winapi", "privacy", "mss", "winreg", "tkinter", "pip", "setuptools", "pkg_resources",
              "_distutils_hack", "IPython", "matplotlib", "scipy", "pandas", "pytest", "PyQt5", "PyQt6",
              "PySide2", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)                               # noqa: F821
exe = EXE(                                      # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="iPadRemoteDesktop",
    console=False,
    argv_emulation=False,
    target_arch="arm64",
    icon=ICON,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="iPadRemoteDesktop")  # noqa: F821
app = BUNDLE(                                   # noqa: F821
    coll,
    name="iPad 远程桌面.app",
    icon=ICON,
    version=VERSION,
    bundle_identifier="com.haoawake.ipad-remote-desktop",
    info_plist={
        "CFBundleName": "iPad 远程桌面",
        "CFBundleDisplayName": "iPad 远程桌面",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleLocalizations": ["zh-Hans", "zh_CN"],
        "LSApplicationCategoryType": "public.app-category.utilities",
        "LSMinimumSystemVersion": "11.0",   # build_mac.py 会按实际打进来的二进制改成真正的最低版本
        "NSHighResolutionCapable": True,
        # 先登记成“不在程序坞里显示”的程序：服务进程（同一个可执行文件加 --worker）一启动系统就会按这里登记它，
        # 写成普通程序的话，程序坞里会闪出第二个「iPad 远程桌面」并留在“最近使用”里。
        # 状态窗口进程启动时自己切成普通程序（mac_ui.run：setActivationPolicy Regular），程序坞图标、菜单栏照常有。
        "LSUIElement": True,
        "LSBackgroundOnly": False,
        "NSSupportsAutomaticTermination": False,
        "NSSupportsSuddenTermination": False,
        "NSDownloadsFolderUsageDescription": "用来保存从 iPad 传过来的照片和文件（“下载”里的「iPad传来的文件」文件夹）。",
        "NSHumanReadableCopyright": "MIT License · github.com/haoawake/ipad-remote-desktop",
    },
)
