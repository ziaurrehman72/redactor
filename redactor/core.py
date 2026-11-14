"""Core redaction logic for lab-report PDFs (no CLI concerns in here).

What is removed (from every page):
  * the whole page header  - letterhead, patient block (name, lab no., age,
    gender, referrer, reported date, status, collected-at / processed-at labs)
  * the whole page footer  - letterhead footer band, page number, barcode
  * on the last page       - doctor signature / name block, the 2-D barcode
                             and the "IMPORTANT INSTRUCTIONS" box

What is kept:
  * the report body (tests, results, units, reference ranges, notes)
  * the report *collection* date/time, re-written as a single clean line

Removal is real, not cosmetic: text is deleted from the content stream,
image pixels are blanked, vector lines are dropped, metadata is wiped and the
file is rewritten (garbage-collected) so old objects do not linger.

Public API: :func:`redact_report` and :class:`RedactionError`.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
DATETIME_RE = re.compile(r"\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*[AP]M", re.I)
BARCODE_TEXT_RE = re.compile(r"^\*[A-Z0-9\-\. $/+%]+\*$")  # Code-39 style "*WM17SPF*"
BARCODE_2D_RE = re.compile(r"^[A-P]{40,}$")  # 2-D barcode drawn with a glyph font
PAGE_NO_RE = re.compile(r"^Page\s+\d+\s+of\s+\d+$", re.I)

FOOTER_BAND_TOP = 796.0  # letterhead footer band starts here (pt from top)
HEADER_GAP = 2.0  # header ends this far above the "Test Report" title
SIGNATURE_GAP = 2.0
BOX_PAD = 3.0  # grow a framed box's rect so its stroke is fully covered (and removed)

# Text of the kept date line
COLLECTED_LABEL = "Collected"
STAMP_FONT, STAMP_SIZE = "hebo", 9  # Helvetica-Bold
STAMP_POS = (35.0, 40.0)  # left margin / baseline


class RedactionError(Exception):
    """A PDF could not be redacted (bad file, wrong password, unknown layout,
    or the safety check found something that should have been removed)."""


@dataclass
class RedactionResult:
    src: Path
    dst: Path
    pages: int
    collected: str | None  # the kept collection date/time
    warnings: list[str] = field(default_factory=list)


@dataclass
class PageResult:
    page_no: int
    rects: list[pymupdf.Rect]  # everything that gets erased
    removed_lines: set[str]
    frames: list[pymupdf.Rect] = field(default_factory=list)  # subset: vector boxes, see redact_page


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def text_lines(page: pymupdf.Page, clip: pymupdf.Rect | None = None):
    """Yield (text, bbox, fontname) for every text line on the page."""
    for block in page.get_text("dict", clip=clip)["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            txt = "".join(s["text"] for s in line["spans"]).strip()
            if txt:
                yield txt, pymupdf.Rect(line["bbox"]), line["spans"][0]["font"]


def find_collected_datetime(page: pymupdf.Page) -> str | None:
    """Value printed next to the 'Collected' label (not 'Reported')."""
    label = None
    for txt, bbox, _ in text_lines(page):
        if txt.rstrip(" :") == COLLECTED_LABEL:
            label = bbox
            break
    if label is None:
        return None
    best = None
    for txt, bbox, _ in text_lines(page):
        if not DATETIME_RE.fullmatch(txt):
            continue
        same_row = abs((bbox.y0 + bbox.y1) / 2 - (label.y0 + label.y1) / 2) < 4
        if same_row and bbox.x0 >= label.x1 and (best is None or bbox.x0 < best[1]):
            best = (txt, bbox.x0)
    return re.sub(r"\s+", " ", best[0]) if best else None


def header_bottom(page: pymupdf.Page) -> float:
    """The header ends just above the 'Test Report' title."""
    hits = page.search_for("Test Report")
    if not hits:
        raise RedactionError(
            f"page {page.number + 1}: no 'Test Report' title found - unsupported layout?"
        )
    return min(r.y0 for r in hits) - HEADER_GAP


def containing_box(page: pymupdf.Page, inner: pymupdf.Rect) -> pymupdf.Rect | None:
    """Smallest vector rectangle that fully contains `inner` (a framed box)."""
    best = None
    for d in page.get_drawings():
        r = d["rect"]
        if r.width > 50 and r.height > 20 and r.contains(inner):
            if best is None or r.get_area() < best.get_area():
                best = r
    return best


def _barcode_rect(bbox: pymupdf.Rect, lines) -> pymupdf.Rect:
    """Rectangle to erase for a barcode drawn with a font.

    The font's text box is much taller than the bars and can overlap the last
    row of the results table. Start the rectangle below any body text that
    overlaps it horizontally, so that text is never clipped.
    """
    top = bbox.y0
    for txt, other, _ in lines:
        if BARCODE_TEXT_RE.match(txt) or PAGE_NO_RE.match(txt):
            continue
        overlaps_x = other.x0 < bbox.x1 and other.x1 > bbox.x0
        if overlaps_x and other.y0 < bbox.y1 - 5:
            top = max(top, other.y1 + 0.5)
    return pymupdf.Rect(bbox.x0 - 2, top, bbox.x1 + 2, bbox.y1)


def plan_page(page: pymupdf.Page) -> PageResult:
    w, h = page.rect.width, page.rect.height
    rects: list[pymupdf.Rect] = []
    frames: list[pymupdf.Rect] = []

    # 1. header (everything above the "Test Report" title, incl. letterhead image)
    rects.append(pymupdf.Rect(0, 0, w, header_bottom(page)))

    # 2. footer band of the letterhead
    rects.append(pymupdf.Rect(0, FOOTER_BAND_TOP, w, h))

    lines = list(text_lines(page))
    twod_rows: list[pymupdf.Rect] = []
    for txt, bbox, _ in lines:
        if BARCODE_TEXT_RE.match(txt):  # bottom barcode, e.g. *WM17SPF*
            rects.append(_barcode_rect(bbox, lines))
        elif PAGE_NO_RE.match(txt):  # "Page 3 of 7"
            rects.append(bbox + (-2, -1, 2, 1))
        elif BARCODE_2D_RE.match(txt):
            twod_rows.append(bbox)

    # 3. last-page extras: signatories, 2-D barcode, instruction box
    if twod_rows:
        rects.append(pymupdf.Rect(twod_rows[0]).include_rect(twod_rows[-1]) + (-4, -4, 4, 4))

    sig_imgs = [
        pymupdf.Rect(i["bbox"])
        for i in page.get_image_info()
        if i["bbox"][2] - i["bbox"][0] < w * 0.6 and i["bbox"][1] > header_bottom(page)
    ]
    if sig_imgs:
        top = min(r.y0 for r in sig_imgs) - 4
        revised = [b for t, b, _ in lines if t.lower().startswith("this is a revised report")]
        bottom = (revised[0].y0 if revised else max(r.y1 for r in sig_imgs)) - SIGNATURE_GAP
        rects.append(pymupdf.Rect(0, top, w, bottom))

    for hit in page.search_for("IMPORTANT INSTRUCTIONS"):
        box = containing_box(page, hit)
        if box:
            frames.append(box + (-BOX_PAD, -BOX_PAD, BOX_PAD, BOX_PAD))
    rects.extend(frames)

    # remember what we are about to delete (used for the leak check)
    removed = set()
    for txt, bbox, _ in lines:
        if any(r.contains(bbox) for r in rects):
            removed.add(txt)
    return PageResult(page.number + 1, rects, removed, frames)


def redact_page(page: pymupdf.Page, plan: PageResult) -> None:
    # Vector frames go in their own pass: MuPDF keeps a line-art path that is
    # only *partly* touched by another rectangle in the same pass (the bottom
    # barcode overlaps the instructions frame), so they would survive otherwise.
    passes = [plan.frames, [r for r in plan.rects if r not in plan.frames]]
    for rects in filter(None, passes):
        for r in rects:
            page.add_redact_annot(r, fill=(1, 1, 1))
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_PIXELS,  # blank the pixels, not just hide
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )


def stamp_collected(page: pymupdf.Page, value: str) -> None:
    page.insert_text(STAMP_POS, f"Collected : {value}", fontname=STAMP_FONT,
                     fontsize=STAMP_SIZE, color=(0, 0, 0))


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def redact_report(src: Path, dst: Path, password: str = "") -> RedactionResult:
    """Redact `src` and write the result to `dst`.

    The output is written to a temporary file, verified, and only then moved
    into place - a file that fails verification is never left at `dst`.
    Raises :class:`RedactionError` on any problem.
    """
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise RedactionError("output path is the same as the input; refusing to overwrite it")

    tmp = dst.with_name(f".{dst.name}.tmp")
    try:
        result = _redact(src, tmp, dst, password)
        os.replace(tmp, dst)
        return result
    except RedactionError:
        raise
    except Exception as exc:  # pymupdf raises various RuntimeError/FileDataError types
        raise RedactionError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)


def _redact(src: Path, tmp: Path, dst: Path, password: str) -> RedactionResult:
    try:
        doc = pymupdf.open(src, filetype="pdf")
    except Exception as exc:
        raise RedactionError(f"not a readable PDF ({exc})") from exc
    with doc:
        if doc.needs_pass and not doc.authenticate(password):
            raise RedactionError("password required or incorrect (use --password)")
        if doc.page_count == 0:
            raise RedactionError("PDF has no pages")

        # Read everything we need *before* touching anything.
        collected = {p.number: find_collected_datetime(p) for p in doc}
        plans = [plan_page(p) for p in doc]
        warnings = []

        for page, plan in zip(doc, plans):
            redact_page(page, plan)
            if collected[page.number]:
                stamp_collected(page, collected[page.number])
            else:
                warnings.append(f"page {page.number + 1}: no collection date found")

        # Wipe document-level personal data and rewrite the file cleanly.
        doc.set_metadata({})
        doc.del_xml_metadata()
        doc.save(tmp, garbage=4, deflate=True, clean=True,
                 encryption=pymupdf.PDF_ENCRYPT_NONE)
        pages = doc.page_count

    verify(tmp, plans, collected)
    first = next((v for v in collected.values() if v), None)
    return RedactionResult(src, dst, pages, first, warnings)


def verify(dst: Path, plans: list[PageResult], collected: dict[int, str | None]) -> None:
    """Fail loudly if anything that should be gone is still in the output."""
    out = pymupdf.open(dst)
    problems = []
    keep = {re.sub(r"\s+", " ", v) for v in collected.values() if v}
    for page, plan in zip(out, plans):
        remaining = {t for t, _, _ in text_lines(page)}
        for txt in plan.removed_lines:
            if len(txt) >= 3 and txt in remaining and txt not in keep:
                problems.append(f"p{plan.page_no}: text survived: {txt!r}")
        # blanked pixels: nothing but white may be left under the removed rects,
        # except our own re-stamped date line (the watermark annot is kept on purpose)
        stamp = [r + (-2, -2, 2, 2) for r in page.search_for("Collected :")]
        stamp = [r.include_rect(pymupdf.Rect(r.x0, r.y0, 595, r.y1)) for r in stamp]
        for r in plan.rects:
            if _has_ink(page, r, stamp):
                problems.append(f"p{plan.page_no}: non-blank pixels in {tuple(round(v) for v in r)}")
        # no leftover embedded image may carry the original raster data
        for img in page.get_image_info(xrefs=True):
            r = pymupdf.Rect(img["bbox"])
            if any(r.intersects(x) and x.contains(r) for x in plan.rects):
                pm = pymupdf.Pixmap(out, img["xref"])
                if pm.colorspace and any(b < 250 for b in pm.samples):
                    problems.append(f"p{plan.page_no}: image xref {img['xref']} still has pixels")
    if out.is_encrypted or any(v for v in out.metadata.values() if v not in (None, "", "format")):
        meta = {k: v for k, v in out.metadata.items() if v and k != "format"}
        if meta or out.is_encrypted:
            problems.append(f"metadata/encryption left: {meta}")
    out.close()
    if problems:
        raise RedactionError("verification failed:\n    " + "\n    ".join(problems))


def _has_ink(page: pymupdf.Page, region: pymupdf.Rect, ignore: list[pymupdf.Rect]) -> bool:
    dpi = 100
    pm = page.get_pixmap(clip=region, dpi=dpi, annots=False)
    k = dpi / 72
    for y in range(pm.height):
        for x in range(pm.width):
            if min(pm.pixel(x, y)[:3]) < 250:
                pt = pymupdf.Point(region.x0 + x / k, region.y0 + y / k)
                if not any(r.contains(pt) for r in ignore):
                    return True
    return False
