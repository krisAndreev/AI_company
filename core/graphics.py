"""
Graphics engine (Pillow): marketing images, product mockups, cover art and video
overlays. Everything is drawn by code from validated inputs (text, theme name,
format name, product page images). Model text is only ever rendered as text.
"""

import math
import random
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from core.design import FORMATS, Theme, font_path, mix, rgb

RESAMPLE = Image.Resampling.LANCZOS


# --- text -----------------------------------------------------------------------------------

@lru_cache(maxsize=256)
def font(key: str, size: int) -> ImageFont.FreeTypeFont:
    path = font_path(key)
    if path is None:
        return ImageFont.load_default(size)
    return ImageFont.truetype(str(path), size)


def wrap_lines(text: str, fnt, max_width: float) -> list[str]:
    lines: list[str] = []
    for paragraph in str(text).split("\n"):
        words, line = paragraph.split(), ""
        for word in words:
            while fnt.getlength(word) > max_width and len(word) > 1:  # break very long words
                cut = len(word)
                while cut > 1 and fnt.getlength(word[:cut]) > max_width:
                    cut -= 1
                if line:
                    lines.append(line)
                    line = ""
                lines.append(word[:cut])
                word = word[cut:]
            candidate = f"{line} {word}".strip()
            if fnt.getlength(candidate) <= max_width:
                line = candidate
            else:
                if line:
                    lines.append(line)
                line = word
        lines.append(line)
    return [l for l in lines if l.strip()] or [""]


def fit_text(text: str, font_key: str, box_w: int, box_h: int, max_size: int,
             min_size: int = 14, spacing: float = 1.18, max_lines: int = 8):
    """Largest font size at which the wrapped text fits the box. Returns (font, lines, line_h)."""
    size = max_size
    while True:
        fnt = font(font_key, size)
        lines = wrap_lines(text, fnt, box_w)
        line_h = int(size * spacing)
        if (len(lines) * line_h <= box_h and len(lines) <= max_lines) or size <= min_size:
            if size <= min_size and len(lines) > max_lines:
                lines = lines[:max_lines]
                lines[-1] = lines[-1].rstrip(".,;: ") + "…"
            return fnt, lines, line_h
        size = max(min_size, int(size * 0.92))


def draw_text(img: Image.Image, box: tuple[int, int, int, int], text: str, font_key: str,
              fill, max_size: int, min_size: int = 14, align: str = "center",
              valign: str = "center", spacing: float = 1.18, max_lines: int = 8,
              shadow: bool = False) -> int:
    """Draw wrapped, auto-sized text inside box (x0, y0, x1, y1). Returns the bottom y used."""
    if not str(text).strip():
        return box[1]
    x0, y0, x1, y1 = box
    fnt, lines, line_h = fit_text(text, font_key, x1 - x0, y1 - y0, max_size, min_size,
                                  spacing, max_lines)
    total = line_h * len(lines)
    y = y0 if valign == "top" else y1 - total if valign == "bottom" else y0 + (y1 - y0 - total) // 2
    draw = ImageDraw.Draw(img)
    fill = rgb(fill) if isinstance(fill, str) else fill
    for line in lines:
        w = fnt.getlength(line)
        x = x0 if align == "left" else x1 - w if align == "right" else x0 + (x1 - x0 - w) / 2
        if shadow:
            draw.text((x + 2, y + 3), line, font=fnt, fill=(0, 0, 0, 140))
        draw.text((x, y), line, font=fnt, fill=fill)
        y += line_h
    return y


# --- backgrounds --------------------------------------------------------------------------

def gradient(size: tuple[int, int], c1, c2, diagonal: bool = True) -> Image.Image:
    w, h = size
    a, b = (rgb(c1) if isinstance(c1, str) else c1), (rgb(c2) if isinstance(c2, str) else c2)
    small = Image.new("RGB", (64, 64))
    px = small.load()
    for y in range(64):
        for x in range(64):
            t = (x + y) / 126 if diagonal else y / 63
            px[x, y] = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return small.resize((w, h), Image.Resampling.BICUBIC)


