"""
Digital products: printable planners, worksheets, checklists, trackers, journals,
workbooks and ebooks / guides, delivered as print-ready PDFs.

AI proposes, code validates and renders:
  1. ProductBrief     (the tool's params: type, title, audience, pages, theme)
  2. ProductOutline   one model proposal: the page list
  3. PageContent      one model proposal per page, built from a FIXED set of block
                      types (heading, text, checklist, table, tracker, calendar, ...)
  4. ProductListing   title / description / 13 tags / suggested price for Etsy-style shops
Code then renders A4 + US Letter PDFs (open-licence fonts embedded), page previews,
three mockup images, a zip bundle for upload and a listing file, and registers all
of them as DRAFT assets for the owner to approve. Publishing stays manual.
"""

import calendar
import io
import re
import zipfile
from pathlib import Path
from typing import Annotated, Literal

from fpdf import FPDF, XPos, YPos
from PIL import Image
from pydantic import Field, field_validator, model_validator

from core import graphics
from core.design import Theme, ThemeName, font_path, rgb, theme
from clients.base import ModelClientError
from core.net import ToolError
from core.schemas import StrictModel
from core.structured import StructuredOutputError
from core.studio import brand_name
from core.tasks import utcnow
from core.tools import Tool, ToolContext, ToolOutput
from core.workspace import new_id, slugify

ACTOR = "STUDIO"
ProductType = Literal["planner", "worksheet", "checklist", "tracker", "journal", "workbook",
                      "ebook", "guide"]
TEXT_HEAVY = {"ebook", "guide", "workbook"}
_PLACEHOLDER = re.compile(r"lorem ipsum|\[insert|\[your|\{\{|todo:|xxx", re.I)


# --- what the model may propose --------------------------------------------------------------

class PagePlan(StrictModel):
    title: str = Field(min_length=2, max_length=60)
    purpose: str = Field(min_length=5, max_length=240)


class ProductBrief(StrictModel):
    product_type: ProductType
    title: str = Field(min_length=3, max_length=70)
    subtitle: str = Field(default="", max_length=110)
    audience: str = Field(min_length=3, max_length=200, description="who buys and uses it")
    content_notes: str = Field(default="", max_length=1200,
                               description="what it must contain, based on research")
    pages: int = Field(default=8, ge=3, le=24,
                       description="content pages (cover, contents and license are added by code)")
    theme: ThemeName = "modern"
    page_plan: list[PagePlan] = Field(
        default_factory=list, max_length=24,
        description="optional fixed page list (title + purpose); if given, no outline is planned")


class ProductOutline(StrictModel):
    pages: list[PagePlan] = Field(min_length=1, max_length=24)


BlockType = Literal["heading", "text", "bullets", "numbered", "checklist", "lines", "table",
                    "tracker", "calendar", "notes", "quote", "cards", "certificate", "court"]

# Tennis court diagrams (block type "court"). Each item holds one or more commands separated
# by ";". Coordinates: x 0-100 across the doubles court (0 = left sideline), y 0-100 along it
# (0 = bottom baseline = "our" side, 50 = net, 100 = top baseline); a little space outside the
# lines is allowed. "---" starts the next panel (max 3 side by side).
COURT_X, COURT_Y = (-12.0, 112.0), (-10.0, 110.0)
_COURT_ARGS = {"title": 0, "us": 2, "them": 2, "ball": 2, "shot": 4, "move": 4, "zone": 4,
               "note": 2}


def parse_court(items: list[str]) -> list[list[tuple]]:
    """Court commands -> panels of (command, numbers, text). Raises ValueError on bad input."""
    panels: list[list[tuple]] = [[]]
    for item in items:
        for cmd in (c.strip() for c in item.split(";")):
            if not cmd:
                continue
            if cmd == "---":
                panels.append([])
                continue
            name, _, rest = cmd.partition(" ")
            name = name.lower()
            if name not in _COURT_ARGS:
                raise ValueError(f"unknown court command {name!r}")
            n = _COURT_ARGS[name]
            parts = rest.split()
            try:
                nums = [float(p) for p in parts[:n]]
            except ValueError:
                raise ValueError(f"court {name}: needs {n} numbers") from None
            if len(nums) != n:
                raise ValueError(f"court {name}: needs {n} numbers")
            for i, v in enumerate(nums):
                lo, hi = COURT_X if i % 2 == 0 else COURT_Y
                if not lo <= v <= hi:
                    raise ValueError(f"court {name}: coordinate {v} outside the drawing")
            text = " ".join(parts[n:])
            if name in ("us", "them") and len(text) > 3:
                raise ValueError("court player labels have at most 3 characters")
            if name in ("title", "note") and not text:
                raise ValueError(f"court {name} needs text")
            panels[-1].append((name, nums, text))
    panels = [p for p in panels if p]
    if not 1 <= len(panels) <= 3:
        raise ValueError("a court block has 1 to 3 panels")
    return panels


class Block(StrictModel):
    type: BlockType
    text: str = Field(default="", max_length=2000,
                      description="heading/text/quote content, or the label of a fill-in block")
    items: list[Annotated[str, Field(max_length=160)]] = Field(default_factory=list, max_length=20)
    columns: list[Annotated[str, Field(max_length=24)]] = Field(default_factory=list, max_length=6)
    count: int = Field(default=0, ge=0, le=31,
                       description="writing lines / empty table rows / tracker days / notes height")

    @model_validator(mode="after")
    def _check(self):
        if self.type in ("heading", "text", "quote", "certificate") and not self.text.strip():
            raise ValueError(f"a {self.type} block needs text")
        if self.type in ("bullets", "numbered", "checklist", "tracker", "cards") and not self.items:
            raise ValueError(f"a {self.type} block needs items")
        if self.type == "table" and not self.columns:
            raise ValueError("a table block needs columns")
        if self.type == "court":
            parse_court(self.items)
        return self


class PageContent(StrictModel):
    blocks: list[Block] = Field(min_length=1, max_length=10)


class ProductListing(StrictModel):
    title: str = Field(min_length=20, max_length=140, description="marketplace listing title")
    description: str = Field(min_length=150, max_length=2500)
    tags: list[Annotated[str, Field(min_length=2, max_length=20)]] = Field(
        min_length=5, max_length=13, description="search tags, each at most 20 characters")
    price_eur: float = Field(ge=0.5, le=100, description="suggested price")

    @field_validator("tags")
    @classmethod
    def _tags(cls, v):
        seen = []
        for tag in v:
            tag = re.sub(r"[\"'“”‘’`]", "", tag)
            tag = re.sub(r"\s+", " ", tag.strip(" ,.;:").lower())
            if tag and tag not in seen:
                seen.append(tag)
        return seen


