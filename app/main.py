"""Thai PDF Studio - web server.

Run:  python -m uvicorn app.main:app --port 8000
Then open http://localhost:8000

Settings come from environment variables (see README, "ตั้งค่า").
Documents live in memory, so run ONE worker process only.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import pdf_engine as E

BASE = Path(__file__).parent


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


MAX_UPLOAD_MB = _env_num("MAX_UPLOAD_MB", 30)       # per PDF file
MAX_MERGE_MB = _env_num("MAX_MERGE_MB", 80)         # all files in one merge
MAX_MERGE_FILES = int(_env_num("MAX_MERGE_FILES", 20))
MAX_PAGES = int(_env_num("MAX_PAGES", 300))
MAX_DOCS = int(_env_num("MAX_DOCS", 20))            # open documents kept in memory
DOC_TTL_MIN = _env_num("DOC_TTL_MINUTES", 60)       # forget idle documents after this
MAX_UNDO = int(_env_num("MAX_UNDO", 15))
MAX_UNDO_MB = _env_num("MAX_UNDO_MB", 60)           # undo history per document
MAX_FONT_MB = _env_num("MAX_FONT_MB", 15)
FONT_TTL_DAYS = _env_num("FONT_TTL_DAYS", 7)        # delete uploaded fonts after this
MAX_PIXELS = 20_000_000                              # biggest page image we render
SOURCE_URL = os.environ.get("SOURCE_URL", "https://github.com/noname087/PDF-studio")

app = FastAPI(title="Thai PDF Studio")


# ---------------------------------------------------------------- doc store

class Doc:
    def __init__(self, name: str, data: bytes):
        self.id = uuid.uuid4().hex
        self.name = Path(name).name[:200] or "document.pdf"
        self.doc = pymupdf.open(stream=data, filetype="pdf")
        if self.doc.needs_pass:
            raise HTTPException(400, "ไฟล์นี้มีรหัสผ่าน กรุณาปลดล็อกก่อน")
        if not self.doc.is_pdf:
            raise HTTPException(400, "ไม่ใช่ไฟล์ PDF")
        if self.doc.page_count > MAX_PAGES:
            raise HTTPException(413, f"ไฟล์มี {self.doc.page_count} หน้า เกินขีดจำกัด {MAX_PAGES} หน้า")
        self.undo: list[bytes] = []
        self.version = 0
        self.lock = threading.Lock()
        self.touched = time.time()

    def snapshot(self):
        self.undo.append(self.doc.tobytes())
        del self.undo[:-MAX_UNDO]
        # keep undo history under the memory budget (always keep the latest step)
        while len(self.undo) > 1 and sum(map(len, self.undo)) > MAX_UNDO_MB * 1024 * 1024:
            self.undo.pop(0)

    def changed(self):
        self.version += 1
        self.touched = time.time()

    def info(self) -> dict:
        return {
            "id": self.id, "name": self.name, "version": self.version,
            "canUndo": bool(self.undo),
            "pages": [{"w": round(p.rect.width, 2), "h": round(p.rect.height, 2),
                       "hasText": bool(p.get_text("text").strip())} for p in self.doc],
        }


DOCS: dict[str, Doc] = {}
DOCS_LOCK = threading.Lock()


def _forget(doc_id: str) -> None:
    d = DOCS.pop(doc_id, None)
    if d:
        with d.lock:
            d.doc.close()
            d.undo.clear()


def cleanup() -> None:
    """Drop idle documents and old uploaded fonts."""
    now = time.time()
    with DOCS_LOCK:
        for k in [k for k, v in DOCS.items() if now - v.touched > DOC_TTL_MIN * 60]:
            _forget(k)
    if E.USER_FONT_DIR.exists():
        for d in E.USER_FONT_DIR.iterdir():
            try:
                if d.is_dir() and now - d.stat().st_mtime > FONT_TTL_DAYS * 86400:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


def _cleanup_loop():
    while True:
        time.sleep(300)
        try:
            cleanup()
        except Exception:
            pass


threading.Thread(target=_cleanup_loop, daemon=True).start()


async def read_upload(f: UploadFile, limit_mb: float, what: str = "ไฟล์") -> bytes:
    """Read an upload but stop as soon as it is bigger than the limit."""
    limit = int(limit_mb * 1024 * 1024)
    chunks, size = [], 0
    while chunk := await f.read(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"{what}ใหญ่เกิน {limit_mb:g} MB")
        chunks.append(chunk)
    return b"".join(chunks)


def get_doc(doc_id: str) -> Doc:
    d = DOCS.get(doc_id)
    if not d:
        raise HTTPException(404, "ไม่พบเอกสาร (อาจหมดอายุ กรุณาเปิดไฟล์ใหม่)")
    d.touched = time.time()
    return d


def add_doc(name: str, data: bytes) -> Doc:
    cleanup()
    try:
        d = Doc(name, data)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "เปิดไฟล์ PDF ไม่ได้ ไฟล์อาจเสียหาย")
    with DOCS_LOCK:
        # too many open documents: forget the ones idle the longest
        while len(DOCS) >= MAX_DOCS:
            _forget(min(DOCS.values(), key=lambda x: x.touched).id)
        DOCS[d.id] = d
    return d


def page_of(d: Doc, n: int) -> pymupdf.Page:
    if not 0 <= n < d.doc.page_count:
        raise HTTPException(404, "ไม่พบหน้านี้")
    return d.doc[n]


# ---------------------------------------------------------------- models

class Style(BaseModel):
    family: str = "Sarabun"
    size: float = 16
    color: str = "#000000"
    bold: bool = False
    italic: bool = False
    align: str = "left"
    line_height: float = 1.25

    def to_engine(self) -> E.TextStyle:
        return E.TextStyle(family=self.family, size=max(2.0, min(self.size, 400.0)),
                           color=self.color, bold=self.bold, italic=self.italic,
                           align=self.align if self.align in ("left", "center", "right") else "left",
                           line_height=self.line_height)


class ReplaceReq(BaseModel):
    bbox: list[float]
    origin: list[float]
    kind: str = "page"
    view_bbox: list[float] | None = None
    text: str = Field(max_length=5000)
    style: Style
    fill: str | None = None


class AddReq(BaseModel):
    x: float
    y: float          # top of the text
    text: str = Field(max_length=5000)
    style: Style


class EraseReq(BaseModel):
    bbox: list[float]
    fill: str | None = None


class RotateReq(BaseModel):
    degrees: int = 90


class MoveReq(BaseModel):
    order: list[int]


class SplitReq(BaseModel):
    mode: str = "ranges"   # ranges | every | each
    ranges: str = ""
    every: int = 1


# ---------------------------------------------------------------- api: fonts

@app.get("/api/health")
def health():
    return {"ok": True, "docs": len(DOCS)}


@app.get("/api/config")
def config():
    return {"maxUploadMB": MAX_UPLOAD_MB, "maxMergeMB": MAX_MERGE_MB, "maxPages": MAX_PAGES,
            "docTtlMinutes": DOC_TTL_MIN, "sourceUrl": SOURCE_URL}


@app.get("/api/fonts")
def fonts(client: str = ""):
    return E.list_fonts(client)


@app.post("/api/fonts")
async def upload_font(file: UploadFile = File(...), client: str = Form(...)):
    folder = E.client_font_dir(client)
    if not folder:
        raise HTTPException(400, "รหัสผู้ใช้ไม่ถูกต้อง")
    name = re.sub(r"[^\w.\- ]", "_", Path(file.filename or "font.ttf").name)[:100]
    if not name.lower().endswith((".ttf", ".otf")):
        raise HTTPException(400, "รองรับเฉพาะไฟล์ .ttf หรือ .otf")
    data = await read_upload(file, MAX_FONT_MB, "ไฟล์ฟอนต์")
    try:
        pymupdf.Font(fontbuffer=data)
    except Exception:
        raise HTTPException(400, "ไฟล์ฟอนต์เสียหายหรือไม่รองรับ")
    folder.mkdir(parents=True, exist_ok=True)
    if len(list(folder.glob("*"))) >= 10:
        raise HTTPException(400, "เพิ่มฟอนต์ได้สูงสุด 10 ไฟล์")
    (folder / name).write_bytes(data)
    return E.list_fonts(client)


@app.get("/fontfile/{key:path}")
def font_binary(key: str, bold: bool = False, italic: bool = False):
    """Serve font files so the browser preview uses the same font as the PDF."""
    return FileResponse(E.font_file(key, bold, italic), media_type="font/ttf")


# ---------------------------------------------------------------- api: documents

@app.post("/api/docs")
async def open_doc(file: UploadFile = File(...)):
    data = await read_upload(file, MAX_UPLOAD_MB)
    d = add_doc(file.filename or "document.pdf", data)
    return d.info()


@app.get("/api/docs/{doc_id}")
def doc_info(doc_id: str):
    return get_doc(doc_id).info()


@app.get("/api/docs/{doc_id}/pages/{n}/image")
def page_image(doc_id: str, n: int, scale: float = 1.5, v: int = 0):
    d = get_doc(doc_id)
    with d.lock:
        page = page_of(d, n)
        scale = max(0.1, min(scale, 5))
        r = page.rect
        if r.width * r.height * scale * scale > MAX_PIXELS:  # huge page: render smaller
            scale = (MAX_PIXELS / (r.width * r.height)) ** 0.5
        png = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/docs/{doc_id}/pages/{n}/lines")
def page_lines(doc_id: str, n: int):
    d = get_doc(doc_id)
    with d.lock:
        return E.page_lines(page_of(d, n))


def _edit(doc_id: str, n: int, fn):
    d = get_doc(doc_id)
    with d.lock:
        page = page_of(d, n)
        d.snapshot()
        try:
            fn(page)
        except HTTPException:
            raise
        except Exception as e:  # roll back on failure
            d.doc.close()
            d.doc = pymupdf.open(stream=d.undo.pop(), filetype="pdf")
            raise HTTPException(500, f"แก้ไขไม่สำเร็จ: {e}")
        d.changed()
        return d.info()


@app.post("/api/docs/{doc_id}/pages/{n}/replace")
def replace_text(doc_id: str, n: int, req: ReplaceReq):
    return _edit(doc_id, n, lambda p: E.replace_line(
        p, req.bbox, req.origin, req.text, req.style.to_engine(), req.fill, req.kind, req.view_bbox))


@app.post("/api/docs/{doc_id}/pages/{n}/add")
def add_text(doc_id: str, n: int, req: AddReq):
    st = req.style.to_engine()
    # (x, y) = where the user clicked: the top-left corner of the new text
    return _edit(doc_id, n, lambda p: E.add_text(p, req.x, req.y, req.text, st))


@app.post("/api/docs/{doc_id}/pages/{n}/erase")
def erase(doc_id: str, n: int, req: EraseReq):
    return _edit(doc_id, n, lambda p: E.erase_text(p, E.view_to_page(p, req.bbox), req.fill))


@app.post("/api/docs/{doc_id}/pages/{n}/rotate")
def rotate(doc_id: str, n: int, req: RotateReq):
    return _edit(doc_id, n, lambda p: p.set_rotation((p.rotation + req.degrees) % 360))


@app.delete("/api/docs/{doc_id}/pages/{n}")
def delete_page(doc_id: str, n: int):
    d = get_doc(doc_id)
    with d.lock:
        page_of(d, n)
        if d.doc.page_count == 1:
            raise HTTPException(400, "ลบหน้าสุดท้ายไม่ได้")
        d.snapshot()
        d.doc.delete_page(n)
        d.changed()
        return d.info()


@app.post("/api/docs/{doc_id}/reorder")
def reorder(doc_id: str, req: MoveReq):
    d = get_doc(doc_id)
    with d.lock:
        if sorted(req.order) != list(range(d.doc.page_count)):
            raise HTTPException(400, "ลำดับหน้าไม่ถูกต้อง")
        d.snapshot()
        d.doc.select(req.order)
        d.changed()
        return d.info()


@app.post("/api/docs/{doc_id}/undo")
def undo(doc_id: str):
    d = get_doc(doc_id)
    with d.lock:
        if not d.undo:
            raise HTTPException(400, "ไม่มีอะไรให้ย้อนกลับ")
        d.doc.close()
        d.doc = pymupdf.open(stream=d.undo.pop(), filetype="pdf")
        d.changed()
        return d.info()


def _download_name(name: str, suffix: str, ext: str) -> str:
    stem = Path(name).stem or "document"
    return f"{stem}{suffix}.{ext}"


def _attachment(data: bytes, filename: str, media: str) -> Response:
    return Response(data, media_type=media, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})


@app.get("/api/docs/{doc_id}/download")
def download(doc_id: str):
    d = get_doc(doc_id)
    with d.lock:
        data = d.doc.tobytes(garbage=3, deflate=True, clean=False)
    suffix = "_edited" if d.version else ""
    return _attachment(data, _download_name(d.name, suffix, "pdf"), "application/pdf")


@app.get("/api/docs/{doc_id}/text")
def export_text(doc_id: str):
    d = get_doc(doc_id)
    with d.lock:
        txt = E.document_text(d.doc)
    return _attachment(txt.encode("utf-8-sig"), _download_name(d.name, "", "txt"), "text/plain; charset=utf-8")


@app.post("/api/docs/{doc_id}/split")
def split(doc_id: str, req: SplitReq):
    d = get_doc(doc_id)
    with d.lock:
        data = d.doc.tobytes()
    try:
        if req.mode == "each":
            parts = E.split_pdf(data, "every", every=1)
        else:
            parts = E.split_pdf(data, req.mode, req.ranges, req.every)
    except ValueError as e:
        raise HTTPException(400, str(e))
    stem = Path(d.name).stem
    parts = [(f"{stem}_{n}", b) for n, b in parts]
    if len(parts) == 1:
        return _attachment(parts[0][1], parts[0][0], "application/pdf")
    return _attachment(E.zip_files(parts), f"{stem}_split.zip", "application/zip")


@app.post("/api/merge")
async def merge(files: list[UploadFile] = File(...)):
    if len(files) < 1:
        raise HTTPException(400, "กรุณาเลือกไฟล์อย่างน้อย 1 ไฟล์")
    if len(files) > MAX_MERGE_FILES:
        raise HTTPException(400, f"รวมได้ครั้งละไม่เกิน {MAX_MERGE_FILES} ไฟล์")
    items, total = [], 0
    for f in files:
        data = await read_upload(f, MAX_UPLOAD_MB, f"ไฟล์ {f.filename} ")
        total += len(data)
        if total > MAX_MERGE_MB * 1024 * 1024:
            raise HTTPException(413, f"ไฟล์รวมกันใหญ่เกิน {MAX_MERGE_MB:g} MB")
        items.append((f.filename, data))
    try:
        merged = E.merge_pdfs(items)
    except Exception:
        raise HTTPException(400, "รวมไฟล์ไม่สำเร็จ มีไฟล์ที่เปิดไม่ได้หรือมีรหัสผ่าน")
    d = add_doc("merged.pdf", merged)
    return d.info()


@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/docs"):
        resp.headers["Cache-Control"] = "private, no-store" if "/image" not in request.url.path \
            else "private, max-age=3600"
    return resp


@app.exception_handler(HTTPException)
async def http_err(_req, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
