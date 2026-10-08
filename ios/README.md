# iPad 远程桌面 · 原生 iPadOS 客户端

纯 SwiftUI / UIKit / URLSession 的 iPad App，**不是 WKWebView 或网页套壳**。连接现有 Windows/macOS 版 iPad 远程桌面的同一个 Python 服务，无须修改电脑端协议。

## 已实现

- 电脑地址（Tailscale 私有地址或 HTTPS 隧道）+ 密码登录；默认使用系统 cookie 恢复登录，密码不写入 UserDefaults。
- WebSocket JPEG 分块帧解码、ACK、按比例完整显示屏幕。
- 直接点按、双击、双指右键、单指拖拽、双指滚动、双指捏合放大、鼠标/触控板悬停移动。
- **连接后原生画面自动成为 first responder，直接接管 iPadOS 分发给本 App 的实体键盘按下/抬起事件。** USB HID Usage 映射为服务端现成的 KeyboardEvent.code，带修饰键组合发往 Windows/macOS；Windows 默认把 iPad 的 ⌘ 映射为 Ctrl。
- 用 UIKeyCommand(wantsPriorityOverSystemBehavior) 接收部分被系统当成菜单动作的组合键；有原生快捷键面板，可以主动发送 ⌘Tab / AltTab 等系统抢占的组合键。
- 软键盘/中文可通过「中文输入」文本框发 Unicode。

**无法实现的范围：** Apple 在 iPadOS 保留的系统快捷键（例如 ⌘Tab、⌘空格、多任务手势）第三方原生 App 也无法完全拦截，不能保证“所有键无例外自动属于远程电脑”；请使用快捷键面板模拟这些组合键。远程 Windows 的 Ctrl+Alt+Del 安全桌面也不能靠普通 SendInput 注入。

## Windows 用户：免 Mac 安装

1. 到 [iPad 原生客户端 Beta Release](https://github.com/haoawake/ipad-remote-desktop/releases/tag/ipad-v0.1.0) 下载 `iPadRemote-iPadOS-unsigned.ipa`。这是 **未签名 IPA**，不能直接在 iPad「文件」中安装。
2. 在 Windows 上安装 [Sideloadly（官方）](https://sideloadly.io/)；根据官方要求安装 Apple 官网版本的 iTunes / iCloud。
3. 用 USB 将 iPad 连到 Windows，解锁并在设备上选择「信任此电脑」。打开 Sideloadly，载入这个 IPA，选择自己的 iPad，使用自己的 Apple ID 完成签名与安装（不需要把 Apple ID 发给本项目）。
4. 首次安装后按系统提示到 iPad「设置 → 通用 → VPN 与设备管理」信任开发者；新版本系统可能还需在「隐私与安全性」开启开发者模式。
5. 免费 Apple ID 的个人签名通常 **7 天到期**；届时用 Sideloadly 重新签名安装或配置其刷新功能。

**提醒：** Sideloadly 是第三方工具，安装与 Apple ID 签名由你自己在本机完成。本项目不提供签名证书或 App Store / TestFlight 分发。原生 App 是首次 Beta，已经配置 CI 编译，但真实 iPad 的键盘及网络仍需你安装后实测。

## 编译并安装到自己的 iPad

1. 需要一台 **Mac**、最新可用的 Xcode，以及 [XcodeGen](https://github.com/yonaskolb/XcodeGen)（可用 `brew install xcodegen`）。
2. 在本文件夹执行 `xcodegen generate`，双击生成的 `iPadRemote.xcodeproj`。
3. 在 Xcode → iPadRemote Target → **Signing & Capabilities** 选择自己的 Apple Development Team，并将 Bundle Identifier 改为自己的唯一 ID。
4. 通过 USB 或 Wi-Fi 在 Xcode 选择自己的 iPad，点击 Run 即可安装。免费个人 Apple Account 可能有签名有效期限制。
5. 电脑端运行原来的 Windows/Mac 服务，iPad 安装 Tailscale（或走 HTTPS 隧道），打开 App 输入远程地址及密码。

仓库的 `ios-native.yml` 会通过 GitHub Actions 自动生成 Xcode 项目，在 iOS Simulator SDK 下构建验证，并上传**未签名 iPad 设备 IPA**和可自行编译的 Xcode 项目 ZIP；Beta Release 也包含这两份附件。未签名 IPA 需要 Sideloadly 或 Xcode 配合你的 Apple ID 签名，不是 App Store 安装包。

## 网络安全

电脑端为 Tailscale 网络时可输入 `http://100.x.x.x`；明文 HTTP 只建议用于可信的加密 VPN/内网，公网请使用 HTTPS。由于电脑端旧协议提供局域网 HTTP，本项目为兼容保留了 ATS HTTP 例外。不要把未加密的远程桌面登录接口暴露到公网。
