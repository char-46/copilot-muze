"""Local media operations for the agent: chart generation and thumbnails.

Charts are rendered with Pillow (no matplotlib); the resulting PNG can be
sent like any other media via the `send_media` tool.
"""

from __future__ import annotations

import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_BG = (255, 255, 255)
_FG = (33, 33, 33)
_GRID = (220, 220, 220)
_BAR = (66, 133, 244)
_LINE = (219, 68, 55)

_CJK_FONTS = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


def _font(size: int) -> ImageFont.ImageFont:
    for p in _CJK_FONTS:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def make_chart(
    out_dir: Path | str,
    title: str,
    labels: list,
    values: list,
    kind: str = "bar",
    width: int = 820,
    height: int = 480,
) -> Path:
    """Render a simple bar/line chart to a PNG under out_dir/charts/."""
    if not labels or not values or len(labels) != len(values):
        raise ValueError("labels 与 values 必须等长且非空")
    vals = [float(v) for v in values]
    out = Path(out_dir) / "charts"
    out.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGB", (width, height), _BG)
    d = ImageDraw.Draw(img)
    f_title = _font(22)
    f_small = _font(13)

    d.text((width // 2, 16), str(title)[:80], fill=_FG, font=f_title, anchor="ma")

    ml, mr, mt, mb = 60, 30, 60, 70
    x0, y0 = ml, height - mb
    x1, y1 = width - mr, mt
    d.line([(x0, y1), (x0, y0)], fill=_GRID, width=1)  # y axis
    d.line([(x0, y0), (x1, y0)], fill=_GRID, width=1)  # x axis

    vmax = max(vals) or 1.0
    vmin = min(0.0, min(vals))
    span = (vmax - vmin) or 1.0
    plot_w = x1 - x0
    plot_h = y0 - y1

    def ypix(v: float) -> float:
        return y0 - (v - vmin) / span * plot_h

    n = len(vals)
    if kind == "line":
        pts = [(x0 + plot_w * (i + 0.5) / n, ypix(v)) for i, v in enumerate(vals)]
        d.line(pts, fill=_LINE, width=3)
        for (px, py), v in zip(pts, vals):
            d.ellipse([px - 4, py - 4, px + 4, py + 4], fill=_LINE)
            d.text((px, py - 16), f"{v:g}", fill=_FG, font=f_small, anchor="mb")
    else:
        slot = plot_w / n
        bw = max(6, int(slot * 0.6))
        for i, v in enumerate(vals):
            cx = x0 + slot * (i + 0.5)
            py = ypix(v)
            d.rectangle([cx - bw / 2, py, cx + bw / 2, y0], fill=_BAR)
            d.text((cx, py - 6), f"{v:g}", fill=_FG, font=f_small, anchor="mb")

    for i, lb in enumerate(labels):
        cx = x0 + plot_w * (i + 0.5) / n
        d.text((cx, y0 + 8), str(lb)[:16], fill=_FG, font=f_small, anchor="ma")

    path = out / f"chart-{int(time.time())}.png"
    img.save(path, "PNG")
    return path


def thumbnail(path: Path | str, size: int = 320) -> Path:
    """Downscale an image in place to a max dimension (writes *_thumb.png)."""
    src = Path(path)
    img = Image.open(src)
    img.thumbnail((size, size))
    dst = src.with_name(src.stem + "_thumb.png")
    img.save(dst, "PNG")
    return dst
