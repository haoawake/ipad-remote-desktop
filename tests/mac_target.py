"""CI 用的“靶子”窗口：把收到的每个鼠标、键盘、滚动事件都记下来，用来验证远程注入是不是真的生效。

    python3 tests/mac_target.py <输出目录>

输出目录里：
    layout.json    各区域在屏幕上的位置（Quartz 全局坐标：点，原点在主屏左上角）
    events.jsonl   收到的事件，一行一个
    anim           这个文件存在时，右上角的色块每帧变色（测连续画面的帧率）
"""
import json
import sys
import time
from pathlib import Path

import objc
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular, NSBackingStoreBuffered, NSColor, NSEvent,
                    NSFloatingWindowLevel, NSFont, NSMakeRect, NSMenu, NSMenuItem, NSScreen, NSScrollView,
                    NSTextView, NSView, NSWindow)
from Foundation import NSNotificationCenter, NSObject, NSTimer
from PyObjCTools import AppHelper

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "target")
OUT.mkdir(parents=True, exist_ok=True)
LOG = open(OUT / "events.jsonl", "a", buffering=1, encoding="utf-8")
T0 = time.time()


def log(**kw):
    kw["t"] = round(time.time() - T0, 3)
    LOG.write(json.dumps(kw, ensure_ascii=False) + "\n")


class Pad(NSView):
    def isFlipped(self):
        return True

    def acceptsFirstResponder(self):
        return True

    def drawRect_(self, r):
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.85, 0.92, 1.0, 1.0).setFill()
        from AppKit import NSRectFill
        NSRectFill(self.bounds())


class Anim(NSView):
    def drawRect_(self, r):
        from AppKit import NSRectFill
        k = (time.time() * 7) % 1.0
        NSColor.colorWithSRGBRed_green_blue_alpha_(k, 1 - k, 0.5, 1.0).setFill()
        NSRectFill(self.bounds())


