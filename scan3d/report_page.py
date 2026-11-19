"""A measurement report's result drawn as one page, for a DICOM series beside its overview picture.

Research use only. The page carries only what the result holds, never a case name or patient
detail, and the same result always draws the same bytes: no clock, no PNG metadata. Its tables
match the panel's Copy table (web/src/lib/reports.ts landmarkTable and spineTable), column for column.
"""

from __future__ import annotations

from functools import cache
from io import BytesIO
import math

from PIL import Image, ImageDraw, ImageFont

from .reports import NOTICE

WIDTH = 1600
_MARGIN = 64
_CONTENT = WIDTH - 2 * _MARGIN
_PAD = 12
_INK, _MUTED, _WARNING, _RULE, _STRIPE = (26, 26, 26), (85, 85, 85), (180, 35, 24), (200, 200, 200), (242, 244, 245)
_WIDER = "Diameters read about 2 mm wider than the mask, as TotalSegmentator measures them."
# The second image of the series, by the picture the result holds.
_SECOND = {
    "cpr.png": "Image 2 of this series: the curved view along the aorta and its diameter profile.",
    "spine.png": "Image 2 of this series: the spine picture with the measured heights.",
}


@cache
def _font(size):
    # Without FreeType, a sized default font raises ImportError, which would surface as a logged 500,
    # or Pillow falls back to a fixed bitmap font that ignores the size.
    try:
        font = ImageFont.load_default(size=size)
    except (ImportError, OSError):
        font = None
    if not isinstance(font, ImageFont.FreeTypeFont):
        raise ValueError("The report page cannot be drawn: this server's Pillow has no FreeType for its text.")
    return font


# An empty cell where a value is missing, as in the copied table.
def _whole(value):
    # As JavaScript's Math.round, so the page and the copied table agree.
    return "" if value is None else str(math.floor(value + 0.5))


def _one(value):
    return "" if value is None else f"{value:.1f}"


def _length(text, font):
    parts = text.split("²")
    return sum(font.getlength(part) for part in parts) + (len(parts) - 1) * _font(round(font.size * 0.6)).getlength("2")


def _put(draw, x, y, text, font, colour):
    """Pillow's own font has no superscript two, so cm² gets a smaller 2 set at the cap height."""
    small = _font(round(font.size * 0.6))
    for index, part in enumerate(text.split("²")):
        if index:
            draw.text((x, y), "2", font=small, fill=colour)
            x += small.getlength("2")
        draw.text((x, y), part, font=font, fill=colour)
        x += font.getlength(part)


def _wrap(text, font, width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}" if line else word
        if line and _length(trial, font) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return [*lines, line] if line else [""]


def _landmarks(result):
    dissection = result.get("dissection")
    head = ["#", "Landmark", "Max (mm)", "Perp. (mm)", "Area (cm²)", *(["TL max (mm)", "FL max (mm)"] if dissection else []), "Note"]
    rows = []
    for mark in result["landmarks"]:
        values = [_whole(mark["diameter_mm"]), _whole(mark["perpendicular_mm"]), _one(mark["area_cm2"])]
        if dissection:
            values += [_whole((mark.get("true_lumen") or {}).get("diameter_mm")), _whole((mark.get("false_lumen") or {}).get("diameter_mm"))]
        available = mark["available"]
        note = "" if available else mark.get("unavailable_reason") or "Not measured."
        rows.append([str(mark["number"]), mark["name"], *(values if available else [""] * len(values)), note])
    return head, rows, {0, *range(2, len(head) - 1)}


def _sections(result):
    dissection = result.get("dissection")
    head = ["Section", "Length (mm)", "Max (mm)", "Volume (ml)", *(["TL volume (ml)", "FL volume (ml)"] if dissection else [])]
    rows = []
    for section in result["sections"]:
        values = [_whole(section["length_mm"]), _whole(section["max_diameter_mm"]), _one(section["volume_ml"])]
        if dissection:
            values += [_one(section["true_lumen_volume_ml"]), _one(section["false_lumen_volume_ml"])]
        rows.append([section["name"], *(values if section["available"] else [""] * len(values))])
    return head, rows, set(range(1, len(head)))


def _vertebrae(result):
    head = ["Vertebra", "Posterior (mm)", "Middle (mm)", "Anterior (mm)", "Difference (%)", "Volume (ml)", "Median (HU)", "Note"]
    rows = []
    for body in result["vertebrae"]:
        if not body["available"]:
            rows.append([body["name"], *[""] * 6, body.get("unavailable_reason") or "Not measured."])
            continue
        notes = [note for flag, note in ((body["height_flag"], "Height difference over 20%"), (body["small_body"], "Body under 2 ml")) if flag]
        heights = [_one(height["mm"]) for height in body["heights"]]
        rows.append([body["name"], *heights, _whole(body["height_difference_percent"]), _one(body["volume_ml"]), _whole(body["median_hu"]), "; ".join(notes)])
    return head, rows, set(range(1, len(head) - 1))


