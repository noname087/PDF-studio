"""Create Thai sample PDFs for testing (one via Chromium, one via PyMuPDF)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples"
OUT.mkdir(exist_ok=True)
FONTS = ROOT / "app" / "fonts"

HTML = f"""<!doctype html><html><head><meta charset="utf-8"><style>
@font-face {{ font-family: TH; src: url('file://{FONTS}/THSarabunNew-Regular.ttf'); }}
@font-face {{ font-family: TH; font-weight: bold; src: url('file://{FONTS}/THSarabunNew-Bold.ttf'); }}
body {{ font-family: TH; font-size: 16pt; margin: 2cm; }}
h1 {{ font-size: 22pt; }}
.box {{ background: #fdf3c4; padding: 6pt; }}
</style></head><body>
<h1>บันทึกข้อความ</h1>
<p>ส่วนราชการ สำนักงานเขตพื้นที่การศึกษาประถมศึกษา</p>
<p>เรื่อง ขออนุมัติเดินทางไปราชการ ณ จังหวัดเชียงใหม่</p>
<p>เรียน ผู้อำนวยการโรงเรียน</p>
<p class="box">ด้วยข้าพเจ้าได้รับมอบหมายให้เข้าร่วมประชุมเชิงปฏิบัติการ ในวันที่ ๑๕ พฤศจิกายน ๒๕๖๙</p>
<p>คำที่มีสระซ้อน: น้ำ ปู่ ผู้ใหญ่ ที่นี่ กี่ปี ป่าไม้ ฎีกา ฐาน ญาติ ปั้น สั่ง ตั๋ว</p>
<p>จึงเรียนมาเพื่อโปรดพิจารณาอนุมัติ</p>
</body></html>"""

def chromium_pdf():
    """Print the HTML to PDF with headless Chromium (needs Node + playwright)."""
    import subprocess
    html_path = OUT / "memo.html"
    html_path.write_text(HTML, encoding="utf-8")
    js = ("const {chromium}=require('playwright');(async()=>{const b=await chromium.launch();"
          "const p=await b.newPage();await p.goto(process.argv[1]);"
          "await p.pdf({path:process.argv[2],format:'A4',printBackground:true});await b.close();})();")
    subprocess.run(["node", "-e", js, html_path.as_uri(), str(OUT / "thai_memo.pdf")], check=True)
    html_path.unlink()

def multipage_pdf():
    import pymupdf
    doc = pymupdf.open()
    css = f"@font-face {{font-family: S; src: url(Sarabun-Regular.ttf);}} body {{font-family: S; font-size: 28pt;}}"
    arch = pymupdf.Archive(str(FONTS))
    for i in range(1, 6):
        pg = doc.new_page()
        pg.insert_htmlbox(pymupdf.Rect(72, 72, 520, 300), f"<p>หน้าที่ {i} ทดสอบการแยกไฟล์</p>", css=css, archive=arch)
    doc.save(OUT / "five_pages.pdf")

if __name__ == "__main__":
    chromium_pdf()
    multipage_pdf()
    print("ok", list(OUT.iterdir()))
