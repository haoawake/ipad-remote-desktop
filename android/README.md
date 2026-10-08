# Android 原生远程桌面 v0.1.0 Beta

真正的 Android **Java / View** 原生客户端（没有 WebView），沿用同仓库 Windows/macOS 电脑端的服务和协议。Android 8.0（API 26）起支持。

## 📲 免费下载安装

1. 打开 [Android APK Release](https://github.com/haoawake/ipad-remote-desktop/releases/tag/android-v0.1.0)，下载 **iPad-Remote-Desktop-Android-v0.1.0.apk**。
2. 在 Android 手机或平板的下载文件中点开 APK。首次从浏览器 / 文件管理器安装时，按系统指引允许“安装未知应用”或“允许此来源”。**不要安装来源不明的改包**。
3. 在手机/平板安装 Tailscale 并登录被控电脑同一个 Tailnet；或者使用电脑生成的 HTTPS Cloudflare Tunnel 地址。
4. 确保 Windows/Mac 电脑端已经启动远程桌面服务。Android App 输入电脑端给出的地址（例如 `http://100.x.x.x` 或 `https://xxx.trycloudflare.com`）和密码，点击连接。
5. 连上后，屏幕可以触摸操作，蓝牙 / USB 键盘直接按即可打字和使用大部分快捷键。**Windows 端默认把手机键盘的 Meta/Win/Command 修饰键映射为 Ctrl**，Mac 端保留 Command。

## 功能与手势

| 操作 | 结果 |
| --- | --- |
| 单指轻点 | 左键单击；连续双击＝左键双击 |
| 单指按住移动 | 远程左键拖动 |
| 长按 | 右键单击 |
| 双指点击 | 远程右键 |
| 双指上下 / 左右滑 | 远程鼠标滚轮 |
| 双指捏合 | 放大远程画面 |
| 外接鼠标 / 触控板 | 移动远程光标、点击、滚动 |
| 实体键盘 Ctrl / Alt / Shift / Meta + 字母 | 在 App 活动且系统分发按键时，原生按键直接发到远程桌面 |
| “快捷键”面板 | 直接模拟 Alt+Tab、Win+D 等无法从当前 Android 键盘发出的组合 |
| “文字”面板 | 使用手机中文输入法、粘贴长文本或特殊符号后发送到电脑 |
| “重置”按钮 | 重置屏幕放大倍率 |

**Android 系统键边界：** Home、最近任务、系统导航以及部分制造商级快捷键由 Android 保留，普通 App 无法全面拦截。系统控制的组合请使用本 App 的远程快捷键面板。Windows 的 Ctrl+Alt+Delete 安全桌面不能通过原服务端常规 SendInput 注入。

## APK 签名说明

本仓库的 GitHub Actions 自动通过 Android SDK + Gradle 构建**带调试签名的 APK**，可在大多数 Android 设备直接侧载，无需付费开发者账号。

Beta 调试证书由 CI 构建机生成，不能保证每次构建都保持同一个签名身份；**更新时可能需要卸载旧包再安装**（会清除 App 中保存的电脑地址）。正式长期分发应使用你自己的固定签名证书和安全托管方式。

为了兼容 Tailscale 的局域网 HTTP，Android Manifest 开启了明文 HTTP。只在 **Tailscale 加密隧道 / 可信内网**中使用 HTTP，公网必须用 HTTPS。电脑端和移动端的通信凭登录密码保护，只有在可信网络里才适合保留 HTTP。

## 编译源代码

在本目录运行：

```bash
gradle :app:assembleDebug
```

需要 JDK 17、Android SDK Platform 35 和 Gradle 8.9。CI 工作流见 `.github/workflows/android.yml`。

APK 是最早期 Beta，已经设置自动构建 / 基础编译验证，但尚未在你的手机型号、实体键盘组合与网络环境上完成实机测试。可将实测问题提交到 Issues。
