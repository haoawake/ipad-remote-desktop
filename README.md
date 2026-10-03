# iPad Remote Desktop

English | [中文](README.zh-CN.md)

A self-hosted, browser-based remote desktop for controlling a Windows PC from an iPad, including over cellular data. The iPad side is just a web page in Safari (add it to the Home Screen for a full-screen, app-like experience); the PC side is a small Python service.

- **Full-screen view and control**: mouse (trackpad or direct-touch modes), keyboard including Chinese IME, shortcut panel, clipboard, open URLs on the PC, upload files.
- **NAT traversal without port forwarding**:
  - **Primary: Tailscale.** Peer-to-peer, WireGuard-encrypted, stable address. Exposed via `tailscale serve` so no firewall changes or admin rights are needed.
  - **Backup: Cloudflare Quick Tunnel.** No account and no app on the iPad; an `https://*.trycloudflare.com` URL works from any network. The URL changes when the tunnel restarts, so each new URL is pushed to a private [ntfy](https://ntfy.sh) topic.
- **Privacy screen**: while you control the PC remotely, its physical display shows a lock-screen image and local keyboard/mouse input is paused, but your iPad still sees the real desktop.

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

## Requirements

- Windows 10 2004+ / Windows 11 (the privacy screen needs `WDA_EXCLUDEFROMCAPTURE`)
- Python 3.10+
- [Tailscale](https://tailscale.com/download) on the PC and the iPad, signed in to the same account (recommended)

## Quick start

```bat
pip install -r requirements.txt
启动远程桌面.bat
```

On first run the service generates a random password and `config.json`, downloads `cloudflared.exe` (~50 MB, from Cloudflare's official GitHub release), and prints the password plus the Tailscale and backup addresses (also written to `访问地址.txt`).

On the iPad: connect Tailscale, open the Tailscale address shown (e.g. `http://100.x.y.z`) in Safari, log in, then **Share → Add to Home Screen**.

Helper scripts: `停止远程桌面.bat` (stop), `开机自启-开启.bat` / `开机自启-关闭.bat` (enable/disable start at login), `重置密码.bat` (new password).

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

Hardware keyboards, mice and trackpads on the iPad work directly (⌘ is mapped to Ctrl by default).

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
| `upload_dir` | `%USERPROFILE%\Downloads\iPad传来的文件` | Where uploaded files are saved |

## Security

- Password login with per-IP and global brute-force lockout; HttpOnly session cookie (30 days if "remember" is checked); WebSocket origin check.
- The service listens on `127.0.0.1` only; outside access is only through Tailscale (WireGuard) or the Cloudflare tunnel (HTTPS).
- "Open URL" accepts only http/https.

## Limitations

- Windows' secure desktop (lock screen, UAC prompts) cannot be captured or controlled, and elevated windows don't accept input from a non-elevated process (UIPI).
- After a reboot, someone has to sign in to Windows before the service starts.
- With the privacy screen on, the Start menu, Alt+Tab, and toast notifications still render above it (they live in higher z-order bands), and audio still plays locally.
- Laptops must keep the lid open (no display, nothing to capture).

## License

[MIT](LICENSE)
