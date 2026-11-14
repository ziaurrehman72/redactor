"""Tests for redactor.core."""
from __future__ import annotations

import hashlib

import pymupdf
import pytest

from redactor import core
from redactor.core import RedactionError, redact_report

from .conftest import COLLECTED, PII, make_pdf


def full_text(path) -> str:
    with pymupdf.open(path) as d:
        return "\n".join(p.get_text() for p in d)


def blank(page, rect) -> bool:
    """True if nothing but (near-)white is drawn in `rect` (annotations such
    as the kept 'Sample Report' watermark are ignored)."""
    pm = page.get_pixmap(clip=rect, dpi=100, annots=False)
    return min(pm.samples) >= 250


# --------------------------------------------------------------------------- #
# End-to-end on the real sample
# --------------------------------------------------------------------------- #
class TestSampleRedaction:
    def test_result(self, redacted):
        result, out = redacted
        assert result.pages == 7
        assert result.collected == COLLECTED
        assert result.warnings == []
        assert result.dst == out and out.exists()

    @pytest.mark.parametrize("needle", PII)
    def test_no_pii_text_survives(self, redacted, needle):
        _, out = redacted
        assert needle not in full_text(out)

    def test_page_count_and_size_unchanged(self, sample, redacted):
        _, out = redacted
        with pymupdf.open(sample) as a, pymupdf.open(out) as b:
            assert a.page_count == b.page_count == 7
            assert [p.rect for p in a] == [p.rect for p in b]

    def test_collection_date_kept_on_every_page(self, redacted):
        _, out = redacted
        with pymupdf.open(out) as d:
            for page in d:
                assert f"Collected : {COLLECTED}" in page.get_text()

    def test_reported_date_is_not_the_one_kept(self, redacted):
        _, out = redacted
        assert "17/1/2024" not in full_text(out)

    def test_report_body_is_intact(self, sample, redacted):
        _, out = redacted
        text = full_text(out)
        for keep in ["Creatinine", "Vitamin B12", "HbA1c", "THYROID PROFILE",
                     "COMPLETE BLOOD COUNT", "Test Report", "Bio. Ref. Interval",
                     "This is a revised report", "Sample Report"]:
            assert keep in text, keep

    def test_every_body_line_is_preserved_on_pages_1_to_6(self, sample, redacted):
        """Nothing between the header and the footer is lost (barcode text and
        page numbers excepted)."""
        _, out = redacted
        with pymupdf.open(sample) as a, pymupdf.open(out) as b:
            for pa, pb in list(zip(a, b))[:6]:
                limit = core.header_bottom(pa)
                body_in = {t for t, bb, _ in core.text_lines(pa)
                           if bb.y0 > limit and bb.y1 < core.FOOTER_BAND_TOP
                           and not t.startswith(("Page ", "*"))}
                body_out = {t for t, _, _ in core.text_lines(pb)}
                assert body_in, f"p{pa.number + 1}: test found no body text"
                assert not body_in - body_out, f"p{pa.number + 1} lost {body_in - body_out}"

    def test_last_page_keeps_body_but_not_signatories(self, redacted):
        _, out = redacted
        with pymupdf.open(out) as d:
            text = d[-1].get_text()
        for keep in ["leucocyte counts are additionally", "Test conducted on EDTA whole blood",
                     "This is a revised report"]:
            assert keep in text
        for gone in ["MD, Pathology", "Technical Director", "Consultant", "NRL", "MCI", "DMC"]:
            assert gone not in text

    def test_value_next_to_barcode_survives(self, redacted):
        """Regression: the barcode font box overlaps the last table row's value."""
        _, out = redacted
        with pymupdf.open(out) as d:
            assert "9.00" in d[0].get_text().split()

    def test_barcode_bars_are_erased_on_every_page(self, redacted):
        """The bars sit below the body; make sure none of their ink is left."""
        _, out = redacted
        with pymupdf.open(out) as d:
            for page in d:
                assert blank(page, pymupdf.Rect(250, 778, 362, 800)), f"p{page.number + 1}"

    def test_barcode_rect_does_not_clip_table_border_row(self, sample, redacted):
        """Regression: the barcode font box overlapped the last row of page 2's
        table and deleted two '|' border characters from it."""
        _, out = redacted
        row = "|   High     |                    |                    |                |                    |"
        with pymupdf.open(sample) as a, pymupdf.open(out) as b:
            assert row in {t for t, _, _ in core.text_lines(a[1])}
            assert row in {t for t, _, _ in core.text_lines(b[1])}

    def test_header_and_footer_pixels_are_white(self, redacted):
        _, out = redacted
        with pymupdf.open(out) as d:
            for page in d:
                top = core.header_bottom(page)
                # below the re-stamped date line (y~33-42), above the title
                assert blank(page, pymupdf.Rect(0, 50, 595, top)), f"header p{page.number + 1}"
                assert blank(page, pymupdf.Rect(0, core.FOOTER_BAND_TOP, 595, 841)), f"footer p{page.number + 1}"

    def test_last_page_signatures_qr_and_box_are_gone(self, redacted):
        _, out = redacted
        with pymupdf.open(out) as d:
            last = d[-1]
            # signature images (y 279-424) and the doctor text below them
            assert blank(last, pymupdf.Rect(0, 275, 595, 482))
            # 2-D barcode + instructions box
            assert blank(last, pymupdf.Rect(0, 500, 595, 790))
            # (vector removal is asserted in test_framed_boxes_are_removed_as_vectors)

    def test_no_signature_images_remain_with_ink(self, redacted):
        _, out = redacted
        with pymupdf.open(out) as d:
            for page in d:
                for info in page.get_image_info(xrefs=True):
                    pm = pymupdf.Pixmap(d, info["xref"])
                    assert min(pm.samples) >= 250, f"image {info['xref']} on p{page.number + 1}"

    def test_body_pixels_are_unchanged(self, sample, redacted):
        """Pixel-for-pixel, the area between header and barcode is identical to
        the original on pages 1-6, so no table border / text is damaged."""
        _, out = redacted
        with pymupdf.open(sample) as a, pymupdf.open(out) as b:
            for pa, pb in list(zip(a, b))[:6]:
                clip = pymupdf.Rect(0, core.header_bottom(pa) + 1, 595, 760)
                ia = pa.get_pixmap(clip=clip, dpi=100, annots=False)
                ib = pb.get_pixmap(clip=clip, dpi=100, annots=False)
                assert ia.samples == ib.samples, f"body changed on p{pa.number + 1}"

    def test_framed_boxes_are_removed_as_vectors(self, redacted):
        """Header frame and the instructions frame are dropped, not just covered;
        only our white redaction rectangles are added."""
        _, out = redacted
        with pymupdf.open(out) as d:
            for page in d:
                strokes = [dr for dr in page.get_drawings() if dr.get("fill") is None]
                # the only unfilled vector lines left are the results table's
                assert all(dr["rect"].y0 >= 200 for dr in strokes), f"p{page.number + 1}"
            assert [dr for dr in d[-1].get_drawings() if dr.get("fill") is None] == []

    def test_watermark_annotation_is_kept(self, redacted):
        _, out = redacted
        with pymupdf.open(out) as d:
            assert all(len(list(p.annots())) == 1 for p in d)

    def test_metadata_and_encryption_wiped(self, sample, redacted):
        _, out = redacted
        with pymupdf.open(sample) as a:
            assert a.metadata["encryption"] and a.metadata["creationDate"]
        with pymupdf.open(out) as d:
            assert not d.is_encrypted
            assert not any(v for k, v in d.metadata.items() if k != "format")
            assert d.get_xml_metadata() == ""

    def test_no_leftover_pii_in_raw_pdf_objects(self, redacted):
        """Redaction is real: nothing readable is left in any object/stream."""
        _, out = redacted
        with pymupdf.open(out) as d:
            blob = b""
            for xref in range(1, d.xref_length()):
                blob += d.xref_object(xref, compressed=False).encode("latin-1", "ignore")
                try:
                    blob += d.xref_stream(xref) or b""
                except Exception:
                    pass
        for needle in (b"DUMMY", b"WM17SPF", b"Pathlabs", b"PathLabs"):
            assert needle not in blob, needle

    def test_output_is_smaller_than_input(self, sample, redacted):
        _, out = redacted
        assert out.stat().st_size < sample.stat().st_size


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class TestHelpers:
    def test_find_collected_datetime_picks_collected_not_reported(self, sample):
        with pymupdf.open(sample) as d:
            assert core.find_collected_datetime(d[0]) == COLLECTED
            assert core.find_collected_datetime(d[6]) == COLLECTED

    def test_find_collected_datetime_none_when_absent(self, tmp_path):
        p = make_pdf(tmp_path / "x.pdf", ((50, 100), "nothing here"))
        with pymupdf.open(p) as d:
            assert core.find_collected_datetime(d[0]) is None

    def test_find_collected_datetime_ignores_collected_at(self, tmp_path):
        p = make_pdf(tmp_path / "x.pdf",
                     ((25, 100), "Collected at :"), ((100, 100), "Some Lab"))
        with pymupdf.open(p) as d:
            assert core.find_collected_datetime(d[0]) is None

    def test_find_collected_datetime_normalises_whitespace(self, minimal_report):
        with pymupdf.open(minimal_report) as d:
            assert core.find_collected_datetime(d[0]) == "1/2/2024 09:30:00AM"

    def test_header_bottom(self, sample):
        with pymupdf.open(sample) as d:
            assert core.header_bottom(d[0]) == pytest.approx(191 - core.HEADER_GAP, abs=1.5)
            # pages 5-7 have a slightly taller header
            assert core.header_bottom(d[6]) > core.header_bottom(d[0])

    def test_header_bottom_without_title_raises(self, tmp_path):
        p = make_pdf(tmp_path / "x.pdf", ((50, 100), "hello"))
        with pymupdf.open(p) as d, pytest.raises(RedactionError, match="Test Report"):
            core.header_bottom(d[0])

    def test_plan_page_regions(self, sample):
        with pymupdf.open(sample) as d:
            first, last = core.plan_page(d[0]), core.plan_page(d[6])
        assert first.page_no == 1 and last.page_no == 7
        # header, footer band, barcode, page number
        assert len(first.rects) == 4
        # + signature block, 2-D barcode, instruction box
        assert len(last.rects) == 7
        assert "Mr.   DUMMY" in first.removed_lines or "Mr. DUMMY" in " ".join(first.removed_lines)
        assert any("Page 1 of 7" == t for t in first.removed_lines)

    def test_plan_page_leaves_body_text_out_of_the_removal_set(self, sample):
        with pymupdf.open(sample) as d:
            plan = core.plan_page(d[0])
        assert "Creatinine" not in plan.removed_lines
        assert "9.00" not in plan.removed_lines

    def test_containing_box(self, sample):
        with pymupdf.open(sample) as d:
            last = d[6]
            hit = last.search_for("IMPORTANT INSTRUCTIONS")[0]
            box = core.containing_box(last, hit)
            assert box is not None and box.contains(hit)
            assert core.containing_box(last, pymupdf.Rect(1, 1, 5, 5)) is None


