"""macOS 版入口：打包成「iPad 远程桌面.app」的主程序就是它（源码运行：python3 app/mac_main.py）。

同一个可执行文件有两种角色：
- 不带参数（双击打开、开机自启）：状态窗口进程 mac_ui —— 窗口、菜单、隐私屏、权限引导，
  并负责把服务进程拉起来、崩溃了 5 秒后自动重启（相当于 Windows 上启动脚本里的那个循环）；
- --worker：服务进程 —— Web 服务、截屏、键鼠注入、穿透通道，就是 Windows 上 main.py 做的事。

两个进程都是 .app 自己的可执行文件，所以系统的「屏幕录制」「辅助功能」授权都记在
「iPad 远程桌面」名下，不会记到“终端”或者 Python 头上。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def run():
    if "--worker" in sys.argv:
        # 服务进程也是这个 .app 的可执行文件，一碰 AppKit 系统就会把它当成第二个「iPad 远程桌面」，
        # 程序坞里多出一个图标（还一直在跳）。先声明“我不是一个有界面的程序”
        from AppKit import NSApplication
        NSApplication.sharedApplication().setActivationPolicy_(2)  # NSApplicationActivationPolicyProhibited
        import main
        main.worker_main()
    else:
        import mac_ui
        mac_ui.run()


if __name__ == "__main__":
    run()