class WrittenPage(StrictModel):
    title: str = Field(min_length=2, max_length=60)
    blocks: list[Block] = Field(min_length=1, max_length=10)


class WrittenProduct(ProductBrief):
    """Call-time params for runs started by the owner or the orchestrator: finished pages and/or
    a finished listing replace the local model's writing. Workers only see ProductBrief."""
    written_pages: list[WrittenPage] = Field(default_factory=list, max_length=24)
    listing: ProductListing | None = None


# --- PDF rendering ----------------------------------------------------------------------------

_REPLACE = {"→": "->", "←": "<-", "✓": "", "✔": "", "★": "*",
            "☐": "", "☑": "", "□": "", "•": "•", "\t": " "}
_EMOJI = re.compile("[\U00010000-\U0010FFFF☀-➿️‍]")


def clean_text(text: str) -> str:
    for a, b in _REPLACE.items():
        text = text.replace(a, b)
    text = _EMOJI.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)          # markdown links -> label
    text = re.sub(r"\(?\bhttps?://\S+?\)?(?=[\s,;]|$)", "", text)  # no web addresses in print
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    text = re.sub(r"__(\S[^_]*?\S)__", r"\1", text.replace("**", ""))
    text = re.sub(r"(?<![\w*])\*(\S(?:[^*\n]*?\S)?)\*(?![\w*])", r"\1", text)   # *emphasis*
    return re.sub(r"[ ]{2,}", " ", text).strip()


