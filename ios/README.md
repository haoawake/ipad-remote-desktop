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

## 编译并安装到自己的 iPad

1. 需要一台 **Mac**、最新可用的 Xcode，以及 [XcodeGen](https://github.com/yonaskolb/XcodeGen)（可用 `brew install xcodegen`）。
2. 在本文件夹执行 `xcodegen generate`，双击生成的 `iPadRemote.xcodeproj`。
3. 在 Xcode → iPadRemote Target → **Signing & Capabilities** 选择自己的 Apple Development Team，并将 Bundle Identifier 改为自己的唯一 ID。
4. 通过 USB 或 Wi-Fi 在 Xcode 选择自己的 iPad，点击 Run 即可安装。免费个人 Apple Account 可能有签名有效期限制。
5. 电脑端运行原来的 Windows/Mac 服务，iPad 安装 Tailscale（或走 HTTPS 隧道），打开 App 输入远程地址及密码。

仓库的 `ios-native.yml` 会通过 GitHub Actions 自动生成 Xcode 项目，在 iOS Simulator SDK 下构建验证，并上传**未签名的 Xcode 项目源码 ZIP**。它不是可直接在 iPad 上点击安装的签名 IPA；如需免 Xcode 安装，必须另外完成开发者签名及分发。

## 网络安全

电脑端为 Tailscale 网络时可输入 `http://100.x.x.x`；明文 HTTP 只建议用于可信的加密 VPN/内网，公网请使用 HTTPS。由于电脑端旧协议提供局域网 HTTP，本项目为兼容保留了 ATS HTTP 例外。不要把未加密的远程桌面登录接口暴露到公网。