# --------------------------------------------------------------------------- #
# Synthetic PDFs: edge cases without the 5 s cost
# --------------------------------------------------------------------------- #
class TestSyntheticReports:
    def test_minimal_report_is_redacted(self, minimal_report, tmp_path):
        out = tmp_path / "o.pdf"
        res = redact_report(minimal_report, out)
        text = full_text(out)
        assert "Jane Roe" not in text and "Name :" not in text
        assert "Collected : 1/2/2024 09:30:00AM" in text
        assert "Glucose 90 mg/dL" in text and "Test Report" in text
        assert res.collected == "1/2/2024 09:30:00AM" and res.pages == 1

    def test_missing_collection_date_gives_warning_not_failure(self, tmp_path):
        src = make_pdf(tmp_path / "n.pdf", ((50, 80), "Name: X"),
                       ((280, 200), "Test Report"), ((40, 300), "Result 1"))
        res = redact_report(src, tmp_path / "o.pdf")
        assert res.collected is None
        assert res.warnings == ["page 1: no collection date found"]
        assert "Name: X" not in full_text(tmp_path / "o.pdf")

    def test_unsupported_layout_raises(self, tmp_path):
        src = make_pdf(tmp_path / "n.pdf", ((50, 80), "just some text"))
        out = tmp_path / "o.pdf"
        with pytest.raises(RedactionError, match="unsupported layout"):
            redact_report(src, out)
        assert not out.exists()

    def test_output_in_new_dir_requires_existing_parent(self, minimal_report, tmp_path):
        with pytest.raises(RedactionError):
            redact_report(minimal_report, tmp_path / "missing" / "o.pdf")

    def test_input_is_not_modified(self, minimal_report, tmp_path):
        before = hashlib.sha256(minimal_report.read_bytes()).hexdigest()
        redact_report(minimal_report, tmp_path / "o.pdf")
        assert hashlib.sha256(minimal_report.read_bytes()).hexdigest() == before

    def test_accepts_str_paths(self, minimal_report, tmp_path):
        res = redact_report(str(minimal_report), str(tmp_path / "o.pdf"))
        assert res.dst == tmp_path / "o.pdf"

    def test_multi_page_report_gets_stamp_on_every_page(self, tmp_path):
        doc = pymupdf.open()
        for i in range(3):
            pg = doc.new_page(width=595, height=841)
            pg.insert_text((25, 100), "Collected", fontsize=10)
            pg.insert_text((92, 100), "3/4/2024  10:00:00PM", fontsize=10)
            pg.insert_text((280, 200), "Test Report", fontsize=10)
            pg.insert_text((40, 300), f"Body {i}", fontsize=10)
        src = tmp_path / "m.pdf"
        doc.save(src)
        out = tmp_path / "o.pdf"
        res = redact_report(src, out)
        assert res.pages == 3
        with pymupdf.open(out) as d:
            for i, page in enumerate(d):
                t = page.get_text()
                assert "Collected : 3/4/2024 10:00:00PM" in t and f"Body {i}" in t


