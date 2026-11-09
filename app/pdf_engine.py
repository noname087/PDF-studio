"""PDF operations: read, edit Thai text, merge, split.

Thai text is drawn with PyMuPDF's HTML engine (Story / insert_htmlbox),
which uses HarfBuzz for OpenType shaping. That is what keeps vowels and
tone marks in the right place (no "floating vowels"). Plain
insert_text()/TextWriter do NOT shape text and should not be used for Thai.
"""

from __future__ import annotations

import html
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from .thai import normalize_thai

FONT_DIR = Path(__file__).parent / "fonts"
USER_FONT_DIR = FONT_DIR / "user"

# family key -> files for each style (falls back to Regular when missing)
BUILTIN_FONTS = {
    "THSarabunNew": {"label": "TH Sarabun New", "files": {
        "regular": "THSarabunNew-Regular.ttf", "bold": "THSarabunNew-Bold.ttf",
        "italic": "THSarabunNew-Italic.ttf", "bolditalic": "THSarabunNew-BoldItalic.ttf"}},
    "Sarabun": {"label": "Sarabun", "files": {
        "regular": "Sarabun-Regular.ttf", "bold": "Sarabun-Bold.ttf",
        "italic": "Sarabun-Italic.ttf", "bolditalic": "Sarabun-BoldItalic.ttf"}},
    "NotoSansThai": {"label": "Noto Sans Thai", "files": {
        "regular": "NotoSansThai-Regular.ttf", "bold": "NotoSansThai-Bold.ttf"}},
    "NotoSerifThai": {"label": "Noto Serif Thai", "files": {
        "regular": "NotoSerifThai-Regular.ttf", "bold": "NotoSerifThai-Bold.ttf"}},
}


# ---------------------------------------------------------------- fonts

def list_fonts() -> list[dict]:
    fonts = [{"key": k, "label": v["label"], "custom": False} for k, v in BUILTIN_FONTS.items()]
    if USER_FONT_DIR.exists():
        for f in sorted(USER_FONT_DIR.glob("*.[ot]tf")):
            fonts.append({"key": "user:" + f.name, "label": f.stem + " (ของฉัน)", "custom": True})
    return fonts


def font_file(family: str, bold: bool, italic: bool) -> Path:
    if family.startswith("user:"):
        p = USER_FONT_DIR / Path(family[5:]).name
        if p.exists():
            return p
        family = "Sarabun"
    info = BUILTIN_FONTS.get(family) or BUILTIN_FONTS["Sarabun"]
    files = info["files"]
    style = ("bold" if bold else "") + ("italic" if italic else "") or "regular"
    for key in (style, "bold" if bold else "regular", "regular"):
        if key in files:
            return FONT_DIR / files[key]
    return FONT_DIR / files["regular"]


# ---------------------------------------------------------------- copy/search fix
#
# HarfBuzz swaps Thai marks for alternate glyphs (e.g. a lower tone mark
# when there is no upper vowel). MuPDF's ToUnicode table only knows glyphs
# that are in the font's cmap, so those alternates come out as "�" when
# someone copies or searches the text. We rebuild the table from the
# font's GSUB rules so every glyph maps back to real Thai characters.

_GLYPH_TEXT_CACHE: dict[str, dict[int, str]] = {}
_PS_NAME_CACHE: dict[str, Path] = {}


def _is_pua(ch: str) -> bool:
    return "" <= ch <= ""


