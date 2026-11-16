#!/usr/bin/env python3
"""
Export a printed-style Quran Mushaf PDF (Indopak 15 lines - Qudratullah)
from the QUL (qul.tarteel.ai) resources.

Required files (QUL downloads):
  * qudratullah-indopak-15-lines.db   -> page/line layout       (mushaf-layout/12)
  * indopak-nastaleeq.db              -> word-by-word text      (script)
  * indopak-nastaleeq_font.ttf        -> the matching font
  * quran-metadata-surah-name.sqlite  -> surah names

Install:   pip install reportlab uharfbuzz fonttools
Run:       python export_mushaf_pdf.py                    # full 610 pages
           python export_mushaf_pdf.py --pages 1-5,602    # test a few pages

How it works
------------
* The font is a Nastaleeq font that relies on OpenType shaping (GSUB/GPOS), which
  ReportLab can't do. So every line is shaped with HarfBuzz, and the resulting
  glyph IDs + positions are drawn directly. For embedding, a private copy of the
  font is made where glyph N is reachable at U+E000+N (shaping tables removed).
* Every line of the DB is placed on a fixed 15-line grid. Lines flagged
  `is_centered=0` are justified to the full text width (like a printed mushaf)
  by stretching word spaces and, if necessary, a slight horizontal glyph scale.
"""
import argparse
import os
import sqlite3
import sys
import tempfile

import uharfbuzz as hb
from fontTools.ttLib import TTFont
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont as RLTTFont
from reportlab.pdfgen import canvas

# --------------------------------------------------------------------------- #
# Configuration (edit freely)
# --------------------------------------------------------------------------- #
PAGE_W, PAGE_H = 148 * mm, 210 * mm        # A5-ish. Change for other sizes.
MARGIN_X = 13 * mm                          # left/right margin to text block
MARGIN_TOP = 20 * mm                        # top of page -> first line
MARGIN_BOTTOM = 18 * mm                     # last line -> bottom of page
LINES_PER_PAGE = 15

INK = HexColor("#111111")
FRAME_COLOR = HexColor("#1d4d3a")           # dark green frame
BANNER_FILL = HexColor("#efe6c8")
BANNER_STROKE = HexColor("#8a6d1f")
HEADER_COLOR = HexColor("#1d4d3a")

# Horizontal glyph scaling limits used when justifying lines
MIN_HSCALE, MAX_HSCALE = 0.90, 1.12
# Maximum factor by which a word space may be widened before scaling glyphs
MAX_SPACE_STRETCH = 2.2

# Standard juz (para) starting points: (surah, ayah)
JUZ_START = [
    (1, 1), (2, 142), (2, 253), (3, 93), (4, 24), (4, 148), (5, 82), (6, 111),
    (7, 88), (8, 41), (9, 93), (11, 6), (12, 53), (15, 1), (17, 1), (18, 75),
    (21, 1), (23, 1), (25, 21), (27, 56), (29, 1), (33, 31), (36, 28), (39, 32),
    (41, 47), (46, 1), (51, 31), (58, 1), (67, 1), (78, 1),
]

# --------------------------------------------------------------------------- #


def arabic_digits(n):
    return "".join(chr(0x6F0 + int(d)) for d in str(n))