def decorative_background(t: Theme, size: tuple[int, int], seed: int = 0,
                          dark: bool = False) -> Image.Image:
    """Soft gradient with blurred colour blobs and a faint dot grid. Deterministic by seed."""
    rnd = random.Random(seed)
    w, h = size
    if dark:
        base = gradient(size, mix(t.accent, "#000000", 0.35), mix(t.accent, t.accent2, 0.35))
    else:
        base = gradient(size, mix(t.soft, "#FFFFFF", 0.3), mix(t.soft, t.accent2, 0.18))
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i in range(5):
        r = int(min(w, h) * rnd.uniform(0.18, 0.42))
        cx, cy = rnd.randint(-r // 2, w + r // 2), rnd.randint(-r // 2, h + r // 2)
        color = rgb(t.accent if i % 2 else t.accent2)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*color, 46 if not dark else 70))
    layer = layer.filter(ImageFilter.GaussianBlur(min(w, h) // 14))
    out = Image.alpha_composite(base.convert("RGBA"), layer)
    dots = Image.new("RGBA", size, (0, 0, 0, 0))
    dd = ImageDraw.Draw(dots)
    step = max(18, min(w, h) // 28)
    dot = (255, 255, 255, 40) if dark else (*rgb(t.line), 45)
    for y in range(step // 2, h, step):
        for x in range(step // 2, w, step):
            dd.ellipse((x - 1.5, y - 1.5, x + 1.5, y + 1.5), fill=dot)
    return Image.alpha_composite(out, dots).convert("RGB")


def cover_fit(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale + centre-crop to fill size exactly."""
    w, h = size
    scale = max(w / img.width, h / img.height)
    resized = img.convert("RGB").resize((math.ceil(img.width * scale), math.ceil(img.height * scale)),
                                        RESAMPLE)
    left, top = (resized.width - w) // 2, (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


def contain_fit(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    scale = min(size[0] / img.width, size[1] / img.height)
    return img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), RESAMPLE)


def paste_with_shadow(canvas: Image.Image, img: Image.Image, xy: tuple[int, int],
                      rotation: float = 0, blur: int = 22, offset: tuple[int, int] = (0, 14),
                      opacity: int = 95) -> None:
    item = img.convert("RGBA")
    if rotation:
        item = item.rotate(rotation, resample=Image.Resampling.BICUBIC, expand=True)
    pad = blur * 3
    shadow = Image.new("RGBA", (item.width + pad * 2, item.height + pad * 2), (0, 0, 0, 0))
    mask = item.split()[3].point(lambda a: opacity if a > 0 else 0)
    shadow.paste((0, 0, 0, 255), (pad, pad), mask)
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    base = canvas.convert("RGBA")
    base.alpha_composite(shadow, (xy[0] - pad + offset[0], xy[1] - pad + offset[1]))
    base.alpha_composite(item, xy)
    canvas.paste(base.convert(canvas.mode))


def rounded(img: Image.Image, radius: int) -> Image.Image:
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius, fill=255)
    img.putalpha(mask)
    return img


# --- mockups --------------------------------------------------------------------------------

def paper_mockup(pages: list[Image.Image], t: Theme, size: tuple[int, int] = FORMATS["listing"],
                 seed: int = 1) -> Image.Image:
    """Up to three printed pages fanned out on a soft background."""
    canvas = decorative_background(t, size, seed)
    w, h = size
    target_h = int(h * 0.78)
    shown = pages[:3][::-1]  # back pages first
    angles = {3: [-8, 6, 0], 2: [-6, 0], 1: [0]}[len(shown)]
    shifts = {3: [-0.17, 0.17, 0], 2: [-0.12, 0.06], 1: [0]}[len(shown)]
    for page, angle, shift in zip(shown, angles, shifts):
        p = contain_fit(page.convert("RGB"), (int(w * 0.5), target_h))
        x = int(w / 2 + shift * w - p.width / 2)
        y = int((h - p.height) / 2)
        paste_with_shadow(canvas, p, (x, y), rotation=angle, blur=max(10, w // 90),
                          offset=(0, w // 110))
    return canvas


def tablet_mockup(page: Image.Image, t: Theme, size: tuple[int, int] = FORMATS["listing"],
                  seed: int = 2) -> Image.Image:
    """The page on a tablet screen (for digital planners / ebooks)."""
    canvas = decorative_background(t, size, seed)
    w, h = size
    screen = contain_fit(page.convert("RGB"), (int(w * 0.46), int(h * 0.74)))
    bezel = max(18, screen.width // 22)
    device = Image.new("RGBA", (screen.width + bezel * 2, screen.height + bezel * 2), (0, 0, 0, 0))
    ImageDraw.Draw(device).rounded_rectangle((0, 0, device.width - 1, device.height - 1),
                                             bezel * 2, fill=(28, 30, 34, 255))
    device.alpha_composite(rounded(screen, bezel // 2), (bezel, bezel))
    paste_with_shadow(canvas, device, ((w - device.width) // 2, (h - device.height) // 2),
                      blur=max(12, w // 80), offset=(0, w // 90))
    return canvas


def pages_grid(pages: list[Image.Image], t: Theme, label: str,
               size: tuple[int, int] = FORMATS["listing"], seed: int = 3) -> Image.Image:
    """A grid of page thumbnails with a label badge ('12 printable pages')."""
    canvas = decorative_background(t, size, seed)
    w, h = size
    shown = pages[:6]
    cols = 3 if len(shown) > 4 else max(1, len(shown))
    rows = math.ceil(len(shown) / cols)
    area_w, area_h = int(w * 0.86), int(h * 0.72)
    cell_w, cell_h = area_w // cols, area_h // rows
    top = int(h * 0.2)
    for i, page in enumerate(shown):
        p = contain_fit(page.convert("RGB"), (int(cell_w * 0.86), int(cell_h * 0.9)))
        cx = (w - area_w) // 2 + (i % cols) * cell_w + (cell_w - p.width) // 2
        cy = top + (i // cols) * cell_h + (cell_h - p.height) // 2
        paste_with_shadow(canvas, p, (cx, cy), blur=max(8, w // 150), offset=(0, w // 200))
    badge_h = int(h * 0.11)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((int(w * 0.25), int(h * 0.04), int(w * 0.75), int(h * 0.04) + badge_h),
                           badge_h // 2, fill=rgb(t.accent))
    draw_text(canvas, (int(w * 0.27), int(h * 0.04), int(w * 0.73), int(h * 0.04) + badge_h),
              label, t.subheading, "#FFFFFF", max_size=badge_h // 2, max_lines=1)
    return canvas


# --- marketing graphics -----------------------------------------------------------------------

def social_graphic(t: Theme, size: tuple[int, int], layout: str, headline: str,
                   subline: str = "", cta: str = "", brand: str = "",
                   photo: Image.Image | None = None, product: Image.Image | None = None,
                   seed: int = 0) -> Image.Image:
    """layout: headline | product | quote | photo. Falls back gracefully when a photo or
    product image is missing (decorative background instead)."""
    w, h = size
    unit = min(w, h)
    pad = int(unit * 0.07)
    if layout == "product" and product is not None:
        canvas = decorative_background(t, size, seed)
        band_h = int(h * (0.34 if h >= w else 0.42))
        item = contain_fit(product, (int(w * 0.84), h - band_h - pad * 2))
        paste_with_shadow(canvas, item, ((w - item.width) // 2, pad + (h - band_h - pad * 2 - item.height) // 2),
                          blur=max(10, unit // 60), offset=(0, unit // 80))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((0, h - band_h, w, h), fill=rgb(t.accent))
        text_box = (pad, h - band_h + pad // 2, w - pad, h - pad // 2 - (int(unit * 0.09) if cta else 0))
        _headline_block(canvas, t, text_box, headline, subline, "#FFFFFF", unit)
        if cta:
            _cta_pill(canvas, t, cta, (w // 2, h - pad // 2 - int(unit * 0.045)), unit, light=True)
    elif layout == "photo" and photo is not None:
        canvas = cover_fit(photo, size)
        scrim = Image.new("RGBA", size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(scrim)
        for i in range(h // 2):
            a = int(200 * (i / (h / 2)) ** 1.6)
            sd.line((0, h // 2 + i, w, h // 2 + i), fill=(0, 0, 0, a))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), scrim).convert("RGB")
        box = (pad, int(h * 0.55), w - pad, h - pad - (int(unit * 0.1) if cta else 0))
        _headline_block(canvas, t, box, headline, subline, "#FFFFFF", unit, align="left",
                        valign="bottom", shadow=True)
        if cta:
            _cta_pill(canvas, t, cta, (pad + int(unit * 0.16), h - pad - int(unit * 0.035)), unit)
    elif layout == "quote":
        canvas = decorative_background(t, size, seed, dark=True)
        draw = ImageDraw.Draw(canvas)
        qf = font("dmserif", int(unit * 0.3))
        draw.text((pad, pad - int(unit * 0.06)), "“", font=qf, fill=rgb(t.accent2))
        box = (pad, int(h * 0.22), w - pad, int(h * 0.78))
        draw_text(canvas, box, headline, t.heading, "#FFFFFF", max_size=int(unit * 0.085),
                  min_size=int(unit * 0.035), spacing=1.25)
        if subline:
            draw_text(canvas, (pad, int(h * 0.8), w - pad, int(h * 0.88)), f"— {subline}",
                      t.body_italic, mix("#FFFFFF", t.accent2, 0.3), max_size=int(unit * 0.04),
                      max_lines=1)
    else:  # headline (also the fallback for missing photo/product)
        if photo is not None:
            canvas = cover_fit(photo, size)
            veil = Image.new("RGBA", size, (*rgb(mix(t.accent, "#000000", 0.55)), 150))
            canvas = Image.alpha_composite(canvas.convert("RGBA"), veil).convert("RGB")
        else:
            canvas = decorative_background(t, size, seed, dark=True)
        box = (pad, int(h * 0.18), w - pad, int(h * 0.78))
        _headline_block(canvas, t, box, headline, subline, "#FFFFFF", unit, shadow=photo is not None)
        if cta:
            _cta_pill(canvas, t, cta, (w // 2, int(h * 0.86)), unit)
    if brand:
        if layout == "product" and product is not None:   # bottom band holds the CTA
            draw_text(canvas, (pad // 2, pad // 4, w - pad // 2, int(pad * 0.85)), brand,
                      t.subheading, t.accent, max_size=max(14, int(unit * 0.028)), max_lines=1,
                      align="left")
        else:
            draw_text(canvas, (pad, h - int(pad * 0.9), w - pad, h - int(pad * 0.25)), brand,
                      t.subheading, "#FFFFFF", max_size=max(14, int(unit * 0.028)), max_lines=1,
                      align="left" if layout == "photo" else "center")
    return canvas


def _headline_block(canvas, t: Theme, box, headline, subline, color, unit, align="center",
                    valign="center", shadow=False):
    x0, y0, x1, y1 = box
    if subline:
        split = y0 + int((y1 - y0) * 0.66)
        bottom = draw_text(canvas, (x0, y0, x1, split), headline, t.heading, color,
                           max_size=int(unit * 0.1), min_size=int(unit * 0.035), align=align,
                           valign="bottom" if valign != "top" else "top", shadow=shadow)
        draw_text(canvas, (x0, bottom + int(unit * 0.015), x1, y1), subline, t.body, color,
                  max_size=int(unit * 0.045), min_size=int(unit * 0.022), align=align,
                  valign="top", max_lines=3, shadow=shadow)
    else:
        draw_text(canvas, box, headline, t.heading, color, max_size=int(unit * 0.11),
                  min_size=int(unit * 0.035), align=align, valign=valign, shadow=shadow)


def _cta_pill(canvas, t: Theme, text: str, center: tuple[int, int], unit: int, light=False):
    fnt = font(t.subheading, max(14, int(unit * 0.036)))
    tw = fnt.getlength(text)
    ph, pw = int(unit * 0.075), int(tw + unit * 0.09)
    cx, cy = center
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((cx - pw // 2, cy - ph // 2, cx + pw // 2, cy + ph // 2), ph // 2,
                           fill=rgb("#FFFFFF" if light else t.accent2))
    draw.text((cx - tw / 2, cy - fnt.size * 0.62), text, font=fnt,
              fill=rgb(t.accent if light else t.ink))


def cover_art(t: Theme, size: tuple[int, int], seed: int = 0) -> Image.Image:
    """Abstract background art for PDF covers (the title is drawn later as PDF text)."""
    w, h = size
    canvas = decorative_background(t, size, seed)
    rnd = random.Random(seed + 7)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i in range(3):  # a few crisp arcs for structure
        r = int(min(w, h) * rnd.uniform(0.35, 0.6))
        cx, cy = rnd.choice([0, w]), rnd.choice([0, h])
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(*rgb(t.accent), 90),
                  width=max(2, w // 220))
    band = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(band).rectangle((0, int(h * 0.3), w, int(h * 0.68)),
                                   fill=(*rgb(t.paper), 215))
    return Image.alpha_composite(Image.alpha_composite(canvas.convert("RGBA"), layer),
                                 band).convert("RGB")


# --- video overlays ---------------------------------------------------------------------------

def caption_overlay(t: Theme, size: tuple[int, int], text: str, position: str = "bottom") -> Image.Image:
    """Transparent RGBA layer with a rounded caption box."""
    w, h = size
    unit = min(w, h)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    if not text.strip():
        return layer
    pad = int(unit * 0.06)
    box_w = w - pad * 2
    fnt, lines, line_h = fit_text(text, t.heading, box_w - pad, int(h * 0.22),
                                  max_size=int(unit * 0.075), min_size=int(unit * 0.04),
                                  max_lines=4)
    box_h = line_h * len(lines) + pad
    y0 = {"top": int(h * 0.1), "center": (h - box_h) // 2}.get(position, h - box_h - int(h * 0.14))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((pad, y0, w - pad, y0 + box_h), int(unit * 0.035),
                        fill=(*rgb(mix(t.accent, "#000000", 0.35)), 205))
    y = y0 + pad // 2
    for line in lines:
        lw = fnt.getlength(line)
        d.text(((w - lw) / 2, y), line, font=fnt, fill=(255, 255, 255, 255))
        y += line_h
    return layer


def end_card(t: Theme, size: tuple[int, int], title: str, cta: str, brand: str = "",
             seed: int = 5) -> Image.Image:
    img = social_graphic(t, size, "headline", title, "", cta, brand, seed=seed)
    return img


def save_png(img: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(path, "PNG", optimize=True)
    return path


def save_jpg(img: Image.Image, path: Path, quality: int = 90) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(path, "JPEG", quality=quality, optimize=True, progressive=True)
    return path
