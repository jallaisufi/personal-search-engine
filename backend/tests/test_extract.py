import pytest

from app.ingest.extract import extract, extract_markdown


def test_markdown_headings_build_paths():
    blocks = extract_markdown(
        "# Lease\n\nIntro text.\n\n## Rent\n\nRent is 1,150 EUR.\n\n## Deposit\n\nThree months.\n"
    )
    assert [(b.text, b.heading_path) for b in blocks] == [
        ("Lease", "Lease"),
        ("Intro text.", "Lease"),
        ("Rent", "Lease > Rent"),
        ("Rent is 1,150 EUR.", "Lease > Rent"),
        ("Deposit", "Lease > Deposit"),
        ("Three months.", "Lease > Deposit"),
    ]


def test_markdown_paragraph_lines_are_joined():
    blocks = extract_markdown("line one\nline two\n\nnext para")
    assert [b.text for b in blocks] == ["line one\nline two", "next para"]


def test_docx_sample_keeps_headings(sample_docs):
    blocks = extract(sample_docs / "work" / "onboarding-guide.docx")
    on_call = next(b for b in blocks if "shadow one on-call" in b.text)
    assert on_call.heading_path == "Engineering Onboarding Guide > On-call"


def test_pdf_sample_keeps_pages(sample_docs):
    pytest.importorskip("fitz")
    blocks = extract(sample_docs / "travel" / "conference-trip.pdf")
    train = next(b for b in blocks if "Darmstadt on 14 April" in b.text)
    talks = next(b for b in blocks if "pgvector" in b.text)
    assert train.page == 1
    assert talks.page == 2


def test_unsupported_type(tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG")
    with pytest.raises(ValueError):
        extract(path)