class Shaper:
    """HarfBuzz shaping + glyph-ID -> PUA remapped font for ReportLab."""

    PUA = 0xE000

    def __init__(self, ttf_path, rl_font_name="MushafFont"):
        self.ttf_path = ttf_path
        with open(ttf_path, "rb") as fh:
            self.data = fh.read()
        self.hb_font = hb.Font(hb.Face(self.data))
        tt = TTFont(ttf_path)
        self.upm = tt["head"].unitsPerEm
        self.order = tt.getGlyphOrder()
        self.space_gid = tt.getGlyphID("space")
        self.cmap = tt.getBestCmap()

        # --- private font: glyph N  ->  U+E000+N, no GSUB/GPOS/GDEF ---------
        for tag in ("GSUB", "GPOS", "GDEF"):
            if tag in tt:
                del tt[tag]
        cmap_table = tt["cmap"]
        keep = None
        for st in cmap_table.tables:
            if st.platformID == 3 and st.format in (4, 12):
                keep = st
                break
        if keep is None:
            raise RuntimeError("font has no Windows Unicode cmap")
        keep.platEncID = 1
        keep.format = 4
        keep.cmap = {self.PUA + i: name for i, name in enumerate(self.order)}
        cmap_table.tables = [keep]
        # Avoid hinting programs that ReportLab can't use anyway
        for tag in ("fpgm", "prep", "cvt "):
            if tag in tt:
                del tt[tag]
        self._tmp = os.path.join(tempfile.gettempdir(), "mushaf_remapped_font.ttf")
        tt.save(self._tmp)
        pdfmetrics.registerFont(RLTTFont(rl_font_name, self._tmp))
        self.rl_name = rl_font_name

    def shape(self, text, direction="rtl"):
        """Return list of (gid, x_off, y_off, x_adv) in font units, left->right."""
        buf = hb.Buffer()
        buf.add_str(text)
        buf.direction = direction
        buf.script = "Arab"
        buf.language = "ur"
        hb.shape(self.hb_font, buf, {"kern": True, "liga": True, "calt": True,
                                     "mark": True, "mkmk": True})
        return [(i.codepoint, p.x_offset, p.y_offset, p.x_advance)
                for i, p in zip(buf.glyph_infos, buf.glyph_positions)]

    def shape_number(self, n):
        """Digits always read left-to-right, even inside Arabic text."""
        return self.shape(arabic_digits(n), direction="ltr")

    def shape_label(self, word, n):
        """'<word> <number>' laid out right-to-left: number sits left of the word."""
        space = self.shape(" ", direction="rtl")
        return self.shape_number(n) + space + self.shape(word)

    @staticmethod
    def natural_width(glyphs):
        return sum(g[3] for g in glyphs)


class MushafDB:
    def __init__(self, layout_db, words_db, meta_db):
        self.layout = sqlite3.connect(layout_db)
        self.words = sqlite3.connect(words_db)
        self.meta = sqlite3.connect(meta_db)
        self.info = self.layout.execute(
            "SELECT name, number_of_pages, lines_per_page FROM info").fetchone()
        self.n_pages = self.info[1]
        self.surah_names = {
            r[0]: r[1] for r in self.meta.execute("SELECT id, name_arabic FROM chapters")}
        self.word_cache = {}
        for wid, s, a, t in self.words.execute("SELECT id, surah, ayah, text FROM words"):
            self.word_cache[wid] = (s, a, t)
        self.bismillah = " ".join(self.word_cache[i][2] for i in range(1, 5))

    def page_lines(self, page):
        return self.layout.execute(
            "SELECT line_number, line_type, is_centered, first_word_id, last_word_id, surah_number "
            "FROM pages WHERE page_number=? ORDER BY line_number", (page,)).fetchall()

    def line_text(self, first, last):
        return " ".join(self.word_cache[i][2] for i in range(int(first), int(last) + 1))

    def first_ayah_on_page(self, page):
        for ln, lt, c, f, l, s in self.page_lines(page):
            if lt == "ayah" and f != "":
                return self.word_cache[int(f)][:2]
        return None

    def surah_on_page(self, page):
        """Surah of the last ayah line on the page (what a printed header shows)."""
        surah = None
        for ln, lt, c, f, l, s in self.page_lines(page):
            if lt == "ayah" and f != "":
                surah = self.word_cache[int(f)][0]
            elif lt == "surah_name" and surah is None:
                surah = int(s)
        return surah


def juz_of(surah, ayah):
    j = 1
    for i, (s, a) in enumerate(JUZ_START, 1):
        if (surah, ayah) >= (s, a):
            j = i
    return j