class Doc(NSView):
    """一大张带格子的画布，滚动时能看出来位置变了。"""

    def isFlipped(self):
        return True

    def drawRect_(self, r):
        from AppKit import NSRectFill
        NSColor.whiteColor().setFill()
        NSRectFill(r)
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.2, 0.4, 0.9, 1.0).setFill()
        x0, y0 = int(r.origin.x // 100) * 100, int(r.origin.y // 100) * 100
        for x in range(x0, int(r.origin.x + r.size.width) + 100, 100):
            NSRectFill(NSMakeRect(x, r.origin.y, 4, r.size.height))
        for y in range(y0, int(r.origin.y + r.size.height) + 100, 100):
            NSRectFill(NSMakeRect(r.origin.x, y, r.size.width, 4))


class Delegate(NSObject):
    def textDidChange_(self, note):
        log(ev="text", value=str(note.object().string()))

    def boundsChanged_(self, note):
        o = note.object().bounds().origin
        log(ev="scrollpos", x=o.x, y=o.y)

    def occlusion_(self, note):
        w = note.object()
        log(ev="occlusion", visible=bool(w.occlusionState() & 2))

    def tick_(self, t):
        if (OUT / "anim").exists():
            self.anim.setNeedsDisplay_(True)


def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    scr = NSScreen.screens()[0].frame()
    H = scr.size.height
    w, h = int(scr.size.width * 0.6), int(scr.size.height * 0.62)
    x, top = int(scr.size.width * 0.08), int(H * 0.16)   # 左上角（Quartz 坐标）
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(x, H - top - h, w, h), 1 | 2 | 8, NSBackingStoreBuffered, False)
    win.setTitle_("RD Target")
    # 浮在普通窗口上面：「iPad 远程桌面」的状态窗口在屏幕中间，不能让它挡住靶子（隐私屏的层级比这高得多）
    win.setLevel_(NSFloatingWindowLevel)
    # 和真正的 Mac 程序一样有「编辑」菜单：⌘A 全选、⌘C 拷贝这些按键是靠菜单项起作用的
    bar, edit = NSMenu.alloc().init(), NSMenu.alloc().initWithTitle_("Edit")
    for title, sel, key in (("Select All", "selectAll:", "a"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
                            ("Undo", "undo:", "z")):
        edit.addItemWithTitle_action_keyEquivalent_(title, sel, key)
    for title, sub in (("RD Target", NSMenu.alloc().initWithTitle_("RD Target")), ("Edit", edit)):
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        it.setSubmenu_(sub)
        bar.addItem_(it)
    app.setMainMenu_(bar)
    content = win.contentView()

    half = w // 2
    tv_scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, half, h))
    tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, half, h))
    tv.setFont_(NSFont.systemFontOfSize_(20))
    tv.setAutomaticQuoteSubstitutionEnabled_(False)
    tv.setAutomaticTextReplacementEnabled_(False)
    tv.setAutomaticSpellingCorrectionEnabled_(False)
    tv_scroll.setDocumentView_(tv)
    content.addSubview_(tv_scroll)

    pad = Pad.alloc().initWithFrame_(NSMakeRect(half, h // 2, half - 120, h // 2))
    content.addSubview_(pad)
    anim = Anim.alloc().initWithFrame_(NSMakeRect(w - 120, h // 2, 120, h // 2))
    content.addSubview_(anim)
    sc = NSScrollView.alloc().initWithFrame_(NSMakeRect(half, 0, half, h // 2))
    sc.setHasVerticalScroller_(True)
    sc.setHasHorizontalScroller_(True)
    sc.setDocumentView_(Doc.alloc().initWithFrame_(NSMakeRect(0, 0, 3000, 3000)))
    content.addSubview_(sc)

    d = Delegate.alloc().init()
    d.anim = anim
    tv.setDelegate_(d)
    sc.contentView().setPostsBoundsChangedNotifications_(True)
    nc = NSNotificationCenter.defaultCenter()
    nc.addObserver_selector_name_object_(d, "boundsChanged:", "NSViewBoundsDidChangeNotification", sc.contentView())
    nc.addObserver_selector_name_object_(d, "occlusion:", "NSWindowDidChangeOcclusionStateNotification", win)

    def handler(ev):
        t = ev.type()
        p = NSEvent.mouseLocation()
        rec = {"ev": "nsevent", "type": int(t), "flags": int(ev.modifierFlags()) & 0xFFFF0000,
               "x": round(p.x, 1), "y": round(H - p.y, 1)}
        if t in (1, 2, 3, 4, 25, 26):
            rec.update(clicks=int(ev.clickCount()), btn=int(ev.buttonNumber()))
        elif t in (10, 11):
            rec.update(code=int(ev.keyCode()), chars=str(ev.characters() or ""), rep=bool(ev.isARepeat()))
        elif t == 12:
            rec.update(code=int(ev.keyCode()))
        elif t == 22:
            rec.update(dx=float(ev.scrollingDeltaX()), dy=float(ev.scrollingDeltaY()),
                       precise=bool(ev.hasPreciseScrollingDeltas()))
        log(**rec)
        return ev
    mask = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 6) | (1 << 7) | (1 << 10) | (1 << 11) | (1 << 12) \
        | (1 << 22) | (1 << 25) | (1 << 26) | (1 << 27)
    NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, handler)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(1 / 60, d, "tick:", None, True)

    win.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    win.makeFirstResponder_(tv)

    def rect(v):
        r = win.convertRectToScreen_(v.convertRect_toView_(v.bounds(), None))
        return [r.origin.x, H - r.origin.y - r.size.height, r.size.width, r.size.height]
    layout = {"window": rect(content), "text": rect(tv_scroll), "pad": rect(pad), "anim": rect(anim),
              "scroll": rect(sc), "number": int(win.windowNumber()), "screen": [scr.size.width, scr.size.height]}
    (OUT / "layout.json").write_text(json.dumps(layout), "utf-8")
    log(ev="ready", layout=layout)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    with objc.autorelease_pool():
        main()
