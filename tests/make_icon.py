"""Regenerates custom_components/homepower/brand/*.png (run: python tests/make_icon.py)."""
import os
from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(__file__), "..", "custom_components", "homepower", "brand")
BG, GREEN, WHITE = (24, 38, 58, 255), (46, 204, 113, 255), (255, 255, 255, 255)
S = 4  # supersample


def icon(size: int) -> Image.Image:
    n = size * S
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, n - 1, n - 1], radius=int(n * 0.22), fill=BG)
    u = n / 100
    # battery body (horizontal) with terminal
    d.rounded_rectangle([16 * u, 30 * u, 80 * u, 70 * u], radius=6 * u, outline=WHITE, width=int(4 * u))
    d.rounded_rectangle([80 * u, 42 * u, 87 * u, 58 * u], radius=2 * u, fill=WHITE)
    # charge level
    d.rounded_rectangle([22 * u, 36 * u, 66 * u, 64 * u], radius=3 * u, fill=GREEN)
    # bolt
    bolt = [(52, 31), (36, 52), (46, 52), (40, 70), (62, 46), (51, 46), (57, 31)]
    d.polygon([(x * u, y * u) for x, y in bolt], fill=WHITE)
    # house roof
    d.line([(30 * u, 24 * u), (50 * u, 10 * u), (70 * u, 24 * u)], fill=GREEN, width=int(4 * u), joint="curve")
    return im.resize((size, size), Image.LANCZOS)


def logo(h: int = 256) -> Image.Image:
    w = h * 4
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    im.paste(icon(h), (0, 0))
    d = ImageDraw.Draw(im)
    font = None
    for name in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(name, int(h * 0.36))
            break
        except OSError:
            pass
    d.text((h * 1.12, h * 0.5), "HomePower", fill=(46, 204, 113, 255), font=font, anchor="lm")
    return im


os.makedirs(OUT, exist_ok=True)
icon(256).save(os.path.join(OUT, "icon.png"))
icon(512).save(os.path.join(OUT, "icon@2x.png"))
logo(256).save(os.path.join(OUT, "logo.png"))
logo(512).save(os.path.join(OUT, "logo@2x.png"))
print("ok")