class Exporter:
    def __init__(self, db, shaper, out_path):
        self.db = db
        self.sh = shaper
        self.c = canvas.Canvas(out_path, pagesize=(PAGE_W, PAGE_H))
        self.c.setTitle("Quran - Indopak 15 Lines (Qudratullah layout)")
        self.c.setAuthor("QUL / Tarteel resources")
        self.text_w = PAGE_W - 2 * MARGIN_X
        self.pitch = (PAGE_H - MARGIN_TOP - MARGIN_BOTTOM) / LINES_PER_PAGE
        self.font_size = self._calibrate_font_size()
        print(f"font size: {self.font_size:.2f} pt, line pitch: {self.pitch/mm:.2f} mm")

    # ---- font size: chosen so the widest *justified* line fits ------------
    def _calibrate_font_size(self):
        widths = []
        for page in range(1, self.db.n_pages + 1):
            for ln, lt, cen, f, l, s in self.db.page_lines(page):
                if lt == "ayah" and not cen and f != "":
                    g = self.sh.shape(self.db.line_text(f, l))
                    widths.append(self.sh.natural_width(g) / self.sh.upm)
        widths.sort()
        # Use ~ the 99th percentile: the few widest lines are compressed slightly.
        ref = widths[int(len(widths) * 0.99) - 1]
        return self.text_w * 0.99 / (ref * MIN_HSCALE ** 0.5)

    # ---- drawing primitives ----------------------------------------------
    def draw_glyphs(self, glyphs, x_left, baseline, size, hscale=1.0,
                    space_extra=0.0, color=INK):
        """Draw shaped glyphs starting at x_left. space_extra in font units."""
        c = self.c
        k = size / self.sh.upm
        t = c.beginText()
        t.setFont(self.sh.rl_name, size)
        t.setHorizScale(hscale * 100)
        t.setFillColor(color)
        pen = 0.0
        for gid, xo, yo, adv in glyphs:
            x = x_left + (pen + xo) * k * hscale
            y = baseline + yo * k
            t.setTextOrigin(x, y)
            t.textOut(chr(Shaper.PUA + gid))
            pen += adv
            if gid == self.sh.space_gid:
                pen += space_extra
        c.drawText(t)

    def draw_line(self, text, baseline, centered, size=None, color=INK):
        size = size or self.font_size
        g = self.sh.shape(text)
        upm = self.sh.upm
        k = size / upm
        W = self.sh.natural_width(g) * k
        T = self.text_w
        n_spaces = sum(1 for x in g if x[0] == self.sh.space_gid)
        hscale, space_extra = 1.0, 0.0

        if centered:
            if W > T:                      # safety: never overflow
                hscale = T / W
                W = T
            x_left = MARGIN_X + (T - W) / 2
        else:
            x_left = MARGIN_X
            if W <= T:
                need = T - W
                # first widen word spaces (up to MAX_SPACE_STRETCH x), rest by hscale
                if n_spaces:
                    space_w = g_space = next(x[3] for x in g if x[0] == self.sh.space_gid) * k
                    max_extra = space_w * (MAX_SPACE_STRETCH - 1) * n_spaces
                    # use hscale for up to half the deficit, spaces take the rest
                    h_try = min(MAX_HSCALE, 1 + (need / W) * 0.5)
                    W2 = W * h_try
                    need2 = T - W2
                    if need2 <= max_extra * h_try:
                        hscale = h_try
                        space_extra = need2 / n_spaces / (k * hscale)
                    else:
                        hscale = min(MAX_HSCALE, T / (W + max_extra))
                        hscale = max(hscale, 1.0)
                        W2 = (W + max_extra) * hscale
                        space_extra = max(0.0, (T - W2) / n_spaces / (k * hscale)) + \
                            max_extra / n_spaces / k
                else:
                    hscale = min(T / W, 1.25)
                    x_left = MARGIN_X + (T - W * hscale) / 2
            else:
                hscale = T / W            # compress
        self.draw_glyphs(g, x_left, baseline, size, hscale, space_extra, color)

    def baseline_for_slot(self, slot):
        """slot: 0-based line slot from the top. Baseline sits ~38% above slot bottom."""
        top = PAGE_H - MARGIN_TOP - slot * self.pitch
        return top - self.pitch * 0.62

    # ---- decorations ------------------------------------------------------
    def draw_frame(self):
        c = self.c
        x0, x1 = MARGIN_X - 4 * mm, PAGE_W - MARGIN_X + 4 * mm
        y1 = PAGE_H - MARGIN_TOP + 2 * mm
        y0 = MARGIN_BOTTOM - 2 * mm
        c.setStrokeColor(FRAME_COLOR)
        c.setLineWidth(1.6)
        c.rect(x0, y0, x1 - x0, y1 - y0)
        c.setLineWidth(0.5)
        c.rect(x0 + 1.4 * mm, y0 + 1.4 * mm, x1 - x0 - 2.8 * mm, y1 - y0 - 2.8 * mm)

    def draw_surah_banner(self, slot, surah):
        c = self.c
        top = PAGE_H - MARGIN_TOP - slot * self.pitch
        h = self.pitch * 0.82
        y = top - self.pitch * 0.5 - h / 2
        x0 = MARGIN_X
        w = self.text_w
        c.setFillColor(BANNER_FILL)
        c.setStrokeColor(BANNER_STROKE)
        c.setLineWidth(1.2)
        c.roundRect(x0, y, w, h, 3 * mm, stroke=1, fill=1)
        c.setLineWidth(0.4)
        c.roundRect(x0 + 1.2 * mm, y + 1.2 * mm, w - 2.4 * mm, h - 2.4 * mm, 2.2 * mm,
                    stroke=1, fill=0)
        # small ornaments on both ends
        for cx in (x0 + 7 * mm, x0 + w - 7 * mm):
            c.setFillColor(BANNER_STROKE)
            c.circle(cx, y + h / 2, 1.6 * mm, stroke=0, fill=1)
            c.setFillColor(BANNER_FILL)
            c.circle(cx, y + h / 2, 0.8 * mm, stroke=0, fill=1)
        name = "سُوْرَةُ " + self.db.surah_names[surah]
        g = self.sh.shape(name)
        size = self.font_size * 0.9
        wdt = self.sh.natural_width(g) * size / self.sh.upm
        base = y + h / 2 - size * 0.30
        self.draw_glyphs(g, PAGE_W / 2 - wdt / 2, base, size, color=HexColor("#3b2a00"))

    def draw_header_footer(self, page):
        c = self.c
        fa = self.db.first_ayah_on_page(page)
        surah = self.db.surah_on_page(page)
        size = self.font_size * 0.62
        base = PAGE_H - MARGIN_TOP + 8 * mm
        if fa:
            juz = juz_of(*fa)
            g = self.sh.shape_label("جُزْءٌ", juz)
            self.draw_glyphs(g, PAGE_W - MARGIN_X - self.sh.natural_width(g) * size / self.sh.upm,
                             base, size, color=HEADER_COLOR)
        if surah:
            g = self.sh.shape("سُوْرَةُ " + self.db.surah_names[surah])
            self.draw_glyphs(g, MARGIN_X, base, size, color=HEADER_COLOR)
        # page number in a small ornament
        cx, cy = PAGE_W / 2, MARGIN_BOTTOM - 9 * mm
        c.setStrokeColor(FRAME_COLOR)
        c.setFillColor(Color(1, 1, 1))
        c.setLineWidth(0.8)
        c.circle(cx, cy, 4.2 * mm, stroke=1, fill=1)
        c.setLineWidth(0.3)
        c.circle(cx, cy, 3.5 * mm, stroke=1, fill=0)
        g = self.sh.shape_number(page)
        s = self.font_size * 0.55
        w = self.sh.natural_width(g) * s / self.sh.upm
        self.draw_glyphs(g, cx - w / 2, cy - s * 0.28, s, color=HEADER_COLOR)

    # ---- page --------------------------------------------------------------
    def draw_page(self, page):
        lines = self.db.page_lines(page)
        self.draw_frame()
        self.draw_header_footer(page)
        n = len(lines)
        # Pages 1-2 (8 lines) are vertically centred; a short last page is top aligned
        offset = (LINES_PER_PAGE - n) / 2 if page in (1, 2) and n < LINES_PER_PAGE else 0
        for ln, lt, cen, f, l, s in lines:
            slot = (ln - 1) + offset
            base = self.baseline_for_slot(slot)
            if lt == "surah_name":
                self.draw_surah_banner(slot, int(s))
            elif lt == "basmallah":
                self.draw_line(self.db.bismillah, base, True)
            elif lt == "ayah":
                self.draw_line(self.db.line_text(f, l), base, bool(cen))
        self.c.showPage()

    def export(self, pages):
        for i, p in enumerate(pages, 1):
            self.draw_page(p)
            if i % 50 == 0 or i == len(pages):
                print(f"  {i}/{len(pages)} pages")
        self.c.save()


def parse_pages(spec, total):
    if not spec:
        return list(range(1, total + 1))
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += range(int(a), int(b) + 1)
        else:
            out.append(int(part))
    return out


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=here, help="folder with the QUL files")
    ap.add_argument("--layout", default="qudratullah-indopak-15-lines.db")
    ap.add_argument("--words", default="indopak-nastaleeq.db")
    ap.add_argument("--font", default="indopak-nastaleeq_font.ttf")
    ap.add_argument("--meta", default="quran-metadata-surah-name.sqlite")
    ap.add_argument("--pages", default="", help="e.g. 1-5,602 (default: all)")
    ap.add_argument("-o", "--out", default="quran_indopak_15_lines.pdf")
    a = ap.parse_args()

    p = lambda n: n if os.path.isabs(n) else os.path.join(a.dir, n)
    db = MushafDB(p(a.layout), p(a.words), p(a.meta))
    sh = Shaper(p(a.font))
    ex = Exporter(db, sh, a.out)
    pages = parse_pages(a.pages, db.n_pages)
    ex.export(pages)
    print("saved:", a.out)


if __name__ == "__main__":
    sys.exit(main())
