"""
Visual design system shared by PDFs, graphics and videos: themes (fonts + colours)
and output formats. The model may only pick a theme / format by NAME; code owns
what each one looks like.

Fonts are open-licence (SIL OFL) files in assets/fonts (run setup_assets.py), so
products that embed them can be sold. Missing fonts fall back to Windows Arial for
drafts; doctor.py warns about it.
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = ROOT / "assets" / "fonts"

FONT_FILES = {
    "poppins": "Poppins-Regular.ttf",
    "poppins-semibold": "Poppins-SemiBold.ttf",
    "poppins-bold": "Poppins-Bold.ttf",
    "lato": "Lato-Regular.ttf",
    "lato-bold": "Lato-Bold.ttf",
    "lato-italic": "Lato-Italic.ttf",
    "dmserif": "DMSerifDisplay-Regular.ttf",
    "amatic": "AmaticSC-Bold.ttf",
    "pacifico": "Pacifico-Regular.ttf",
}
_WINDOWS_FALLBACK = {"bold": "arialbd.ttf", "italic": "ariali.ttf", "": "arial.ttf"}

ThemeName = Literal["modern", "classic", "playful", "minimal", "bold", "botanical"]


class Theme(BaseModel):
    name: str
    heading: str          # font key for titles
    subheading: str       # font key for section headings
    body: str
    body_bold: str
    body_italic: str
    accent: str           # main colour
    accent2: str          # highlight colour
    ink: str              # text colour
    paper: str            # page background
    soft: str             # light fill (boxes, bands)
    line: str             # rules, grid lines


THEMES: dict[str, Theme] = {t.name: t for t in [
    Theme(name="modern", heading="poppins-bold", subheading="poppins-semibold", body="lato",
          body_bold="lato-bold", body_italic="lato-italic", accent="#3D5A80", accent2="#EE6C4D",
          ink="#1F2933", paper="#FFFFFF", soft="#E8EEF4", line="#9FB3C8"),
    Theme(name="classic", heading="dmserif", subheading="dmserif", body="lato",
          body_bold="lato-bold", body_italic="lato-italic", accent="#7A4E2D", accent2="#C9A227",
          ink="#2B2118", paper="#FFFDF8", soft="#F3EBDD", line="#CDBFA8"),
    Theme(name="playful", heading="pacifico", subheading="poppins-semibold", body="poppins",
          body_bold="poppins-bold", body_italic="lato-italic", accent="#E84A7F", accent2="#FFB84C",
          ink="#2F2E41", paper="#FFFFFF", soft="#FFF0F5", line="#F4A7C0"),
    Theme(name="minimal", heading="poppins-semibold", subheading="poppins-semibold", body="lato",
          body_bold="lato-bold", body_italic="lato-italic", accent="#222222", accent2="#9A8C7A",
          ink="#222222", paper="#FFFFFF", soft="#F3F3F1", line="#BDBDBD"),
    Theme(name="bold", heading="poppins-bold", subheading="poppins-bold", body="poppins",
          body_bold="poppins-bold", body_italic="lato-italic", accent="#5B3E96", accent2="#FFC53D",
          ink="#1B1B1E", paper="#FFFFFF", soft="#EFE9F7", line="#B9A6D9"),
    Theme(name="botanical", heading="dmserif", subheading="poppins-semibold", body="lato",
          body_bold="lato-bold", body_italic="lato-italic", accent="#4F7942", accent2="#D4A373",
          ink="#233021", paper="#FCFCF7", soft="#EDF3E8", line="#A9C29F"),
]}

# Pixel sizes for marketing images and videos.
FORMATS: dict[str, tuple[int, int]] = {
    "square": (1080, 1080),       # Instagram / Facebook feed
    "portrait": (1080, 1350),     # Instagram portrait
    "story": (1080, 1920),        # Stories, Reels, TikTok, Shorts
    "pin": (1000, 1500),          # Pinterest
    "landscape": (1200, 630),     # blog / link previews / X
    "listing": (2400, 1800),      # Etsy / Gumroad listing images (4:3)
    "thumbnail": (1280, 720),     # YouTube
}
FormatName = Literal["square", "portrait", "story", "pin", "landscape", "listing", "thumbnail"]


def theme(name: str) -> Theme:
    return THEMES.get(name, THEMES["modern"])


def font_path(key: str) -> Path | None:
    """The TTF for a font key, falling back to Windows Arial (drafts only)."""
    fname = FONT_FILES.get(key)
    if fname and (FONTS_DIR / fname).is_file():
        return FONTS_DIR / fname
    style = "bold" if "bold" in key else "italic" if "italic" in key else ""
    fallback = Path("C:/Windows/Fonts") / _WINDOWS_FALLBACK[style]
    return fallback if fallback.is_file() else None


def fonts_installed() -> list[str]:
    return [k for k, f in FONT_FILES.items() if (FONTS_DIR / f).is_file()]


def rgb(hex_color: str | tuple) -> tuple[int, int, int]:
    if isinstance(hex_color, tuple):
        return tuple(hex_color[:3])
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def mix(c1: str | tuple, c2: str | tuple, t: float) -> tuple[int, int, int]:
    a = rgb(c1) if isinstance(c1, str) else c1
    b = rgb(c2) if isinstance(c2, str) else c2
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