class ProductPDF(FPDF):
    MARGIN = 16
    TOP = 32          # content starts below the page header band
    BOTTOM = 18

    def __init__(self, t: Theme, page_format: str, brand: str, product_title: str):
        super().__init__(orientation="P", unit="mm", format=page_format)
        self.t = t
        self.brand = brand
        self.product_title = product_title
        self.page_title = ""
        self.chrome = False
        self.week_starts_sunday = page_format.lower() == "letter"
        self.core_fonts = False
        self.fresh_page = False     # True: the next content page reuses the current empty page
        self._register_fonts()
        self.set_margins(self.MARGIN, self.TOP, self.MARGIN)
        self.set_auto_page_break(True, margin=self.BOTTOM)
        self.set_title(product_title)
        self.set_author(brand or "")
        self.set_creator("AI Company studio")

    # fonts + colours
    def _register_fonts(self):
        fams = {"H": [("", self.t.heading)], "S": [("", self.t.subheading)],
                "Body": [("", self.t.body), ("B", self.t.body_bold), ("I", self.t.body_italic)]}
        try:
            for fam, styles in fams.items():
                for style, key in styles:
                    path = font_path(key)
                    if path is None:
                        raise FileNotFoundError(key)
                    self.add_font(fam, style, str(path))
        except (FileNotFoundError, RuntimeError):
            self.core_fonts = True

    def font(self, fam: str, size: float, style: str = ""):
        if self.core_fonts:
            self.set_font("helvetica", "B" if fam in ("H", "S") else style, size)
        else:
            self.set_font(fam, style if fam == "Body" else "", size)

    def text_safe(self, s: str) -> str:
        s = clean_text(s)
        return s.encode("latin-1", "replace").decode("latin-1") if self.core_fonts else s

    def color(self, kind: str, hex_color: str):
        r, g, b = rgb(hex_color)
        {"text": self.set_text_color, "draw": self.set_draw_color,
         "fill": self.set_fill_color}[kind](r, g, b)

    # chrome
    def header(self):
        if not self.chrome:
            return
        self.color("fill", self.t.soft)
        self.rect(0, 0, self.w, 24, style="F")
        self.color("fill", self.t.accent)
        self.rect(0, 24, self.w, 1.2, style="F")
        self.font("S", 15)
        self.color("text", self.t.accent)
        self.set_xy(self.MARGIN, 8)
        self.cell(self.epw, 9, self.text_safe(self.page_title), new_x=XPos.LMARGIN,
                  new_y=YPos.NEXT)
        self.set_y(self.TOP)

    def footer(self):
        if not self.chrome or self.page_no() == 1:   # never on the cover
            return
        self.set_y(-12)
        self.font("Body", 8)
        self.color("text", self.t.line)
        left = self.text_safe(f"{self.product_title}" + (f"  |  {self.brand}" if self.brand else ""))
        self.cell(self.epw / 2, 6, left)
        self.cell(self.epw / 2, 6, str(self.page_no()), align="R")

    def body_bottom(self) -> float:
        return self.h - self.BOTTOM

    def ensure(self, height: float):
        if self.get_y() + height > self.body_bottom():
            self.add_page()

    def full_body_height(self) -> float:
        return self.body_bottom() - self.TOP

    # pages
    def cover(self, title: str, subtitle: str, tagline: str, art: Image.Image):
        self.chrome = False
        self.add_page()
        art = graphics.cover_fit(art, (1240, round(1240 * self.h / self.w)))
        self.image(art, x=0, y=0, w=self.w, h=self.h)
        top, bottom = self.h * 0.3, self.h * 0.68
        width = self.w - 2 * 22
        size = 40
        while size > 18:
            self.font("H", size)
            lines = self.multi_cell(width, size * 0.48, self.text_safe(title), align="C",
                                    dry_run=True, output="LINES")
            if len(lines) <= 3:
                break
            size -= 2
        block = len(lines) * size * 0.48 + (16 if subtitle else 0) + (10 if tagline else 0)
        self.set_xy(22, top + (bottom - top - block) / 2)
        self.color("text", self.t.ink)
        self.multi_cell(width, size * 0.48, self.text_safe(title), align="C",
                        new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if subtitle:
            self.ln(4)
            self.set_x(22)
            self.font("S", 14)
            self.color("text", self.t.accent)
            self.multi_cell(width, 7, self.text_safe(subtitle), align="C",
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if tagline:
            self.ln(2)
            self.set_x(22)
            self.font("Body", 10, "I")
            self.color("text", self.t.ink)
            self.multi_cell(width, 5.5, self.text_safe(tagline), align="C",
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if self.brand:
            self.set_xy(22, self.h - 26)
            self.font("S", 11)
            self.color("text", self.t.accent)
            self.cell(width, 6, self.text_safe(self.brand), align="C")

    def content_page(self, title: str, blocks: list[Block], section: bool = False,
                     fill_rest: bool = False):
        self.page_title = title
        self.chrome = True
        if self.fresh_page:
            self.fresh_page = False
            self.header()
        else:
            self.add_page()
        if section:
            try:
                self.start_section(self.text_safe(title))
            except Exception:
                pass
        for block in blocks:
            getattr(self, f"_b_{block.type}")(block)
            self.ln(3.5)
        rest = self.body_bottom() - self.get_y() - 10
        if fill_rest and rest > 45:   # printables: never leave half a page empty
            self._label("Notes")
            self._notes_area(self.get_y(), rest - 8)

    def _notes_area(self, y: float, height: float):
        self.color("draw", self.t.line)
        self.set_line_width(0.25)
        gap = 8.5
        for i in range(1, int(height / gap) + 1):
            self.line(self.MARGIN, y + i * gap, self.MARGIN + self.epw, y + i * gap)
        self.set_y(y + height)

    def closing_page(self, title: str, paragraphs: list[str]):
        self.page_title = title
        self.chrome = True
        if self.fresh_page:
            self.fresh_page = False
            self.header()
        else:
            self.add_page()
        for i, p in enumerate(paragraphs):
            self.font("Body", 10.5, "B" if i % 2 == 0 else "")
            self.color("text", self.t.ink)
            self.multi_cell(self.epw, 5.6, self.text_safe(p), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.ln(2 if i % 2 == 0 else 5)

    # blocks
    def _b_heading(self, b: Block):
        self.ensure(14)
        self.font("S", 13.5)
        self.color("text", self.t.accent)
        self.multi_cell(self.epw, 7, self.text_safe(b.text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.color("draw", self.t.accent2)
        self.set_line_width(0.6)
        self.line(self.MARGIN, self.get_y() + 0.6, self.MARGIN + 18, self.get_y() + 0.6)
        self.ln(2.5)

    def _b_text(self, b: Block):
        self.ensure(12)
        self.font("Body", 10.5)
        self.color("text", self.t.ink)
        for para in [p for p in b.text.split("\n") if p.strip()]:
            self.multi_cell(self.epw, 5.4, self.text_safe(para), align="J",
                            new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.ln(1.6)

    def _list(self, b: Block, marker: str):
        self.font("Body", 10.5)
        for i, item in enumerate(b.items, start=1):
            self.ensure(8)
            y = self.get_y()
            self.color("draw", self.t.accent)
            self.color("fill", self.t.accent)
            self.set_line_width(0.35)
            if marker == "bullet":
                self.rect(self.MARGIN + 1, y + 2, 1.8, 1.8, style="F")
            elif marker == "box":
                self.rect(self.MARGIN, y + 0.6, 4.4, 4.4, style="D", round_corners=True,
                          corner_radius=0.8)
            else:
                self.font("S", 10)
                self.color("text", self.t.accent)
                self.set_xy(self.MARGIN, y)
                self.cell(7, 5.4, f"{i}.")
                self.font("Body", 10.5)
            self.color("text", self.t.ink)
            self.set_xy(self.MARGIN + 8, y)
            self.multi_cell(self.epw - 8, 5.4, self.text_safe(item), new_x=XPos.LMARGIN,
                            new_y=YPos.NEXT)
            self.ln(1.8 if marker == "box" else 0.8)

    def _b_bullets(self, b: Block):
        self._list(b, "bullet")

    def _b_numbered(self, b: Block):
        self._list(b, "number")

    def _b_checklist(self, b: Block):
        self._list(b, "box")

    def _label(self, text: str):
        if text.strip():
            self.font("S", 10.5)
            self.color("text", self.t.ink)
            self.multi_cell(self.epw, 6, self.text_safe(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.ln(1)

    def _b_lines(self, b: Block):
        count = b.count or 8
        gap = 8.5
        count = min(count, int((self.full_body_height() - 10) / gap))
        self.ensure(7 + min(count, 4) * gap)
        self._label(b.text)
        self.color("draw", self.t.line)
        self.set_line_width(0.25)
        for _ in range(count):
            self.ensure(gap)
            y = self.get_y() + gap
            self.line(self.MARGIN, y, self.MARGIN + self.epw, y)
            self.set_y(y)
        self.ln(2)

    def _b_notes(self, b: Block):
        height = min(max((b.count or 6) * 8, 30), self.full_body_height() - 12)
        self.ensure(height + 8)
        self._label(b.text or "Notes")
        y = self.get_y()
        self.color("draw", self.t.line)
        self.color("fill", self.t.soft)
        self.set_line_width(0.3)
        self.rect(self.MARGIN, y, self.epw, height, style="DF", round_corners=True,
                  corner_radius=3)
        self.color("fill", self.t.line)
        step = 6
        for gy in range(int(y + step), int(y + height - 2), step):  # dot grid
            for gx in range(int(self.MARGIN + step), int(self.MARGIN + self.epw - 2), step):
                self.rect(gx, gy, 0.35, 0.35, style="F")
        self.set_y(y + height + 1)

    def _b_table(self, b: Block):
        rows = min(b.count or 8, 25)
        row_h, head_h = 9, 9
        rows = min(rows, int((self.full_body_height() - head_h - 10) / row_h))
        self.ensure(head_h + min(rows, 3) * row_h + 6)
        self._label(b.text)
        col_w = self.epw / len(b.columns)
        self._table_head(b.columns, col_w, head_h)
        self.color("draw", self.t.line)
        self.set_line_width(0.25)
        for r in range(rows):
            if self.get_y() + row_h > self.body_bottom():
                self.add_page()
                self._table_head(b.columns, col_w, head_h)
            y = self.get_y()
            if r % 2:
                self.color("fill", self.t.soft)
                self.rect(self.MARGIN, y, self.epw, row_h, style="F")
            for c in range(len(b.columns)):
                self.rect(self.MARGIN + c * col_w, y, col_w, row_h, style="D")
            self.set_y(y + row_h)

    def _table_head(self, columns, col_w, head_h):
        y = self.get_y()
        self.color("fill", self.t.accent)
        self.rect(self.MARGIN, y, self.epw, head_h, style="F")
        self.set_text_color(255, 255, 255)
        for c, name in enumerate(columns):
            self.fit_cell(self.MARGIN + c * col_w, y, col_w, head_h, name, "S", 9.5, "C")
        self.set_y(y + head_h)

    def fit_cell(self, x: float, y: float, w: float, h: float, text: str, fam: str,
                 size: float, align: str = "L", min_size: float = 6.5):
        """Text inside a fixed box: shrink the font, then wrap to two lines, then cut."""
        text = self.text_safe(text)
        inner = w - 2
        while size > min_size:
            self.font(fam, size)
            if self.get_string_width(text) <= inner:
                break
            size -= 0.5
        self.font(fam, size)
        if self.get_string_width(text) <= inner:
            self.set_xy(x, y)
            self.cell(w, h, text, align=align)
            return
        lines = self.multi_cell(inner, size * 0.42, text, dry_run=True, output="LINES")
        if len(lines) > 2:
            lines = [lines[0], lines[1].rstrip()[:-1] + "."]
        line_h = size * 0.42
        top = y + (h - line_h * len(lines)) / 2
        for i, line in enumerate(lines):
            self.set_xy(x + 1, top + i * line_h)
            self.cell(inner, line_h, line, align=align)

    def _b_tracker(self, b: Block):
        days = b.count if b.count in range(5, 32) else 7
        label_w = 46 if days > 10 else 60
        cell = (self.epw - label_w) / days
        row_h = 7.5 if days > 10 else 9
        items = b.items[: int((self.full_body_height() - 20) / row_h)]
        self.ensure(8 + row_h * min(len(items), 4) + 6)
        self._label(b.text)
        y = self.get_y()
        self.font("S", 7 if days > 10 else 9)
        self.color("text", self.t.accent)
        names = ["M", "T", "W", "T", "F", "S", "S"] if days == 7 else [str(d) for d in range(1, days + 1)]
        for d, name in enumerate(names):
            self.set_xy(self.MARGIN + label_w + d * cell, y)
            self.cell(cell, 6, name, align="C")
        self.set_y(y + 6.5)
        self.set_line_width(0.25)
        for i, item in enumerate(items):
            if self.get_y() + row_h > self.body_bottom():
                self.add_page()
            y = self.get_y()
            if i % 2 == 0:
                self.color("fill", self.t.soft)
                self.rect(self.MARGIN, y, self.epw, row_h, style="F")
            self.color("text", self.t.ink)
            self.fit_cell(self.MARGIN + 1, y, label_w - 2, row_h, item, "Body",
                          9 if days > 10 else 10)
            self.color("draw", self.t.line)
            box = min(cell, row_h) * 0.62
            for d in range(days):
                cx = self.MARGIN + label_w + d * cell + (cell - box) / 2
                self.rect(cx, y + (row_h - box) / 2, box, box, style="D", round_corners=True,
                          corner_radius=0.6)
            self.set_y(y + row_h)

    def _b_calendar(self, b: Block):
        head = 7
        avail = self.full_body_height() - 16
        cell_h = max(14, min(24, (avail - head) / 6))
        self.ensure(head + 6 * cell_h + 8)
        self._label(b.text or "Month: ____________________")
        col_w = self.epw / 7
        days = (["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] if self.week_starts_sunday
                else ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        y = self.get_y()
        self.color("fill", self.t.accent)
        self.rect(self.MARGIN, y, self.epw, head, style="F")
        self.font("S", 9)
        self.set_text_color(255, 255, 255)
        for i, d in enumerate(days):
            self.set_xy(self.MARGIN + i * col_w, y)
            self.cell(col_w, head, d, align="C")
        y += head
        self.color("draw", self.t.line)
        self.set_line_width(0.25)
        for r in range(6):
            for c in range(7):
                self.rect(self.MARGIN + c * col_w, y + r * cell_h, col_w, cell_h, style="D")
                self.color("fill", self.t.soft)
                self.rect(self.MARGIN + c * col_w + 1.2, y + r * cell_h + 1.2, 6, 5, style="F")
        self.set_y(y + 6 * cell_h + 2)

    def _b_quote(self, b: Block):
        self.ensure(18)
        y = self.get_y()
        self.font("Body", 12, "I")
        self.color("text", self.t.accent)
        self.set_x(self.MARGIN + 7)
        self.multi_cell(self.epw - 10, 6.4, self.text_safe(b.text), new_x=XPos.LMARGIN,
                        new_y=YPos.NEXT)
        self.color("fill", self.t.accent2)
        self.rect(self.MARGIN, y, 1.6, max(6.4, self.get_y() - y), style="F")
        self.ln(1)

    def _b_cards(self, b: Block):
        """Cut-out cards in two columns (activity cards, coupons). 'Title: detail' items get a
        bold title line."""
        gap, cols = 4, 2
        rows = -(-len(b.items) // cols)
        self._label(b.text)
        avail = self.body_bottom() - self.get_y() - 2
        card_h = min(48, (avail + gap) / rows - gap)
        if card_h < 26:                     # too many cards for the space: fit_blocks trims them
            card_h = 26
        card_w = (self.epw - gap) / cols
        y0 = self.get_y()
        for i, item in enumerate(b.items):
            x = self.MARGIN + (i % cols) * (card_w + gap)
            y = y0 + (i // cols) * (card_h + gap)
            if y + card_h > self.body_bottom():
                self.add_page()
                y0 = self.get_y() - (i // cols) * (card_h + gap)
                y = self.get_y()
            self.color("draw", self.t.line)
            self.set_line_width(0.3)
            self.set_dash_pattern(dash=1.6, gap=1.2)
            self.rect(x, y, card_w, card_h, style="D", round_corners=True, corner_radius=2.5)
            self.set_dash_pattern()
            title, _, detail = item.partition(": ") if ": " in item[:60] else ("", "", item)
            self.set_xy(x + 4, y + 4)
            if title:
                self.font("S", 11)
                self.color("text", self.t.accent)
                self.multi_cell(card_w - 8, 5.6, self.text_safe(title), align="C",
                                new_x=XPos.LEFT, new_y=YPos.NEXT)
                self.ln(1)
                self.set_x(x + 4)
            self.font("Body", 9.5)
            self.color("text", self.t.ink)
            self.multi_cell(card_w - 8, 4.8, self.text_safe(detail), align="C",
                            new_x=XPos.LEFT, new_y=YPos.NEXT)
        self.set_y(y0 + rows * (card_h + gap))

    def _b_certificate(self, b: Block):
        """A framed certificate filling the rest of the page: award line, name, date, signature."""
        y = self.get_y() + 2
        h = self.body_bottom() - y - 4
        if h < 120:
            self.add_page()
            y = self.get_y() + 2
            h = self.body_bottom() - y - 4
        self.color("draw", self.t.accent)
        self.set_line_width(1.2)
        self.rect(self.MARGIN, y, self.epw, h, style="D", round_corners=True, corner_radius=4)
        self.color("draw", self.t.accent2)
        self.set_line_width(0.4)
        self.rect(self.MARGIN + 4, y + 4, self.epw - 8, h - 8, style="D", round_corners=True,
                  corner_radius=3)
        inner = self.epw - 30
        self.set_xy(self.MARGIN + 15, y + h * 0.16)
        self.font("H", 36)
        self.color("text", self.t.accent)
        self.multi_cell(inner, 16, "Certificate", align="C", new_x=XPos.LEFT, new_y=YPos.NEXT)
        self.font("S", 14)
        self.multi_cell(inner, 8, "of Completion", align="C", new_x=XPos.LEFT, new_y=YPos.NEXT)
        self.ln(12)
        self.set_x(self.MARGIN + 15)            # ln() resets x to the page margin
        self.font("Body", 11)
        self.color("text", self.t.ink)
        self.multi_cell(inner, 6, "This certificate is proudly presented to", align="C",
                        new_x=XPos.LEFT, new_y=YPos.NEXT)
        self.ln(16)
        self.color("draw", self.t.line)
        self.set_line_width(0.4)
        yy = self.get_y()
        self.line(self.MARGIN + 30, yy, self.MARGIN + self.epw - 30, yy)
        self.ln(10)
        self.set_x(self.MARGIN + 15)
        self.font("Body", 12.5, "I")
        self.color("text", self.t.ink)
        self.multi_cell(inner, 7, self.text_safe(b.text), align="C",
                        new_x=XPos.LEFT, new_y=YPos.NEXT)
        yy = y + h - 34
        half = (self.epw - 50) / 2
        for k, label in enumerate(("Date", "Signed")):
            x = self.MARGIN + 18 + k * (half + 14)
            self.line(x, yy, x + half, yy)
            self.set_xy(x, yy + 2)
            self.font("Body", 9.5)
            self.cell(half, 5, label, align="C")
        self.set_y(y + h + 2)

    _COURT_ASPECT = (10.97 * (COURT_X[1] - COURT_X[0])) / (23.77 * (COURT_Y[1] - COURT_Y[0]))

    def _b_court(self, b: Block):
        """1-3 top-down doubles courts side by side: players (us = accent, them = grey),
        ball, shots (solid arrows), movement (dashed arrows), zones and small notes.
        count = drawing height in 4 mm steps (default 24 = 96 mm)."""
        panels = parse_court(b.items)
        self._label(b.text)
        gap = 6
        slot_w = (self.epw - gap * (len(panels) - 1)) / len(panels)
        has_title = any(c[0] == "title" for p in panels for c in p)
        cap_h = 7 if has_title else 0
        h = min((b.count or 24) * 4, slot_w / self._COURT_ASPECT)
        self.ensure(h + cap_h + 2)
        y0 = self.get_y()
        for i, panel in enumerate(panels):
            w = h * self._COURT_ASPECT
            x0 = self.MARGIN + i * (slot_w + gap) + (slot_w - w) / 2
            title = next((c[2] for c in panel if c[0] == "title"), "")
            if title:
                self.font("S", 10)
                self.color("text", self.t.accent)
                self.set_xy(self.MARGIN + i * (slot_w + gap), y0)
                self.cell(slot_w, 6, self.text_safe(title), align="C")
            self._court(panel, x0, y0 + cap_h, w, h)
        self.set_y(y0 + cap_h + h + 1)

    def _court(self, panel: list[tuple], x0: float, y0: float, w: float, h: float):
        sx = w / (COURT_X[1] - COURT_X[0])
        sy = h / (COURT_Y[1] - COURT_Y[0])
        px = lambda x: x0 + (x - COURT_X[0]) * sx
        py = lambda y: y0 + (COURT_Y[1] - y) * sy          # y = 0 (our baseline) at the bottom
        svc = 6.40 / 23.77 * 100                            # service line distance from the net
        alley = 1.37 / 10.97 * 100
        # surface + lines
        self.color("fill", self.t.soft)
        self.rect(x0, y0, w, h, style="F", round_corners=True, corner_radius=2)
        with self.local_context(fill_opacity=0.30):         # playing surface
            self.color("fill", self.t.accent)
            self.rect(px(0), py(100), 100 * sx, 100 * sy, style="F")
        for name, n, _ in panel:                            # zones under the lines
            if name == "zone":
                with self.local_context(fill_opacity=0.45):
                    self.color("fill", self.t.accent2)
                    self.rect(px(min(n[0], n[2])), py(max(n[1], n[3])), abs(n[2] - n[0]) * sx,
                              abs(n[3] - n[1]) * sy, style="F")
        self.color("draw", "#FFFFFF")
        self.set_line_width(0.6)
        self.rect(px(0), py(100), 100 * sx, 100 * sy)
        self.set_line_width(0.45)
        for x in (alley, 100 - alley):
            self.line(px(x), py(0), px(x), py(100))
        for y in (50 - svc, 50 + svc):
            self.line(px(alley), py(y), px(100 - alley), py(y))
        self.line(px(50), py(50 - svc), px(50), py(50 + svc))
        for y in (0, 100):                                  # centre marks
            self.line(px(50), py(y), px(50), py(y + (2 if y == 0 else -2)))
        self.color("draw", self.t.ink)
        self.set_line_width(0.8)
        self.line(px(-4), py(50), px(104), py(50))          # net + posts
        self.color("fill", self.t.ink)
        for x in (-4, 104):
            self.ellipse(px(x) - 0.7, py(50) - 0.7, 1.4, 1.4, style="F")
        # arrows, then players/ball on top, then notes
        for name, n, _ in panel:
            if name in ("shot", "move"):
                self._arrow(px(n[0]), py(n[1]), px(n[2]), py(n[3]), dashed=name == "move")
        r = max(2.6, min(4.2, w * 0.068))
        for name, n, text in panel:
            if name in ("us", "them"):
                self.color("fill", self.t.accent if name == "us" else "#8A8F98")
                self.color("draw", "#FFFFFF")
                self.set_line_width(0.4)
                self.ellipse(px(n[0]) - r, py(n[1]) - r, 2 * r, 2 * r, style="DF")
                if text:
                    self.font("Body", r * 2.3, "B")
                    self.color("text", "#FFFFFF")
                    self.set_xy(px(n[0]) - r, py(n[1]) - r)
                    self.cell(2 * r, 2 * r, self.text_safe(text), align="C")
            elif name == "ball":
                self.color("fill", "#D9E84A")
                self.color("draw", self.t.ink)
                self.set_line_width(0.25)
                self.ellipse(px(n[0]) - 1.3, py(n[1]) - 1.3, 2.6, 2.6, style="DF")
        for name, n, text in panel:
            if name == "note":
                self.font("Body", 8, "B")
                self.color("text", self.t.ink)
                tw = self.get_string_width(self.text_safe(text)) + 2
                x = min(max(px(n[0]) - tw / 2, x0 + 0.5), x0 + w - tw - 0.5)
                with self.local_context(fill_opacity=0.85):
                    self.color("fill", "#FFFFFF")
                    self.rect(x, py(n[1]) - 2, tw, 4, style="F", round_corners=True,
                              corner_radius=1)
                self.set_xy(x, py(n[1]) - 2)
                self.cell(tw, 4, self.text_safe(text), align="C")

    def _arrow(self, x1: float, y1: float, x2: float, y2: float, dashed: bool):
        import math
        self.color("draw", self.t.accent if dashed else self.t.accent2)
        self.color("fill", self.t.accent if dashed else self.t.accent2)
        self.set_line_width(0.45 if dashed else 0.6)
        ang = math.atan2(y2 - y1, x2 - x1)
        head = 2.6
        bx, by = x2 - head * math.cos(ang), y2 - head * math.sin(ang)
        if dashed:
            self.set_dash_pattern(dash=1.4, gap=1.0)
        self.line(x1, y1, bx, by)
        self.set_dash_pattern()
        spread = 0.45
        pts = [(x2, y2),
               (x2 - head * math.cos(ang - spread), y2 - head * math.sin(ang - spread)),
               (x2 - head * math.cos(ang + spread), y2 - head * math.sin(ang + spread))]
        self.polygon(pts, style="F")


def render_toc(pdf: ProductPDF, outline):
    pdf.chrome = False
    pdf.set_y(28)
    pdf.font("H", 26)
    pdf.color("text", pdf.t.accent)
    pdf.cell(pdf.epw, 12, "Contents", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(6)
    for section in outline:
        pdf.font("Body", 11)
        pdf.color("text", pdf.t.ink)
        name, page = pdf.text_safe(section.name), str(section.page_number)
        dots_w = pdf.epw - pdf.get_string_width(name) - pdf.get_string_width(page) - 4
        dots = "." * max(3, int(dots_w / max(0.1, pdf.get_string_width("."))))
        pdf.cell(pdf.epw, 8, f"{name} {dots} {page}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def build_pdf(path: Path, t: Theme, page_format: str, brand: str, brief: ProductBrief,
              pages: list[tuple[str, list[Block]]], art: Image.Image) -> int:
    pdf = ProductPDF(t, page_format, brand, brief.title)
    tagline = f"For {brief.audience}" if len(brief.audience) < 80 else ""
    pdf.cover(brief.title, brief.subtitle, tagline, art)
    toc = brief.product_type in TEXT_HEAVY and len(pages) >= 3
    if toc:
        try:
            pdf.add_page()          # the ToC is drawn on the page that is current here
            pdf.insert_toc_placeholder(render_toc, pages=1)
            pdf.fresh_page = True   # fpdf already broke to the first content page
        except Exception:
            toc = False
    for title, blocks in pages:
        pdf.content_page(title, blocks, section=toc,
                         fill_rest=brief.product_type not in TEXT_HEAVY)
    who = brand or "the seller"
    year = utcnow().year
    pdf.closing_page("Thank you!", [
        "Thank you for your purchase!",
        f"This file includes {'A4' if page_format == 'A4' else 'US Letter'} pages. Print at 100% "
        "(\"actual size\") on regular or heavier paper. Print single pages as often as you need.",
        "Personal use licence",
        f"You may print and use this product for your own personal use. You may not resell, "
        f"share, or distribute the files, in whole or in part. (c) {year} {who}. All rights reserved.",
    ])
    pdf.output(str(path))
    return pdf.page_no()


_MONTHS = re.compile(r"\b(january|february|march|april|may|june|july|august|september|october|"
                     r"november|december)\b", re.I)


def tidy_blocks(title: str, blocks: list[Block]) -> list[Block]:
    """Code-side fixes for common model slips: a heading that repeats the page title, and dated
    calendars in an undated printable."""
    norm = lambda s: re.sub(r"\W+", " ", s).strip().lower()
    out = []
    for b in blocks:
        if b.type == "heading" and norm(b.text) == norm(title) and len(blocks) > 1:
            continue
        if b.type == "calendar" and _MONTHS.search(b.text):
            b = b.model_copy(update={"text": "Month: ____________________"})
        out.append(b)
    return out or blocks


def _pages_used(t: Theme, page_format: str, title: str, blocks: list[Block]) -> int:
    pdf = ProductPDF(t, page_format, "", "fit")
    pdf.content_page(title, blocks)
    return pdf.page_no()


_SHRINK = {"lines": (8, 3), "notes": (6, 3), "table": (8, 3),   # type: (default count, minimum)
           "court": (24, 15)}


def fit_blocks(t: Theme, page_format: str, title: str, blocks: list[Block]) -> list[Block]:
    """Make one planned page fit on one printed page: first shorten fill-in areas (writing
    lines, notes, table rows), then trim card/tracker/list rows, then drop trailing blocks.
    A spill-over page padded with 'Notes' is filler a buyer notices."""
    blocks = list(blocks)
    for _ in range(60):
        if _pages_used(t, page_format, title, blocks) <= 1:
            return blocks
        best, best_size = None, 0
        for i, b in enumerate(blocks):
            if b.type in _SHRINK:
                default, low = _SHRINK[b.type]
                size = b.count or default
                if size > low and size > best_size:
                    best, best_size = i, size
        if best is not None:
            blocks[best] = blocks[best].model_copy(update={"count": max(_SHRINK[blocks[best].type][1],
                                                                        best_size - 2)})
            continue
        trim = next((i for i in range(len(blocks) - 1, -1, -1)
                     if blocks[i].type in ("cards", "tracker", "checklist", "bullets", "numbered")
                     and len(blocks[i].items) > 2), None)
        if trim is not None:
            blocks[trim] = blocks[trim].model_copy(update={"items": blocks[trim].items[:-1]})
        elif len(blocks) > 1:
            blocks.pop()
        else:
            return blocks
    return blocks


def render_previews(pdf_path: Path, out_dir: Path, max_pages: int = 8,
                    width_px: int = 1200) -> list[Path]:
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(pdf_path))
    paths = []
    try:
        for i in range(min(max_pages, len(doc))):
            page = doc[i]
            scale = width_px / page.get_width()
            img = page.render(scale=scale).to_pil().convert("RGB")
            p = out_dir / f"page-{i + 1:02d}.png"
            img.save(p, "PNG", optimize=True)
            paths.append(p)
            page.close()
    finally:
        doc.close()
    return paths


# --- the builder ------------------------------------------------------------------------------

_BLOCK_GUIDE = (
    "Block types (use ONLY these):\n"
    "- heading: text = a short section heading\n"
    "- text: text = a paragraph of 40-180 words\n"
    "- bullets / numbered / checklist: items = 3-12 short lines\n"
    "- quote: text = one short motivational or key sentence\n"
    "- lines: text = label/question, count = writing lines (3-20)\n"
    "- notes: text = label, count = box height in lines (4-20)\n"
    "- table: text = label, columns = 2-6 column headers, count = empty rows to fill (3-20)\n"
    "- tracker: text = label, items = rows of yes/no habits to TICK, count = days (7 or 31). "
    "Never for amounts (minutes, hours, money): use a table for those\n"
    "- calendar: text = label (an undated monthly grid)\n"
    "- cards: text = label, items = 2-12 cut-out cards, each 'Short title: one sentence' "
    "(activity cards, coupons, conversation starters)\n"
    "- certificate: text = what was achieved (one sentence); fills the rest of the page\n"
    "- court: ONLY for tennis products - a court diagram; items = commands 'us X Y label', "
    "'them X Y label', 'ball X Y', 'shot X1 Y1 X2 Y2', 'move X1 Y1 X2 Y2', 'zone X1 Y1 X2 Y2', "
    "'note X Y text', 'title text', '---' = next panel (max 3). X 0-100 across, Y 0 = our "
    "baseline, 50 = net, 100 = far baseline\n")


def _page_guidance(ptype: str) -> str:
    if ptype in TEXT_HEAVY:
        return ("This is a reading product: use 3-7 blocks, mostly heading/text/bullets/numbered/"
                "quote, 250-450 words of concrete, practical, specific advice. You may end with "
                "one short fill-in block (lines, checklist or table) as an exercise.")
    return ("This is a PRINTABLE to fill in by hand: use 2-5 blocks, mostly fill-in blocks "
            "(checklist, lines, table, tracker, calendar, notes). At most one short text block. "
            "Fill the page usefully. Keep it UNDATED and reusable: no specific years, months "
            "or dates, and no example amounts the buyer would have to cross out. Keep table "
            "column headers short (1-2 words).")


class ProductSettings(StrictModel):
    formats: list[Literal["A4", "Letter"]] = ["A4", "Letter"]
    preview_pages: int = Field(default=6, ge=1, le=12)
    write_listing: bool = True
    max_content_pages: int = Field(default=12, ge=3, le=24)   # code cap on the model's brief


class ProductTool(Tool):
    """Builds a complete digital product (PDFs + previews + mockups + listing + bundle)."""
    name = "product_builder"
    Params = ProductBrief            # what a model may propose
    CallParams = WrittenProduct      # what a direct call may also pass (finished pages/listing)
    Settings = ProductSettings
    needs_model = True

    def run(self, brief: ProductBrief, ctx: ToolContext) -> ToolOutput:
        c = ctx.company
        if brief.pages > self.settings.max_content_pages:
            brief = brief.model_copy(update={"pages": self.settings.max_content_pages})
        t = theme(brief.theme)
        brand = brand_name(c)
        product_id = new_id("prod")
        folder = c.workspace.version_dir(ctx.project_id, "products", brief.title)
        warnings: list[str] = []
        ctx.log(ACTOR, "Building product", product=product_id, title=brief.title,
                type=brief.product_type, pages=brief.pages)

        background = ((f"Background from earlier work:\n{ctx.context[:2500]}\n\n") if ctx.context else "")
        head = (f"Product: \"{brief.title}\"" + (f" - {brief.subtitle}" if brief.subtitle else "")
                + f"\nType: {brief.product_type}. Audience: {brief.audience}.\n"
                + (f"Must contain: {brief.content_notes}\n" if brief.content_notes else ""))

        written = getattr(brief, "written_pages", [])[:self.settings.max_content_pages]
        if written:
            plans = [PagePlan(title=w.title, purpose="finished page supplied by the caller")
                     for w in written]
        elif brief.page_plan:
            plans = brief.page_plan[:self.settings.max_content_pages]
        else:
            outline = ctx.generate(
                f"{background}{head}\nPlan exactly {brief.pages} content pages for this product "
                f"(the cover and licence page are added automatically - do not include them). "
                f"Each page needs a short title and its purpose. Make the pages build a complete, "
                f"useful product in a sensible order.",
                ProductOutline, system="You design best-selling digital products. Be specific.")
            plans = outline.pages[:brief.pages]

        pages: list[tuple[str, list[Block]]] = []
        for i, plan in enumerate(plans, start=1):
            prompt = (
                f"{head}\nPage {i} of {len(plans)}: \"{plan.title}\" - {plan.purpose}\n"
                f"Pages in this product: {', '.join(p.title for p in plans)}\n\n"
                f"{_BLOCK_GUIDE}\n{_page_guidance(brief.product_type)}\n"
                "Write real, specific content: no placeholders such as 'Lorem ipsum' or "
                "'[insert]'. Do not repeat the page title as a heading. Plain text only: no "
                "markdown, no web addresses, no brand, app or organisation names. Everything "
                "must fit on ONE printed page.")
            blocks = list(written[i - 1].blocks) if written else None
            # a model crash (e.g. a repeat loop) gets one retry, then a safe notes page
            for attempt, temp in enumerate(() if written else (0.5, 0.8)):
                try:
                    blocks = ctx.generate(prompt, PageContent, temperature=temp,
                                          system="You write and design pages for digital "
                                                 "products.").blocks
                    break
                except StructuredOutputError as e:
                    ctx.log(ACTOR, "Page fallback", product=product_id, page=i,
                            errors=e.errors[-1:])
                    break
                except ModelClientError as e:
                    ctx.log(ACTOR, "Page model error", product=product_id, page=i,
                            attempt=attempt + 1, error=str(e)[:200])
            if blocks is None:
                warnings.append(f"page {i} '{plan.title}': model output invalid, used a notes page")
                blocks = [Block(type="notes", text=plan.purpose[:200], count=18)]
            blocks = tidy_blocks(plan.title, blocks)
            for fmt in self.settings.formats:
                fitted = fit_blocks(t, fmt, plan.title, blocks)
                if len(fitted) < len(blocks):
                    warnings.append(f"page {i} '{plan.title}': dropped {len(blocks) - len(fitted)} "
                                    f"block(s) to fit one page")
                blocks = fitted
            pages.append((plan.title, blocks))
            if any(_PLACEHOLDER.search(b.text + " ".join(b.items)) for b in blocks):
                warnings.append(f"page {i} '{plan.title}' contains placeholder text - review it")

        art = graphics.cover_art(t, (1240, 1754), seed=len(brief.title))
        pdfs, page_count = [], 0
        for fmt in self.settings.formats:
            path = folder / f"{slugify(brief.title, 50)}-{fmt}.pdf"
            page_count = build_pdf(path, t, fmt, brand, brief, pages, art)
            pdfs.append((fmt, path))

        preview_dir = folder / "previews"
        preview_dir.mkdir(exist_ok=True)
        previews = render_previews(pdfs[0][1], preview_dir, self.settings.preview_pages)
        preview_imgs = [Image.open(p).convert("RGB") for p in previews]
        mockups = {
            "cover": graphics.paper_mockup(preview_imgs[:3], t, seed=11),
            "tablet": graphics.tablet_mockup(preview_imgs[0], t, seed=12),
            "grid": graphics.pages_grid(preview_imgs[1:7] or preview_imgs, t,
                                        f"{page_count} pages - A4 + US Letter"
                                        if len(pdfs) > 1 else f"{page_count} pages", seed=13),
        }
        mockup_paths = {k: graphics.save_jpg(v, folder / "mockups" / f"mockup-{k}.jpg")
                        for k, v in mockups.items()}

        listing = None
        if getattr(brief, "listing", None):
            listing = brief.listing
        elif self.settings.write_listing:
            listing = self._listing(ctx, brief, plans, page_count, warnings)
        listing_md = folder / "listing.md"
        listing_md.write_text(self._listing_markdown(brief, listing, page_count, len(pdfs)),
                              encoding="utf-8")

        bundle = folder / f"{slugify(brief.title, 50)}-download.zip"
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
            for fmt, path in pdfs:
                z.write(path, path.name)
            z.writestr("README.txt", f"{brief.title}\n\nPrint at 100% (actual size). "
                                     f"Personal use only - do not share or resell.\n")

        meta = {"product_title": brief.title, "product_type": brief.product_type,
                "theme": brief.theme, "audience": brief.audience, "pages": page_count,
                "warnings": warnings}
        reg = lambda path, kind, title, extra=None, thumb=None: c.assets.register(
            path, kind, title, "product_builder", ctx.project_id, ctx.task_id, product_id,
            {**meta, **(extra or {})}, thumb_from=thumb)
        assets = [reg(path, "product_pdf", f"{brief.title} ({fmt})", {"format": fmt},
                      thumb=previews[0]) for fmt, path in pdfs]
        assets.append(reg(bundle, "bundle", f"{brief.title} - download bundle", thumb=previews[0]))
        assets += [reg(p, "preview", f"{brief.title} - page {i + 1}", {"page": i + 1})
                   for i, p in enumerate(previews)]
        assets += [reg(p, "mockup", f"{brief.title} - {k} mockup", {"style": k})
                   for k, p in mockup_paths.items()]
        assets.append(reg(listing_md, "listing", f"{brief.title} - listing",
                          {"listing": listing.model_dump() if listing else None}))

        ctx.log(ACTOR, "Built product", product=product_id, pages=page_count,
                files=len(assets), warnings=warnings)
        summary = {"product_id": product_id, "title": brief.title, "pages": page_count,
                   "formats": [f for f, _ in pdfs], "warnings": warnings,
                   "listing_title": listing.title if listing else None,
                   "suggested_price_eur": listing.price_eur if listing else None}
        text = (f"Product '{brief.title}' built: {page_count} pages, formats "
                f"{', '.join(f for f, _ in pdfs)}. Pages: {', '.join(p.title for p in plans)}.\n"
                + ("Listing title: " + listing.title + "\n" if listing else "")
                + ("Warnings: " + "; ".join(warnings) + "\n" if warnings else "")
                + "Files:\n" + "\n".join(c.assets.summary_lines(assets[:3] + assets[-4:])))
        return ToolOutput(summary=summary, assets=assets, text=text)

    def _listing(self, ctx, brief, plans, page_count, warnings) -> ProductListing | None:
        try:
            return ctx.generate(
                f"Write the marketplace listing (e.g. Etsy) for this digital download.\n"
                f"Product: {brief.title} - {brief.subtitle}\nType: {brief.product_type}, "
                f"{page_count} pages, instant download PDF (A4 + US Letter).\n"
                f"Audience: {brief.audience}\nPages: {', '.join(p.title for p in plans)}\n"
                f"Title: max 140 characters, lead with what buyers search for. Description: what "
                f"is included, who it is for, how it works (instant download, print at home), "
                f"personal-use note. 13 tags, each at most 20 characters. Suggest a realistic "
                f"price in EUR.", ProductListing,
                system="You write high-converting, honest marketplace listings. Never invent "
                       "reviews, sales numbers or features the product does not have.")
        except StructuredOutputError:
            warnings.append("listing could not be generated - write it manually")
            return None

    @staticmethod
    def _listing_markdown(brief, listing: ProductListing | None, pages: int, n_formats: int) -> str:
        if listing is None:
            return f"# {brief.title}\n\n(No listing generated.)\n"
        return (f"# {listing.title}\n\n**Suggested price:** EUR {listing.price_eur:.2f}\n\n"
                f"**Tags ({len(listing.tags)}):** {', '.join(listing.tags)}\n\n"
                f"## Description\n\n{listing.description}\n\n---\n"
                f"Product: {brief.title} | {pages} pages | "
                f"{'A4 + US Letter' if n_formats > 1 else 'PDF'} | theme {brief.theme}\n")