class TestErrors:
    def test_same_input_and_output_refused(self, minimal_report):
        before = minimal_report.read_bytes()
        with pytest.raises(RedactionError, match="same as the input"):
            redact_report(minimal_report, minimal_report)
        assert minimal_report.read_bytes() == before

    def test_not_a_pdf(self, tmp_path):
        src = tmp_path / "x.pdf"
        src.write_text("hello, I am not a pdf")
        with pytest.raises(RedactionError, match="not a readable PDF"):
            redact_report(src, tmp_path / "o.pdf")

    def test_empty_file(self, tmp_path):
        src = tmp_path / "x.pdf"
        src.write_bytes(b"")
        with pytest.raises(RedactionError):
            redact_report(src, tmp_path / "o.pdf")

    def test_pdf_without_pages(self, tmp_path):
        src = tmp_path / "zero.pdf"
        src.write_bytes(
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
            b"trailer<</Root 1 0 R/Size 3>>\n%%EOF\n")
        with pytest.raises(RedactionError):  # "no pages" or "not a readable PDF"
            redact_report(src, tmp_path / "o.pdf")
        assert not (tmp_path / "o.pdf").exists()

    def test_missing_input(self, tmp_path):
        with pytest.raises(RedactionError):
            redact_report(tmp_path / "nope.pdf", tmp_path / "o.pdf")

    def test_wrong_and_missing_password(self, locked_pdf, tmp_path):
        out = tmp_path / "o.pdf"
        for pw in ("", "wrong"):
            with pytest.raises(RedactionError, match="password"):
                redact_report(locked_pdf, out, password=pw)
        assert list(tmp_path.iterdir()) == []

    def test_correct_password(self, locked_pdf, minimal_report, tmp_path):
        # Re-lock the small synthetic report: the sample would take 5 s again.
        locked = tmp_path / "locked_mini.pdf"
        with pymupdf.open(minimal_report) as d:
            d.save(locked, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                   user_pw="s3cret", owner_pw="owner")
        out = tmp_path / "o.pdf"
        res = redact_report(locked, out, password="s3cret")
        assert res.pages == 1
        with pymupdf.open(out) as d:
            assert not d.is_encrypted

    def test_failed_verification_leaves_no_output_and_no_temp(
            self, minimal_report, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RedactionError("verification failed: pretend")
        monkeypatch.setattr(core, "verify", boom)
        out = tmp_path / "o.pdf"
        with pytest.raises(RedactionError, match="verification failed"):
            redact_report(minimal_report, out)
        assert not out.exists()
        assert [p.name for p in tmp_path.iterdir()] == ["mini.pdf"]  # no .tmp left

    def test_failure_does_not_clobber_existing_output(
            self, minimal_report, tmp_path, monkeypatch):
        out = tmp_path / "o.pdf"
        out.write_bytes(b"previous good output")
        monkeypatch.setattr(core, "verify",
                            lambda *a, **k: (_ for _ in ()).throw(RedactionError("x")))
        with pytest.raises(RedactionError):
            redact_report(minimal_report, out)
        assert out.read_bytes() == b"previous good output"

    def test_unexpected_exceptions_are_wrapped(self, minimal_report, tmp_path, monkeypatch):
        monkeypatch.setattr(core, "plan_page", lambda p: 1 / 0)
        with pytest.raises(RedactionError, match="ZeroDivisionError"):
            redact_report(minimal_report, tmp_path / "o.pdf")
        assert not (tmp_path / "o.pdf").exists()

    def test_success_replaces_existing_output(self, minimal_report, tmp_path):
        out = tmp_path / "o.pdf"
        out.write_bytes(b"old")
        redact_report(minimal_report, out)
        assert out.read_bytes()[:5] == b"%PDF-"


# --------------------------------------------------------------------------- #
# The safety net itself
# --------------------------------------------------------------------------- #
class TestVerify:
    def _plans(self, path):
        with pymupdf.open(path) as d:
            collected = {p.number: core.find_collected_datetime(p) for p in d}
            return [core.plan_page(p) for p in d], collected

    def test_detects_unredacted_file(self, sample):
        """Feeding the ORIGINAL file to verify() must fail: it is full of PII."""
        plans, collected = self._plans(sample)
        with pytest.raises(RedactionError) as e:
            core.verify(sample, plans, collected)
        assert "text survived" in str(e.value)

    def test_detects_leftover_metadata(self, minimal_report, tmp_path):
        plans, collected = self._plans(minimal_report)
        out = tmp_path / "o.pdf"
        redact_report(minimal_report, out)
        with pymupdf.open(out) as d:
            d.set_metadata({"author": "Jane Roe"})
            tagged = tmp_path / "tagged.pdf"
            d.save(tagged)
        with pytest.raises(RedactionError, match="metadata"):
            core.verify(tagged, plans, collected)

    def test_detects_leftover_ink_in_removed_region(self, minimal_report, tmp_path):
        plans, collected = self._plans(minimal_report)
        out = tmp_path / "o.pdf"
        redact_report(minimal_report, out)
        with pymupdf.open(out) as d:
            d[0].draw_rect(pymupdf.Rect(100, 120, 200, 150), color=(0, 0, 0), fill=(0, 0, 0))
            dirty = tmp_path / "dirty.pdf"
            d.save(dirty)
        with pytest.raises(RedactionError, match="non-blank pixels"):
            core.verify(dirty, plans, collected)

    def test_passes_on_clean_output(self, minimal_report, tmp_path):
        plans, collected = self._plans(minimal_report)
        out = tmp_path / "o.pdf"
        redact_report(minimal_report, out)
        core.verify(out, plans, collected)  # no exception
