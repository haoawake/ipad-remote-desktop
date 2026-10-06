"""不依赖真实屏幕的单元测试（Windows 和 macOS 上都跑）：python -m unittest discover -s tests -v"""
import io
import json
import re
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

MAC = sys.platform == "darwin"


class FakeShot:
    def __init__(self, arr):
        self.height, self.width = arr.shape
        self.bgra = arr


class FakeGrabber:
    """假的截屏：monitors 和 grab 的样子与 mss / macapi.Grabber 相同。"""

    def __init__(self, w, h, shot_w=None, shot_h=None):
        self.monitors = [None, {"left": 0, "top": 0, "width": w, "height": h, "pw": w, "ph": h, "ps": 1.0}]
        self.frame = np.full((shot_h or h, shot_w or w), 0xFF336699, np.uint32)

    def grab(self, m, *a):
        return FakeShot(self.frame.copy())

    def close(self):
        pass


def parse(msg):
    kind, fid, n = struct.unpack_from("<BIH", msg)
    off, rects = 7, []
    for _ in range(n):
        x, y, w, h, ln = struct.unpack_from("<HHHHI", msg, off)
        off += 12
        img = Image.open(io.BytesIO(msg[off:off + ln]))
        rects.append((x, y, w, h, img.size))
        off += ln
    return kind, rects


class EncoderTest(unittest.TestCase):
    def setUp(self):
        import capture
        self.capture = capture

    def make(self, grabber, scale=1.0):
        enc = self.capture.Encoder()
        enc.sct = grabber
        enc.configure(scale=scale, quality=60)
        return enc

    def test_full_then_idle_then_tile(self):
        g = FakeGrabber(256, 128)
        enc = self.make(g)
        msg, info = enc.produce()
        kind, rects = parse(msg)
        self.assertEqual(kind, 1)
        self.assertEqual(rects, [(0, 0, 256, 128, (256, 128))])
        self.assertEqual(enc.produce(), (None, None))  # 画面没变：不发
        g.frame[70:80, 140:150] = 0xFF000000            # 改一小块：只发那一个 64x64 块
        msg, info = enc.produce()
        _, rects = parse(msg)
        self.assertEqual([r[:4] for r in rects], [(128, 64, 64, 64)])

    def test_scaled(self):
        enc = self.make(FakeGrabber(256, 128), scale=0.5)
        _, rects = parse(enc.produce()[0])
        self.assertEqual(rects[0][4], (128, 64))
        self.assertEqual(enc.geometry()["sw"], 128)

    def test_point_resolution_capture(self):
        """macOS 的低分辨率档按“点”截：截出来的图只有一半大，Encoder 要按 2 倍换算，不能再缩一次。"""
        g = FakeGrabber(512, 256, shot_w=256, shot_h=128)
        g.monitors[1]["ps"] = 2.0
        enc = self.make(g, scale=0.5)
        geo = enc.geometry()
        self.assertEqual((geo["width"], geo["sw"], geo.get("ps")), (512, 256, 2.0))
        _, rects = parse(enc.produce()[0])
        self.assertEqual(rects, [(0, 0, 256, 128, (256, 128))])

    def test_rects_merge(self):
        mask = np.zeros((3, 4), bool)
        mask[0, 1:3] = True
        mask[1, 1:3] = True
        mask[2, 0] = True
        rects = sorted(self.capture.Encoder._rects(mask))
        self.assertEqual(rects, [(0, 2, 1, 1), (1, 0, 2, 2)])


class WebTest(unittest.TestCase):
    def test_both_shortcut_panels(self):
        html = (ROOT / "web" / "index.html").read_text("utf-8")
        self.assertEqual(len(re.findall(r'class="keyrow" data-host="win"', html)), 3)
        self.assertEqual(len(re.findall(r'class="keyrow" data-host="mac" hidden', html)), 3)
        # Windows 那一套还是原来的按键
        for combo in ("ControlLeft+KeyC", "AltLeft+Tab", "MetaLeft+KeyD", "ControlLeft+ShiftLeft+Escape"):
            self.assertIn('data-combo="%s"' % combo, html)
        for combo in ("MetaLeft+KeyC", "MetaLeft+Tab", "ControlLeft+ArrowUp", "AltLeft+MetaLeft+Escape",
                      "MetaLeft+Space"):
            self.assertIn('data-combo="%s"' % combo, html)


@unittest.skipUnless(MAC, "只在 macOS 上跑")
class MacKeymapTest(unittest.TestCase):
    def test_keymap(self):
        import macapi
        codes = set(re.findall(r'data-(?:code|combo)="([^"]+)"', (ROOT / "web" / "index.html").read_text("utf-8")))
        keys = {k for c in codes for k in c.split("+")}
        missing = [k for k in keys if k not in macapi.CODE_KC and k not in macapi.MEDIA]
        self.assertEqual(missing, [], "快捷键面板里有 macOS 不认识的按键")
        self.assertEqual(macapi.CODE_KC["KeyA"], 0)
        self.assertEqual(macapi.CODE_KC["MetaLeft"], 0x37)

    def test_coords(self):
        import macapi
        g = {"left": 100.0, "top": 50.0, "ps": 2.0}
        self.assertEqual(macapi.to_global(g, 200, 100), (200.0, 100.0))
        self.assertEqual(macapi.to_local(g, 200.0, 100.0), (200.0, 100.0))

    def test_displays(self):
        import macapi
        ds = macapi._displays()
        self.assertTrue(ds and ds[0]["width"] > 0)
        print(json.dumps(ds))


class OpenUrlTest(unittest.TestCase):
    def test_only_http(self):
        from unittest import mock
        import server
        opened = []
        with mock.patch.object(server.osapi, "open_url", opened.append):
            for u in ("javascript:alert(1)", "file:///etc/passwd", "mailto:a@b.c", "data:text/html,x"):
                self.assertEqual(server.open_url(u), "只支持 http/https 网址", u)
            self.assertEqual(opened, [])
            for u, want in (("bilibili.com", "https://bilibili.com"), ("localhost:8080/x", "https://localhost:8080/x"),
                            ("http://a.b/c?d=1", "http://a.b/c?d=1"), ("192.168.1.2:8000", "https://192.168.1.2:8000")):
                self.assertTrue(server.open_url(u).startswith("已在电脑上打开"), u)
                self.assertEqual(opened[-1], want)


if __name__ == "__main__":
    unittest.main()
