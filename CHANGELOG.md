# 更新记录

## v1.0.1

新增 **免安装版**：下载 `iPad-Remote-Desktop-win-x64.zip`，解压后双击 `启动远程桌面.bat` 就能用，不用再自己装 Python 和依赖。

### 安装

1. 下载本页的 **`iPad-Remote-Desktop-win-x64.zip`**，解压到一个固定的位置（比如 `D:\iPad远程桌面`）。
2. 电脑和 iPad 都安装 [Tailscale](https://tailscale.com/download)，登录同一个账号。
3. 双击 `启动远程桌面.bat`。如果 Windows 提示“无法验证发布者”，点「运行」。窗口里会显示登录密码和访问地址。
4. （可选）双击 `开机自启-开启.bat`，以后登录 Windows 后自动在后台启动。

### 改动

- 压缩包里自带 Python 3.13（python.org 官方嵌入式版本）和 aiohttp、mss、numpy、Pillow，约 31 MB。
- 所有脚本优先使用包里自带的 `runtime\python.exe`，没有时再用系统里的 Python，从源码运行的用法不变。
- 新增 `scripts/build_portable.py` 和发版流程：推送 `v*` 标签后自动在 Windows 上打包并发布。

## v1.0.0

首个正式版本（需要自己安装 Python 3.10+ 和依赖）。