def _glyph_text_map(path: Path) -> dict[int, str]:
    key = str(path)
    if key in _GLYPH_TEXT_CACHE:
        return _GLYPH_TEXT_CACHE[key]
    import unicodedata
    from fontTools.ttLib import TTFont

    f = TTFont(str(path), lazy=True)
    order = f.getGlyphOrder()
    real: dict[str, str] = {}
    pua: dict[str, str] = {}
    for cp, g in sorted(f.getBestCmap().items()):
        ch = chr(cp)
        (pua if _is_pua(ch) else real).setdefault(g, ch)
    derived: dict[str, str] = {}

    def known(g):
        return real.get(g) or derived.get(g)

    def learn(g, text):
        if text and g not in real and g not in derived:
            derived[g] = text
            return True
        return False

    subtables = []
    if "GSUB" in f and f["GSUB"].table.LookupList:
        for lk in f["GSUB"].table.LookupList.Lookup:
            for st in lk.SubTable:
                st = getattr(st, "ExtSubTable", st)
                subtables.append((getattr(st, "LookupType", lk.LookupType), st))
    for _ in range(4):  # repeat: alternates of alternates
        grew = False
        for typ, st in subtables:
            if typ == 1:
                for a, b in st.mapping.items():
                    grew |= learn(b, known(a))
            elif typ == 2:
                for a, outs in st.mapping.items():
                    s = known(a)
                    if not s:
                        continue
                    parts = unicodedata.normalize("NFKD", s)
                    if len(parts) == len(outs):
                        for g, ch in zip(outs, parts):
                            grew |= learn(g, ch)
                    else:
                        grew |= learn(outs[0], s)
            elif typ == 3:
                for a, alts in st.alternates.items():
                    for b in alts:
                        grew |= learn(b, known(a))
            elif typ == 4:
                for first, ligs in st.ligatures.items():
                    for lig in ligs:
                        comps = [known(first)] + [known(c) for c in lig.Component]
                        if all(comps):
                            grew |= learn(lig.LigGlyph, "".join(comps))
        if not grew:
            break

    from .thai import _PUA_MAP
    out: dict[int, str] = {}
    for gid, g in enumerate(order):
        s = real.get(g) or derived.get(g)
        if not s and g in pua:
            s = _PUA_MAP.get(ord(pua[g]), pua[g])
        if s:
            out[gid] = s
    f.close()
    _GLYPH_TEXT_CACHE[key] = out
    return out


def _scan_font_names() -> None:
    from fontTools.ttLib import TTFont
    _PS_NAME_CACHE.clear()
    for p in list(FONT_DIR.glob("*.[ot]tf")) + list(USER_FONT_DIR.glob("*.[ot]tf")):
        try:
            names = TTFont(str(p), lazy=True)["name"]
            for nid in (4, 6):  # full name, PostScript name
                n = names.getDebugName(nid)
                if n:
                    _PS_NAME_CACHE[_name_key(n)] = p
        except Exception:
            pass


def _name_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"^[A-Z]{6}\+", "", name).lower())


def _font_by_ps_name(ps: str) -> Path | None:
    """Find which of our font files an embedded PDF font came from."""
    ps = _name_key(ps)
    if ps not in _PS_NAME_CACHE:
        _scan_font_names()  # maybe a font was uploaded since the last scan
    return _PS_NAME_CACHE.get(ps)


def _tounicode_cmap(m: dict[int, str]) -> bytes:
    def hx(s):
        return s.encode("utf-16-be").hex()
    items = sorted(m.items())
    out = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
           "/CIDSystemInfo <</Registry (Adobe) /Ordering (UCS) /Supplement 0>> def",
           "/CMapName /ThaiPDFStudio-UCS def", "/CMapType 2 def",
           "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange"]
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        out.append(f"{len(chunk)} beginbfchar")
        out += [f"<{gid:04x}> <{hx(s)}>" for gid, s in chunk]
        out.append("endbfchar")
    out += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(out).encode()


def fix_text_mapping(page: pymupdf.Page) -> None:
    """Rewrite ToUnicode of our embedded Thai fonts on this page."""
    doc = page.parent
    for xref, ext, ftype, basefont, *_ in page.get_fonts():
        if ftype != "Type0":
            continue
        path = _font_by_ps_name(basefont)
        if not path:
            continue
        kind, val = doc.xref_get_key(xref, "ToUnicode")
        if kind != "xref":
            continue
        tu = int(val.split()[0])
        if b"ThaiPDFStudio-UCS" in (doc.xref_stream(tu) or b""):
            continue  # already fixed
        doc.update_stream(tu, _tounicode_cmap(_glyph_text_map(path)))