def _thresholds(result):
    native, arterial, venous = (_whole(result["density_thresholds_hu"][phase]) for phase in ("native", "arterial", "portal_venous"))
    return f"TotalSegmentator reads L1 to L5 as low density under {native} HU without contrast, {arterial} HU arterial and {venous} HU portal venous."


class _Page:
    def __init__(self, draw):
        self.draw, self.y = draw, _MARGIN

    def text(self, text, size=24, colour=_INK, gap=8):
        font = _font(size)
        for line in _wrap(text, font, _CONTENT):
            _put(self.draw, _MARGIN, self.y, line, font, colour)
            self.y += round(size * 1.35)
        self.y += gap

    def table(self, head, rows, right, wrap_last=False, size=22):
        """A table whose last column, with `wrap_last`, takes the width left and wraps."""
        font, line = _font(size), round(size * 1.4)
        widths = [max(_length(cell, font) for cell in column) + 2 * _PAD for column in zip(head, *rows)]
        if wrap_last:
            widths[-1] = max(_CONTENT - sum(widths[:-1]), 160)
        lefts = [_MARGIN + sum(widths[:index]) for index in range(len(widths))]

        def cells(row):
            wrapped = [_wrap(cell, font, widths[index] - 2 * _PAD) if wrap_last and index == len(row) - 1 else [cell] for index, cell in enumerate(row)]
            return wrapped, max(len(lines) for lines in wrapped) * line + _PAD

        def put(wrapped, colour):
            for index, lines in enumerate(wrapped):
                for number, text in enumerate(lines):
                    x = lefts[index] + widths[index] - _PAD - _length(text, font) if index in right else lefts[index] + _PAD
                    _put(self.draw, x, self.y + _PAD // 2 + number * line, text, font, colour)

        wrapped, height = cells(head)
        put(wrapped, _MUTED)
        self.y += height
        self.draw.line((_MARGIN, self.y, _MARGIN + sum(widths), self.y), fill=_RULE, width=2)
        for number, row in enumerate(rows):
            wrapped, height = cells(row)
            if number % 2:
                self.draw.rectangle((_MARGIN, self.y + 1, _MARGIN + sum(widths), self.y + height), fill=_STRIPE)
            put(wrapped, _INK)
            self.y += height
        self.y += 24


def _layout(draw, result, title):
    page = _Page(draw)
    engine = result["engine"]
    page.text(f"{title} report", size=44, gap=12)
    page.text(NOTICE, colour=_WARNING)
    page.text(f"{engine['name']} {engine['version']}, {result['report']} report {engine['report_version']}", colour=_MUTED, gap=0)
    page.text(f"3DScan revision {result['revision_id'][:8]} · measured {result['created_at'][:10]}", colour=_MUTED, gap=24)
    contrast = result.get("contrast") or {}
    if contrast.get("native"):
        page.text(f"Non-contrast scan (aorta median {_whole(contrast['aorta_median_hu'])} HU): true and false lumen not measured.", gap=16)
    if result.get("landmarks"):
        head, rows, right = _landmarks(result)
        page.table(head, rows, right, wrap_last=True)
    if result["report"] == "aorta" and result.get("sections"):
        head, rows, right = _sections(result)
        page.table(head, rows, right)
        page.text(_WIDER, colour=_MUTED, gap=24)
    if result.get("vertebrae"):
        head, rows, right = _vertebrae(result)
        page.table(head, rows, right, wrap_last=True)
        page.text(_thresholds(result), colour=_MUTED, gap=24)
    if warnings := result.get("warnings"):
        page.text("Warnings", size=28, gap=4)
        for warning in warnings:
            page.text(" ".join(str(warning).split()), gap=2)
        page.y += 22
    if second := next((line for name, line in _SECOND.items() if name in (result.get("files") or ())), None):
        page.text(second, colour=_MUTED)
    return page.y + _MARGIN


def render(result, title):
    """PNG bytes of the page: white, 1600 px wide and as tall as its content."""
    # Laid out once to measure; Pillow clips what falls outside the first image.
    height = _layout(ImageDraw.Draw(Image.new("RGB", (1, 1))), result, title)
    image = Image.new("RGB", (WIDTH, height), "white")
    _layout(ImageDraw.Draw(image), result, title)
    stream = BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()
