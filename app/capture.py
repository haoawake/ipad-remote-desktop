"""截屏 + 分块差分 + JPEG 编码。

每个连接一个 Encoder，运行在自己的单线程里（mss 不能跨线程用）。
只发送变化的 64x64 块；画面静止 0.4 秒后，把之前低质量发出的块用高质量补发一次，
这样动的时候省流量、停下来的时候文字清晰。
"""
import io
import struct
import time

import mss
import numpy as np
from PIL import Image

TILE = 64
REFINE_DELAY = 0.4
REFINE_QUALITY = 88


def list_monitors():
    with mss.MSS() as s:
        return [{"index": i, "left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}
                for i, m in enumerate(s.monitors) if i > 0]


class Encoder:
    def __init__(self):
        self.sct = None
        self.monitor = 1
        self.scale = 0.75
        self.quality = 65
        self.prev = None
        self.lq_time = None
        self.frame_id = 0
        self.force_full = True
        self.geom = None

    # ---- 配置
    def configure(self, monitor=None, scale=None, quality=None):
        if monitor is not None:
            self.monitor = int(monitor)
        if scale is not None:
            self.scale = float(min(max(scale, 0.25), 1.0))
        if quality is not None:
            self.quality = int(min(max(quality, 20), 95))
        self.prev = None
        self.force_full = True

    def _mon(self):
        if self.sct is None:
            self.sct = mss.MSS()
        mons = self.sct.monitors
        if self.monitor < 1 or self.monitor >= len(mons):
            self.monitor = 1
        return mons[self.monitor]

    def geometry(self):
        m = self._mon()
        sw, sh = max(1, round(m["width"] * self.scale)), max(1, round(m["height"] * self.scale))
        return {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"],
                "sw": sw, "sh": sh, "scale": self.scale, "monitor": self.monitor}

    # ---- 主流程
    def produce(self):
        """截一帧，返回 (二进制消息或 None, 统计信息)。"""
        m = self._mon()
        geom = self.geometry()
        if geom != self.geom:
            self.geom = geom
            self.prev = None
        shot = self.sct.grab(m)
        W, H = shot.width, shot.height
        cur = np.frombuffer(shot.bgra, np.uint32).reshape(H, W)
        ty, tx = -(-H // TILE), -(-W // TILE)
        now = time.monotonic()

        if self.prev is None or self.prev.shape != cur.shape or self.force_full:
            dirty = np.ones((ty, tx), bool)
            self.lq_time = np.zeros((ty, tx))
            self.force_full = False
        else:
            neq = cur != self.prev
            rows = np.logical_or.reduceat(neq, np.arange(0, H, TILE), axis=0)
            dirty = np.logical_or.reduceat(rows, np.arange(0, W, TILE), axis=1)
        self.prev = cur

        q = self.quality
        lq_stamp = now if q < REFINE_QUALITY else 0
        # 已经静止一段时间、但之前是低质量发出去的块 → 高质量补发（逐块判断，光标闪烁不影响别处）
        due = ~dirty & (self.lq_time > 0) & (now - self.lq_time > REFINE_DELAY)
        n_dirty = int(dirty.sum())
        if n_dirty == 0 and not due.any():
            return None, None
        if n_dirty > 0.6 * tx * ty:  # 变化面积过大就直接整帧
            jobs = [((0, 0, tx, ty), q)]
            self.lq_time[:] = lq_stamp
        else:
            jobs = [(r, q) for r in self._rects(dirty)] + [(r, REFINE_QUALITY) for r in self._rects(due)]
            self.lq_time[dirty] = lq_stamp
            self.lq_time[due] = 0
        refine = n_dirty == 0

        self.frame_id = (self.frame_id + 1) & 0xFFFFFFFF
        parts = []
        s = self.scale
        for (rx, ry, rw, rh), q in jobs:
            x0, y0 = rx * TILE, ry * TILE
            x1, y1 = min(W, (rx + rw) * TILE), min(H, (ry + rh) * TILE)
            sub = cur[y0:y1, x0:x1]
            img = Image.frombytes("RGB", (x1 - x0, y1 - y0), sub.tobytes(), "raw", "BGRX")
            sx0, sy0 = round(x0 * s), round(y0 * s)
            sx1, sy1 = round(x1 * s), round(y1 * s)
            if s != 1.0:
                tw, th = max(1, sx1 - sx0), max(1, sy1 - sy0)
                if abs(s - 0.5) < 1e-6 and img.width % 2 == 0 and img.height % 2 == 0:
                    img = img.reduce(2)
                else:
                    img = img.resize((tw, th), Image.Resampling.BOX)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=q, subsampling=0 if q >= 85 else 2)
            data = buf.getvalue()
            parts.append(struct.pack("<HHHHI", sx0, sy0, img.width, img.height, len(data)))
            parts.append(data)
        header = struct.pack("<BIH", 1, self.frame_id, len(jobs))
        msg = header + b"".join(parts)
        return msg, {"id": self.frame_id, "rects": len(jobs), "bytes": len(msg), "refine": refine, "tiles": n_dirty}

    @staticmethod
    def _rects(mask):
        """把脏块掩码合并成矩形列表 (tx, ty, tw, th)，单位是块。"""
        out = []
        open_runs = {}
        ty, tx = mask.shape
        for y in range(ty):
            row = mask[y]
            runs = []
            x = 0
            while x < tx:
                if row[x]:
                    x0 = x
                    while x < tx and row[x]:
                        x += 1
                    runs.append((x0, x))
                else:
                    x += 1
            new_open = {}
            for r in runs:
                if r in open_runs:
                    rect = open_runs.pop(r)
                    rect[3] += 1
                    new_open[r] = rect
                else:
                    new_open[r] = [r[0], y, r[1] - r[0], 1]
            out.extend(open_runs.values())
            open_runs = new_open
        out.extend(open_runs.values())
        return [tuple(r) for r in out]

    def close(self):
        if self.sct is not None:
            try:
                self.sct.close()
            except Exception:
                pass
            self.sct = None
