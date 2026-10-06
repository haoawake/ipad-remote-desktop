"""CI 用：拿 WebKit（Safari 的内核）模拟 iPad，登录网页端、截几张图，确认电脑是 Mac 时网页换成了 Mac 的一套。

    python3 tests/web_shot.py --port 8765 --password xxxx --out out/web --os mac

需要：pip install playwright && python -m playwright install webkit
不开隐私屏（预先在 localStorage 里关掉），免得后面的测试被它拦住。
"""
import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--password", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--os", default="mac")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    checks, errors = [], []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print("%s  %s  —  %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)

    with sync_playwright() as p:
        dev = dict(p.devices.get("iPad Pro 11 landscape") or {
            "viewport": {"width": 1194, "height": 834}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True})
        dev.pop("default_browser_type", None)
        browser = p.webkit.launch()
        ctx = browser.new_context(**dev)
        ctx.add_init_script("try { localStorage.setItem('rd.privacy', 'false') } catch (e) {}")
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append("pageerror: %s" % e))
        # 打开页面时先问一次“登录了没有”，没登录服务器回 401 是正常的，不算报错
        page.on("console", lambda m: m.type == "error" and " 401 " not in m.text and errors.append("console: %s" % m.text))
        base = "http://127.0.0.1:%d/" % a.port
        page.goto(base)
        page.wait_for_selector("#pw", state="visible", timeout=15000)
        page.screenshot(path=str(a.out / "web-login.png"))
        page.fill("#pw", a.password)
        page.press("#pw", "Enter")
        try:
            page.wait_for_function("document.querySelector('#screen').width > 100", timeout=15000)
            ok = True
        except Exception as e:  # noqa: BLE001
            ok = False
            errors.append("canvas: %s" % e)
        check("网页端登录后收到画面", ok, page.evaluate("[document.querySelector('#screen').width, "
                                                     "document.querySelector('#screen').height]"))
        time.sleep(2.5)
        page.screenshot(path=str(a.out / "web-desktop.png"))
        page.click("[data-act=keys]")
        time.sleep(0.6)
        page.screenshot(path=str(a.out / "web-keys.png"))
        shown = page.evaluate("[...document.querySelectorAll('#keys [data-host]')].filter(e => !e.hidden)"
                              ".map(e => e.dataset.host)")
        check("快捷键面板只显示「%s」的那几行" % a.os, shown and set(shown) == {a.os}, shown)
        labels = page.evaluate("[...document.querySelectorAll('#keys [data-host]:not([hidden]) button')]"
                               ".map(b => b.textContent.trim())")
        want = ["⌘ Command", "强制退出", "调度中心"] if a.os == "mac" else ["Win", "任务管理器"]
        check("快捷键面板里有 %s" % "、".join(want), all(w in labels for w in want), labels)
        page.click("[data-act=keys]")
        time.sleep(0.3)
        page.click("[data-act=settings]")
        time.sleep(0.6)
        page.screenshot(path=str(a.out / "web-settings.png"))
        cmd = page.evaluate("[document.querySelector('#opt-cmd').checked, "
                            "document.querySelector('#opt-cmd').parentElement.textContent.trim()]")
        if a.os == "mac":
            check("Mac 电脑：外接键盘的 ⌘ 默认就是 ⌘（不当 Control）", cmd[0] is False and "Control" in cmd[1], cmd)
        else:
            check("Windows 电脑：外接键盘的 ⌘ 默认当 Ctrl", cmd[0] is True and "Ctrl" in cmd[1], cmd)
        check("网页没有报错", not errors, errors)
        browser.close()

    (a.out / "results.json").write_text(json.dumps({"checks": checks, "errors": errors}, ensure_ascii=False, indent=1),
                                        "utf-8")
    failed = [c["name"] for c in checks if not c["ok"]]
    print("\n通过 %d 项，失败 %d 项" % (len(checks) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