def guess_family(pdf_font_name: str) -> str:
    """Pick the bundled font that looks closest to the font used in the PDF."""
    n = re.sub(r"^[A-Z]{6}\+", "", pdf_font_name or "").lower().replace(" ", "")
    if "sarabun" in n and ("th" in n or "psk" in n or "new" in n or "it" in n):
        return "THSarabunNew"
    if "sarabun" in n:
        return "Sarabun"
    if any(k in n for k in ("angsana", "cordia", "browallia", "eucrosia", "freesia",
                            "iris", "jasmine", "lily", "niramit", "chakra", "kodchasan",
                            "thsarabun", "psk", "th")) and not n.startswith("tahoma"):
        return "THSarabunNew"
    if any(k in n for k in ("times", "serif", "garamond", "georgia", "cambria", "mincho")):
        return "NotoSerifThai"
    if any(k in n for k in ("tahoma", "arial", "helvet", "calibri", "leelawad", "segoe",
                            "noto", "verdana", "prompt", "kanit")):
        return "NotoSansThai"
    return "Sarabun"


# ---------------------------------------------------------------- reading

def _color_hex(c: int) -> str:
    return "#%06x" % (c & 0xFFFFFF)


def page_lines(page: pymupdf.Page) -> list[dict]:
    """Every text line on the page with position and style, Thai cleaned up."""
    flags = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_MEDIABOX_CLIP
    data = page.get_text("dict", flags=flags)
    out = []
    for b in data["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            spans = [s for s in ln["spans"] if s["text"].strip()]
            if not spans:
                continue
            # keep text that reads left-to-right either in the PDF itself
            # ("page" lines) or as shown on screen after page rotation ("view" lines)
            d = pymupdf.Point(ln["dir"]) * page.rotation_matrix - pymupdf.Point(0, 0) * page.rotation_matrix
            if abs(ln["dir"][1]) <= 0.01 and ln["dir"][0] > 0:
                kind = "page"
            elif abs(d.y) <= 0.01 and d.x > 0:
                kind = "view"
            else:
                continue  # vertical / slanted text is not editable
            text = normalize_thai("".join(s["text"] for s in ln["spans"])).strip()
            main = max(spans, key=lambda s: len(s["text"]))
            bbox = pymupdf.Rect(ln["bbox"])
            fontname = main["font"]
            out.append({
                "bbox": [round(v, 2) for v in bbox],
                # where the line appears on screen (differs when the page is rotated)
                "view_bbox": [round(v, 2) for v in (bbox * page.rotation_matrix).normalize()],
                "text": text,
                "size": round(main["size"], 2),
                "color": _color_hex(main["color"]),
                "font": fontname,
                "family": guess_family(fontname),
                "bold": bool(main["flags"] & 16) or "bold" in fontname.lower(),
                "italic": bool(main["flags"] & 2) or "italic" in fontname.lower() or "oblique" in fontname.lower(),
                "origin": [round(v, 2) for v in spans[0]["origin"]],
                "kind": kind,
            })
    return out


def document_text(doc: pymupdf.Document) -> str:
    parts = []
    for page in doc:
        parts.append(f"===== หน้า {page.number + 1} =====")
        parts.append(normalize_thai(page.get_text("text", sort=True)))
    return "\n".join(parts)


# ---------------------------------------------------------------- writing

@dataclass
class TextStyle:
    family: str = "Sarabun"
    size: float = 14
    color: str = "#000000"
    bold: bool = False
    italic: bool = False
    align: str = "left"          # left | center | right
    line_height: float = 1.25


def _html_for(text: str, st: TextStyle, font_path: Path) -> tuple[str, str, pymupdf.Archive]:
    archive = pymupdf.Archive(str(font_path.parent))
    css = (
        "@font-face {font-family: ThaiFont; src: url(%s);}\n"
        "* {margin:0; padding:0;}\n"
        "body {font-family: ThaiFont; font-size: %.2fpt; color: %s; line-height: %.3f;"
        " text-align: %s; font-weight: normal; font-style: normal;}\n"
    ) % (font_path.name, st.size, st.color, st.line_height, st.align)
    lines = [html.escape(ln) or "&nbsp;" for ln in text.split("\n")]
    # keep runs of spaces
    lines = [re.sub(r"  ", " &nbsp;", ln) for ln in lines]
    body = "<p>" + "<br/>".join(lines) + "</p>"
    return body, css, archive


def _measure_width(text: str, st: TextStyle, font_path: Path) -> float:
    font = pymupdf.Font(fontfile=str(font_path))
    return max((font.text_length(ln, fontsize=st.size) for ln in text.split("\n")), default=0)


def _first_baseline(page: pymupdf.Page) -> tuple[float, float] | None:
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            for s in ln["spans"]:
                if s["text"].strip():
                    return s["origin"]
    return None


def draw_text(page: pymupdf.Page, text: str, st: TextStyle, x: float, baseline: float,
              box_width: float | None = None) -> pymupdf.Rect:
    """Draw (shaped) text so its first baseline lands exactly on `baseline`.

    x is the left edge of the text box. box_width is the width available
    for center/right alignment; if the text is wider, the box grows.
    """
    text = normalize_thai(text.replace("\r\n", "\n"))
    font_path = font_file(st.family, st.bold, st.italic)
    need = _measure_width(text, st, font_path) * 1.04 + st.size * 0.5
    width = max(need, box_width or 0)
    page_w = page.rect.width
    if x + width > page_w - 2:  # do not run off the page: wrap instead
        width = max(page_w - 2 - x, st.size * 2)
    body, css, archive = _html_for(text, st, font_path)

    # 1) lay the text out on a scratch page to learn where the baseline ends up
    tmp = pymupdf.open()
    tpage = tmp.new_page(width=page.rect.width, height=max(page.rect.height, 2000))
    probe = pymupdf.Rect(x, 0, x + width, tpage.rect.height)
    spare, _ = tpage.insert_htmlbox(probe, body, css=css, archive=archive, scale_low=1)
    used_h = probe.height - spare if spare >= 0 else probe.height
    origin = _first_baseline(tpage)
    tmp.close()
    dy = baseline - origin[1] if origin else baseline - st.size
    dx = x - origin[0] if (origin and st.align == "left") else 0

    # 2) draw it for real, shifted so the baselines (and left edge) match
    rect = pymupdf.Rect(x + dx, dy, x + dx + width, dy + used_h + st.size)
    page.insert_htmlbox(rect, body, css=css, archive=archive, scale_low=1)
    fix_text_mapping(page)
    return rect


def erase_text(page: pymupdf.Page, rect: pymupdf.Rect, fill: str | None = None) -> None:
    """Really remove text under rect (images and drawings are left alone)."""
    fill_color = False
    if fill:
        h = fill.lstrip("#")
        fill_color = tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    page.add_redact_annot(rect, fill=fill_color, cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)


def _shrink_vert(r: pymupdf.Rect, frac: float = 0.18) -> pymupdf.Rect:
    """Line boxes of neighbouring lines can touch; trim a little top/bottom
    so redaction does not eat letters from the line above or below."""
    d = r.height * frac
    return pymupdf.Rect(r.x0, r.y0 + d, r.x1, r.y1 - d)


def replace_line(page: pymupdf.Page, bbox, origin, new_text: str, st: TextStyle,
                 fill: str | None = None, kind: str = "page", view_bbox=None) -> None:
    rect = pymupdf.Rect(bbox)
    if kind == "view" and view_bbox:
        # text that was added upright on a rotated page
        vr = pymupdf.Rect(view_bbox)
        erase_text(page, view_to_page(page, _shrink_vert(vr)), fill)
        add_text(page, vr.x0, vr.y0, new_text, st)
        return
    erase_text(page, _shrink_vert(rect), fill)
    if new_text.strip():
        x = rect.x0
        if st.align != "left":
            draw_text(page, new_text, st, x, origin[1], box_width=rect.width)
        else:
            draw_text(page, new_text, st, origin[0], origin[1])


def view_to_page(page: pymupdf.Page, rect) -> pymupdf.Rect:
    """Screen (rotated) coordinates -> PDF page coordinates."""
    return (pymupdf.Rect(rect) * page.derotation_matrix).normalize()


def add_text(page: pymupdf.Page, x: float, top: float, text: str, st: TextStyle) -> None:
    """Add text whose top-left corner is at (x, top) in screen coordinates."""
    if not text.strip():
        return
    if page.rotation == 0:
        draw_text(page, text, st, x, top + st.size * 0.8)
        return
    # rotated page: lay the text out upright as the user sees it
    text = normalize_thai(text.replace("\r\n", "\n"))
    font_path = font_file(st.family, st.bold, st.italic)
    vw = page.rect.width
    width = min(_measure_width(text, st, font_path) * 1.04 + st.size * 0.5, max(vw - x - 2, st.size * 2))
    height = (text.count("\n") + 1) * st.size * st.line_height * 1.3 + st.size
    body, css, archive = _html_for(text, st, font_path)
    # learn how far the first line's box sits from the layout box corner,
    # so the visible text starts exactly at (x, top)
    tmp = pymupdf.open()
    tp = tmp.new_page(width=width + 50, height=height + 50)
    tp.insert_htmlbox(pymupdf.Rect(0, 0, width, height), body, css=css, archive=archive, scale_low=1)
    first = next((ln["bbox"] for b in tp.get_text("dict")["blocks"] for ln in b.get("lines", [])), None)
    tmp.close()
    ox, oy = (first[0], first[1]) if first else (0, 0)
    rect = view_to_page(page, (x - ox, top - oy, x - ox + width, top - oy + height))
    page.insert_htmlbox(rect, body, css=css, archive=archive, scale_low=1, rotate=page.rotation)
    fix_text_mapping(page)


# ---------------------------------------------------------------- page / file ops

def parse_ranges(spec: str, page_count: int) -> list[list[int]]:
    """'1-3, 5, 7-' -> [[0,1,2],[4],[6..last]] (0-based)."""
    groups = []
    for part in re.split(r"[,;\s]+", spec.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)", part)
        if m:
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else page_count
        elif part.isdigit():
            a = b = int(part)
        else:
            raise ValueError(f"รูปแบบช่วงหน้าไม่ถูกต้อง: {part}")
        if a < 1 or b > page_count or a > b:
            raise ValueError(f"ช่วงหน้า {part} เกินจำนวนหน้า (มี {page_count} หน้า)")
        groups.append(list(range(a - 1, b)))
    if not groups:
        raise ValueError("กรุณาระบุช่วงหน้า")
    return groups


def merge_pdfs(files: list[tuple[str, bytes]]) -> bytes:
    out = pymupdf.open()
    for _name, data in files:
        with pymupdf.open(stream=data, filetype="pdf") as src:
            out.insert_pdf(src)
    buf = out.tobytes(garbage=3, deflate=True)
    out.close()
    return buf


def split_pdf(data: bytes, mode: str, spec: str = "", every: int = 1) -> list[tuple[str, bytes]]:
    src = pymupdf.open(stream=data, filetype="pdf")
    n = src.page_count
    if mode == "every":
        every = max(1, int(every))
        groups = [list(range(i, min(i + every, n))) for i in range(0, n, every)]
    else:
        groups = parse_ranges(spec, n)
    parts = []
    for g in groups:
        out = pymupdf.open()
        for p in g:
            out.insert_pdf(src, from_page=p, to_page=p)
        label = f"{g[0] + 1}" if len(g) == 1 else f"{g[0] + 1}-{g[-1] + 1}"
        parts.append((f"pages_{label}.pdf", out.tobytes(garbage=3, deflate=True)))
        out.close()
    src.close()
    return parts


def zip_files(files: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            z.writestr(name, data)
    return buf.getvalue()
