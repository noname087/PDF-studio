"""Basic checks. Run:  python -m pytest -q   (or: python tests/test_engine.py)"""
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import pdf_engine as E  # noqa: E402
from app.thai import normalize_thai  # noqa: E402


def make_doc(text="ทดสอบ ภาษาไทย", size=18, rotation=0):
    doc = pymupdf.open()
    page = doc.new_page()
    E.add_text(page, 72, 100, text, E.TextStyle(family="Sarabun", size=size))
    E.add_text(page, 72, 140, "บรรทัดที่สอง ห้ามหาย", E.TextStyle(family="Sarabun", size=size))
    if rotation:
        page.set_rotation(rotation)
    return doc


def test_normalize_pua():
    # Word-style PDF text: shifted tone marks stored as Private Use Area chars
    assert normalize_thai("ผู") == "ผู้"
    assert normalize_thai("ที") == "ที่"
    assert normalize_thai("าน") == "ฐาน"
    assert normalize_thai("น้ํา") == "น้ำ"      # ้ + ํ + า
    assert normalize_thai("นํ้า") == "น้ำ"      # ํ + ้ + า
    assert normalize_thai("ก่ี") == "กี่"            # tone before vowel


def test_edit_keeps_thai_and_neighbours():
    doc = make_doc()
    page = doc[0]
    lines = E.page_lines(page)
    first = lines[0]
    new = "น้ำใจ ผู้ใหญ่ ปั้นจั่น ฎีกา ญาติ ฐาน"
    E.replace_line(page, first["bbox"], first["origin"], new,
                   E.TextStyle(family="Sarabun", size=first["size"]))
    after = [l["text"] for l in E.page_lines(page)]
    assert new in after
    assert "บรรทัดที่สอง ห้ามหาย" in after
    assert "ทดสอบ ภาษาไทย" not in after
    moved = [l for l in E.page_lines(page) if l["text"] == new][0]
    assert abs(moved["origin"][1] - first["origin"][1]) < 0.6   # same baseline
    assert abs(moved["origin"][0] - first["origin"][0]) < 0.6   # same left edge


def test_edit_on_rotated_page():
    doc = make_doc(rotation=90)
    page = doc[0]
    lines = E.page_lines(page)
    first = [l for l in lines if l["text"] == "ทดสอบ ภาษาไทย"][0]
    E.replace_line(page, first["bbox"], first["origin"], "แก้ไขบนหน้าที่หมุน",
                   E.TextStyle(family="Sarabun", size=first["size"]))
    after = E.page_lines(page)
    texts = [l["text"] for l in after]
    assert "แก้ไขบนหน้าที่หมุน" in texts and "ทดสอบ ภาษาไทย" not in texts
    assert "บรรทัดที่สอง ห้ามหาย" in texts
    moved = [l for l in after if l["text"] == "แก้ไขบนหน้าที่หมุน"][0]
    assert abs(moved["origin"][1] - first["origin"][1]) < 0.6


def test_copy_text_has_no_broken_chars():
    """Text we write must copy/search back as real Thai, in every bundled font."""
    sample = "ที่ น้ำ ปั้น ผู้ใหญ่ ญาติ ฐาน ฎีกา กี่ปี ต่ำ สั่ง ตั๋ว Hello 123"
    for fam in E.BUILTIN_FONTS:
        doc = pymupdf.open()
        E.add_text(doc.new_page(), 72, 100, sample, E.TextStyle(family=fam, size=18))
        reopened = pymupdf.open(stream=doc.tobytes())
        got = reopened[0].get_text().strip()
        assert "�" not in got, fam
        assert normalize_thai(got) == sample, (fam, got)


def test_merge_and_split():
    a = make_doc("ไฟล์ A").tobytes()
    b = make_doc("ไฟล์ B").tobytes()
    merged = E.merge_pdfs([("a.pdf", a), ("b.pdf", b), ("a.pdf", a)])
    assert pymupdf.open(stream=merged).page_count == 3
    parts = E.split_pdf(merged, "ranges", "1-2, 3")
    assert [pymupdf.open(stream=p[1]).page_count for p in parts] == [2, 1]
    parts = E.split_pdf(merged, "every", every=1)
    assert len(parts) == 3
    try:
        E.split_pdf(merged, "ranges", "2-9")
        raise AssertionError("should fail")
    except ValueError:
        pass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
