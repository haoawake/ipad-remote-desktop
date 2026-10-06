"""画 macOS 应用图标：和 web/icon-512.png 同一个图案（显示器 + 鼠标箭头、蓝紫渐变），
但按 macOS 的规矩画成圆角方块、四周留边、带一点投影——直接拿网页那张满版的方图当图标，在程序坞里会显得很突兀。

    用法：python3 scripts/make_mac_icon.py <输出的 .iconset 目录>

之后由 build_mac.py 调 iconutil 合成 .icns。只依赖 Pillow，任何系统上都能跑（方便在 Windows 上预览）。
"""
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

SS = 4                      # 超采样倍数，画完缩小得到平滑边缘
N = 1024 * SS
BODY = (100, 100, 924, 924)  # 1024 画布上的圆角方块（Apple 图标网格：824 见方）
RADIUS = 186
C0, C1 = (79, 139, 255), (122, 92, 255)  # 左上 → 右下，取自 web 图标


def P(x, y):
    """web 图标（512 见方、满版）上的坐标 → 本图标的坐标（放进圆角方块里）。"""
    k = (BODY[2] - BODY[0]) / 512
    return ((BODY[0] + x * k) * SS, (BODY[1] + y * k) * SS)


def L(v):
    return v * (BODY[2] - BODY[0]) / 512 * SS


def gradient():
    g = Image.new("RGB", (N, N))
    px = g.load()
    # 对角线渐变：按 (x+y) 插值，逐行算太慢，先画一条再拉伸
    line = Image.new("RGB", (2 * N, 1))
    lp = line.load()
    for i in range(2 * N):
        t = i / (2 * N - 1)
        lp[i, 0] = tuple(int(C0[c] + (C1[c] - C0[c]) * t) for c in range(3))
    for y in range(N):
        g.paste(line.crop((y, 0, y + N, 1)), (0, y))
    del px
    return g


def main(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).rounded_rectangle([v * SS for v in BODY], RADIUS * SS, fill=255)

    art = Image.new("L", (N, N), 0)
    d = ImageDraw.Draw(art)
    # 显示器外框（描边 27，圆角 20）、支架、底座、箭头——尺寸量自 web/icon-512.png
    d.rounded_rectangle([*P(103, 123), *P(408, 337)], L(20), fill=255)
    d.rounded_rectangle([*P(130, 150), *P(381, 310)], L(4), fill=0)
    d.rectangle([*P(243, 337), *P(269, 386)], fill=255)
    d.rounded_rectangle([*P(185, 386), *P(326, 412)], L(3), fill=255)
    d.polygon([P(241, 183), P(241, 273), P(262, 254), P(276, 289), P(296, 280), P(282, 246), P(311, 245)], fill=255)

    body = Image.composite(Image.new("RGB", (N, N), (255, 255, 255)), gradient(), art).convert("RGBA")
    body.putalpha(mask)

    # 投影：圆角方块的形状往下挪一点、模糊、半透明
    shadow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    sm = ImageChops.offset(mask, 0, 12 * SS).filter(ImageFilter.GaussianBlur(14 * SS))
    shadow.putalpha(sm.point(lambda v: v * 0.32))
    icon = Image.alpha_composite(shadow, body).resize((1024, 1024), Image.Resampling.LANCZOS)

    icon.save(out.parent / "icon-1024.png")
    for size in (16, 32, 128, 256, 512):
        icon.resize((size, size), Image.Resampling.LANCZOS).save(out / ("icon_%dx%d.png" % (size, size)))
        icon.resize((size * 2, size * 2), Image.Resampling.LANCZOS).save(out / ("icon_%dx%d@2x.png" % (size, size)))
    print("图标已生成：", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "build/icon.iconset")
