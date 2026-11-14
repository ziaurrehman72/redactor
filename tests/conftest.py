"""Shared fixtures. A real redaction of the sample takes ~5 s, so it is done
once per session; edge cases use tiny synthetic PDFs instead."""
from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from redactor import redact_report

SAMPLE = Path(__file__).resolve().parent.parent / "sampleReport" / "WM17S.pdf"
COLLECTED = "7/11/2023 11:08:00AM"

# Everything that identifies the patient / lab / signatories in the sample.
PII = [
    "DUMMY", "WM17SPF", "SELF", "Male", "25 Years", "17/1/2024",
    "LPL", "Lal", "Rohini", "ROHINI", "DELHI", "lalpathlabs",
    "Ajay Gupta", "Gurleen", "Mazumdar", "Sunanda", "Kamal Modi",
    "Page 1 of 7", "IMPORTANT INSTRUCTIONS", "Customer Care",
    "AHEEEHAPMC",  # first characters of the 2-D barcode text
]


@pytest.fixture(scope="session")
def sample() -> Path:
    assert SAMPLE.exists(), f"missing sample report {SAMPLE}"
    return SAMPLE


@pytest.fixture(scope="session")
def redacted(sample, tmp_path_factory):
    """(result, output path) of one real redaction of the sample."""
    out = tmp_path_factory.mktemp("redacted") / "sample_redacted.pdf"
    return redact_report(sample, out), out


@pytest.fixture(scope="session")
def locked_pdf(sample, tmp_path_factory) -> Path:
    """The sample, re-saved with the user password 's3cret'."""
    path = tmp_path_factory.mktemp("locked") / "locked.pdf"
    with pymupdf.open(sample) as d:
        d.authenticate("")
        d.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256,
               user_pw="s3cret", owner_pw="owner")
    return path


def make_pdf(path: Path, *lines: tuple[tuple[float, float], str]) -> Path:
    """Write a one-page PDF with the given (position, text) lines."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=841)
    for pos, text in lines:
        page.insert_text(pos, text, fontsize=10)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def minimal_report(tmp_path) -> Path:
    """Smallest PDF the redactor accepts: a 'Test Report' title and a
    collection date, plus some header text that must disappear."""
    return make_pdf(
        tmp_path / "mini.pdf",
        ((25, 70), "Name : Jane Roe"),
        ((25, 100), "Collected"),
        ((92, 100), "1/2/2024  09:30:00AM"),
        ((280, 200), "Test Report"),
        ((40, 300), "Glucose 90 mg/dL"),
    )
