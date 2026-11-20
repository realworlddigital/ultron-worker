"""The Ultron sigil as an icon (tray, window, Ultron.exe): a red glowing eye on black.

``python -m ultron_worker.desktop.icon ultron.ico`` writes the multi-size .ico the build uses.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def make(size: int = 256) -> Any:
    from PIL import Image, ImageDraw, ImageFilter

    s = size * 4  # draw large, downsample for smooth edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, s - 1, s - 1), fill=(10, 3, 5, 255))
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse((s * 0.18, s * 0.18, s * 0.82, s * 0.82), fill=(255, 42, 61, 200))
    glow = glow.filter(ImageFilter.GaussianBlur(s * 0.06))
    img.alpha_composite(glow)
    d = ImageDraw.Draw(img)
    w = max(4, s // 28)
    d.ellipse((s * 0.06, s * 0.06, s * 0.94, s * 0.94), outline=(255, 42, 61, 255), width=w)
    d.ellipse((s * 0.27, s * 0.27, s * 0.73, s * 0.73), fill=(179, 0, 27, 255))
    d.ellipse((s * 0.36, s * 0.36, s * 0.64, s * 0.64), fill=(255, 42, 61, 255))
    d.ellipse((s * 0.45, s * 0.45, s * 0.55, s * 0.55), fill=(255, 245, 245, 255))
    return img.resize((size, size), Image.LANCZOS)


def save_ico(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    make(256).save(
        path,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    return path


if __name__ == "__main__":
    print(save_ico(Path(sys.argv[1] if len(sys.argv) > 1 else "ultron.ico")))
