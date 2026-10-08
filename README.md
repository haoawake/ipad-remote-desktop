# iPad Remote Desktop

English | [中文](README.zh-CN.md)

A self-hosted, browser-based remote desktop for controlling a Windows PC **or a Mac** from an iPad, including over cellular data. The iPad client can be either a **native iPadOS app (Beta)** or a Safari page; the host computer runs a small Python service (bundled in the Windows/Mac releases).

- **Full-screen view and control**: mouse (trackpad or direct-touch modes), keyboard including Chinese IME, shortcut panel, clipboard, open URLs on the PC, upload files.
- **NAT traversal without port forwarding**:
  - **Primary: Tailscale.** Peer-to-peer, WireGuard-encrypted, stable address. Exposed via `tailscale serve` so no firewall changes or admin rights are needed.
  - **Backup: Cloudflare Quick Tunnel.** No account and no app on the iPad; an `https://*.trycloudflare.com` URL works from any network. The URL changes when the tunnel restarts, so each new URL is pushed to a private [ntfy](https://ntfy.sh) topic.
- **Privacy screen**: while you control the PC remotely, its physical display shows a lock-screen image and local keyboard/mouse input is paused, but your iPad still sees the real desktop.

| Download ([Releases](https://github.com/haoawake/ipad-remote-desktop/releases/latest)) | For |
|---|---|
| `iPad-Remote-Desktop-win-x64.zip` | Windows 10 2004+ / Windows 11 (64-bit), portable |
| `iPad-Remote-Desktop-mac-arm64.zip` | Apple silicon Macs (M1 and later), macOS 11 or later — contains `iPad 远程桌面.app` |

**Native iPad client:** [iPadOS Beta release](https://github.com/haoawake/ipad-remote-desktop/releases/tag/ipad-v0.1.0) provides an **unsigned IPA** and an Xcode project. On Windows, Sideloadly can sign/install the IPA using your Apple ID; on Mac, use Xcode. [Native app installation guide](ios/README.md). iPadOS-reserved shortcuts such as Command+Tab cannot be universally intercepted by any third-party app.

## How it works

```
iPad Safari ──(Tailscale  or  Cloudflare Tunnel)──▶ 127.0.0.1:8765  aiohttp service
   canvas  ◀── WebSocket: JPEG dirty tiles ─────────  mss screen capture + numpy tile diff
   touch   ──▶ WebSocket: JSON input events ────────▶  SendInput (ctypes)
```

- **Streaming**: the screen is captured with `mss`, compared against the previous frame in 64×64 tiles with numpy, and only changed regions are JPEG-encoded and sent. Tiles that stay still for ~0.4s are re-sent once at high quality so text sharpens. The client acknowledges each frame, so the frame rate adapts to the link instead of queueing latency. An idle screen costs almost no bandwidth.
- **Cursor** is rendered client-side from the real Windows cursor shape, with local prediction in trackpad mode for low perceived latency.
- **Input** is injected with `SendInput` (absolute moves, wheel and horizontal wheel, virtual keys, Unicode text).
- **Privacy screen**: a topmost, click-through, non-activating window marked `WDA_EXCLUDEFROMCAPTURE`, so it appears on the monitor but not in any screen capture. Low-level keyboard/mouse hooks drop physical input while letting injected (remote) input through. Unlocking Windows locally with the account password/PIN dismisses it.
- **On macOS** the same service runs inside a native AppKit app: capture with `CGWindowListCreateImage` (per-display, in pixels, so Retina is sharp), input via Quartz `CGEvent`s (click counts for double-click, drags, pixel-precise "continuous" scrolling, modifier flags, Unicode typing for Chinese/emoji), and the privacy screen is a `CGShieldingWindowLevel` window on every display. Frames are captured from the windows *below* it (`kCGWindowListOptionOnScreenBelowWindow`), so the iPad sees the real desktop; a `CGEventTap` drops local input and passes events tagged by this app.

## Requirements

- Windows 10 2004+ / Windows 11 (the privacy screen needs `WDA_EXCLUDEFROMCAPTURE`), or an Apple silicon Mac with macOS 11+
- Nothing else for the portable package; Python 3.10+ only when running from source
- [Tailscale](https://tailscale.com/download) on the PC and the iPad, signed in to the same account (recommended)

## Quick start (Windows)

Download **`iPad-Remote-Desktop-win-x64.zip`** from [Releases](https://github.com/haoawake/ipad-remote-desktop/releases/latest), unzip it somewhere permanent, and double-click `启动远程桌面.bat`. The portable package bundles an embedded Python with all dependencies, so nothing needs to be installed.

To run from source instead:

```bat
pip install -r requirements.txt
启动远程桌面.bat
```

The scripts use the bundled `runtime\python.exe` when it exists and fall back to `python` on `PATH`. `python scripts/build_portable.py <version>` builds the portable package.

On first run the service generates a random password and `config.json`, downloads `cloudflared.exe` (~50 MB, from Cloudflare's official GitHub release), and prints the password plus the Tailscale and backup addresses (also written to `访问地址.txt`).

On the iPad: connect Tailscale, open the Tailscale address shown (e.g. `http://100.x.y.z`) in Safari, log in, then **Share → Add to Home Screen**.

Helper scripts: `停止远程桌面.bat` (stop), `开机自启-开启.bat` / `开机自启-关闭.bat` (enable/disable start at login), `重置密码.bat` (new password).

## Quick start (Mac)

1. Download **`iPad-Remote-Desktop-mac-arm64.zip`**, double-click to unzip, and drag **iPad 远程桌面** into **Applications**. The runtime is bundled; no Python needed.
2. **First open**: the app is ad-hoc signed (no paid Apple developer certificate), so macOS says it can't verify the developer. Click *Done*, open **System Settings → Privacy & Security**, scroll down and click **Open Anyway** (macOS 15+). Alternatively run `xattr -cr "/Applications/iPad 远程桌面.app"`.
3. Grant the two permissions the window asks for — **Screen Recording** (without it the iPad only sees the wallpaper) and **Accessibility** (without it clicks/typing do nothing and the privacy screen can't block local input). The buttons open the right Settings panes. When macOS asks whether the app may "bypass the system private window picker", click **Allow** (macOS 15 re-asks about once a month).
4. Install [Tailscale](https://tailscale.com/download/mac) on the Mac and the iPad with the same account.

The status window shows the password, the Tailscale address, the backup address and ntfy topic, with Copy buttons, plus *Reset password…*, *Start at login* (a LaunchAgent) and *Stop & Quit*. Closing the window keeps the service running; ⌘Q stops it. Data lives in `~/Library/Application Support/iPad 远程桌面/`, uploads go to `~/Downloads/iPad传来的文件`. `cloudflared` (darwin-arm64, pinned version, SHA-256 checked) is downloaded on first run.

If the Mac's current input source is an input method (e.g. Pinyin), remote typing temporarily switches it to the ASCII keyboard layout so the Mac-side IME doesn't swallow the injected text (the iPad's own keyboard/IME is used for Chinese); the original input source is restored 2 minutes after the remote session ends, or when the app quits.

When the computer is a Mac, the web client switches the shortcut panel to Mac keys (⌘ ⇧ ⌥ ⌃, ⌘Tab, Mission Control, Spotlight, Force Quit ⌥⌘Esc, Activity Monitor, Finder…) and an external keyboard's ⌘ stays ⌘ by default.

From source: `python3 -m pip install -r requirements-mac.txt && python3 app/mac_main.py` (permissions then belong to Terminal). Build the app: `bash scripts/mac_deps.sh && python3 scripts/build_mac.py 1.1.0`.

## Gestures

| Gesture | Trackpad mode (default) | Direct mode |
|---|---|---|
| One-finger drag | Move pointer | Left-button drag |
| Tap | Left click (tap twice = double click) | Click where tapped |
| Long press | Start drag | Right click |
| Tap, then press and drag | Drag | — |
| Two-finger tap | Right click | Right click |
| Two-finger swipe | Scroll (with inertia) | Scroll (with inertia) |
| Pinch | Zoom view (follows the pointer) | Zoom and pan view |
| Two-finger hold (1 s) | Show/hide toolbar | Show/hide toolbar |

Hardware keyboards, mice and trackpads on the iPad work directly (⌘ is mapped to Ctrl by default on a Windows host, and stays ⌘ on a Mac host).

## Configuration (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `password` | random | Login password |
| `port` | `8765` | Local port (binds to 127.0.0.1 only) |
| `tailscale` | `true` | Configure the Tailscale path automatically |
| `cloudflare` | `true` | Enable the Cloudflare backup tunnel; set `false` to keep the PC off the public internet |
| `ntfy_topic` | random | ntfy topic for backup-URL pushes; empty disables |
| `keep_display_on` | `true` | Prevent sleep and display-off while running |
| `privacy` | `true` | Enable the privacy screen feature |
| `privacy_block_input` | `true` | Block local keyboard/mouse while the privacy screen is on |
| `upload_dir` | `%USERPROFILE%\Downloads\iPad传来的文件` (Mac: `~/Downloads/iPad传来的文件`) | Where uploaded files are saved |

## Security

- Password login with per-IP and global brute-force lockout; HttpOnly session cookie (30 days if "remember" is checked); WebSocket origin check.
- The service listens on `127.0.0.1` only; outside access is only through Tailscale (WireGuard) or the Cloudflare tunnel (HTTPS).
- "Open URL" accepts only http/https.

## Limitations

- Windows' secure desktop (lock screen, UAC prompts) cannot be captured or controlled, and elevated windows don't accept input from a non-elevated process (UIPI).
- After a reboot, someone has to sign in to Windows before the service starts.
- With the privacy screen on, the Start menu, Alt+Tab, and toast notifications still render above it (they live in higher z-order bands), and audio still plays locally.
- Laptops must keep the lid open (no display, nothing to capture).

### On macOS

- The pointer itself can't be stopped from moving locally (macOS moves it before delivering events); the app warps it straight back, so local mouse movement only makes it twitch and nothing can be clicked.
- While a password field has focus, macOS Secure Input bypasses every event tap, so local typing reaches that field.
- Volume/brightness HUDs may appear above the privacy screen; audio still plays locally. The system's purple screen-recording indicator in the top-right corner stays visible (WindowServer draws it above every window).
- System authentication dialogs (admin password prompts) don't accept synthetic keystrokes.
- To dismiss the privacy screen at the Mac: wait until the remote session has been disconnected for 2 minutes, press **Enter** — the Mac locks — and unlock it with the login password / Touch ID; the overlay is gone after unlock.
- Don't lock the Mac or close a MacBook's lid while away: nobody can unlock it remotely. The app keeps the Mac and display awake while it runs.
- The app is ad-hoc signed, so after updating to a new version macOS may treat it as a new app: remove the old entry under Screen Recording / Accessibility with "−" and grant again.

## License

[MIT](LICENSE)
