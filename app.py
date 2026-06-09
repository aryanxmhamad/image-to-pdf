from flask import Flask, render_template, request, send_file, jsonify
from fpdf import FPDF
from PIL import Image
from werkzeug.utils import secure_filename
import tempfile, os, io, zipfile, threading, time
import pypdf
import ocrmypdf
from pdf2image import convert_from_path

app = Flask(__name__)

# ── Progress tracking (per-request via simple dict keyed by task_id) ──────────
_progress = {}
_progress_lock = threading.Lock()

def set_progress(task_id, value, message=""):
    with _progress_lock:
        _progress[task_id] = {"pct": value, "msg": message}

def clear_progress(task_id):
    with _progress_lock:
        _progress.pop(task_id, None)

# ── Helpers ────────────────────────────────────────────────────────────────────
ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "webp", "tiff"}
ALLOWED_PDF_EXT   = {"pdf"}

def is_image(fn): return "." in fn and fn.rsplit(".",1)[1].lower() in ALLOWED_IMAGE_EXT
def is_pdf(fn):   return "." in fn and fn.rsplit(".",1)[1].lower() in ALLOWED_PDF_EXT

PAGE_SIZES = {
    "A4":     (210,   297),
    "Letter": (215.9, 279.4),
    "Legal":  (215.9, 355.6),
    "A3":     (297,   420),
}

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/progress/<task_id>")
def progress(task_id):
    """SSE endpoint — streams progress updates to the browser."""
    def generate():
        while True:
            with _progress_lock:
                info = _progress.get(task_id, {"pct": 0, "msg": ""})
            yield f"data: {info['pct']}|{info['msg']}\n\n"
            if info["pct"] >= 100:
                break
            time.sleep(0.25)
    return app.response_class(generate(), mimetype="text/event-stream",
                               headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/convert", methods=["POST"])
def convert():
    """Images → PDF (with optional merge + OCR)."""
    task_id   = request.form.get("task_id", "default")
    order     = request.form.get("order", "")
    quality   = int(request.form.get("quality", 85))
    page_size = request.form.get("page_size", "A4")
    do_ocr    = request.form.get("ocr", "false").lower() == "true"
    merge_pdf = request.files.get("merge_pdf")
    files     = request.files.getlist("images")

    if not files or files[0].filename == "":
        return jsonify({"error": "No image files selected"}), 400

    file_map = {secure_filename(f.filename): f for f in files if f.filename}
    if order:
        ordered_names = [n.strip() for n in order.split(",") if n.strip() in file_map]
        for n in file_map:
            if n not in ordered_names:
                ordered_names.append(n)
    else:
        ordered_names = list(file_map.keys())

    pw, ph = PAGE_SIZES.get(page_size, (210, 297))
    pdf    = FPDF(unit="mm", format=[pw, ph])
    margin = 10
    max_w  = pw - 2 * margin
    max_h  = ph - 2 * margin

    temp_files    = []
    output_pdf_path = None
    total = len(ordered_names)

    try:
        set_progress(task_id, 5, "Preparing images…")

        for i, name in enumerate(ordered_names):
            set_progress(task_id, 5 + int(55 * i / total), f"Processing {name}…")
            f    = file_map[name]
            tmp  = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            f.save(tmp.name)
            temp_files.append(tmp.name)

            img = Image.open(tmp.name)
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P": img = img.convert("RGBA")
                bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA","LA") else None)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            img.save(tmp.name, "JPEG", quality=quality, optimize=True)
            iw, ih = img.size
            ratio  = min(max_w / (iw * 0.264583), max_h / (ih * 0.264583))
            dw     = iw * 0.264583 * ratio
            dh     = ih * 0.264583 * ratio
            x = margin + (max_w - dw) / 2
            y = margin + (max_h - dh) / 2

            pdf.add_page()
            pdf.image(tmp.name, x=x, y=y, w=dw, h=dh)

        set_progress(task_id, 62, "Saving PDF…")
        img_pdf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        img_pdf_path = img_pdf.name
        img_pdf.close()
        temp_files.append(img_pdf_path)
        pdf.output(img_pdf_path)

        # ── Merge ──────────────────────────────────────────────
        if merge_pdf and merge_pdf.filename and is_pdf(merge_pdf.filename):
            set_progress(task_id, 68, "Merging PDFs…")
            m_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            merge_pdf.save(m_tmp.name)
            m_tmp.close()
            temp_files.append(m_tmp.name)

            writer = pypdf.PdfWriter()
            for page in pypdf.PdfReader(m_tmp.name).pages:
                writer.add_page(page)
            for page in pypdf.PdfReader(img_pdf_path).pages:
                writer.add_page(page)

            merged = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            merged.close()
            temp_files.append(merged.name)
            with open(merged.name, "wb") as out:
                writer.write(out)
            working_path = merged.name
        else:
            working_path = img_pdf_path

        # ── OCR ────────────────────────────────────────────────
        if do_ocr:
            set_progress(task_id, 75, "Running OCR (this may take a moment)…")
            ocr_out = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            ocr_out.close()
            temp_files.append(ocr_out.name)
            try:
                ocrmypdf.ocr(working_path, ocr_out.name,
                             skip_text=True, optimize=1, progress_bar=False)
                working_path = ocr_out.name
            except Exception as e:
                # OCR failed — return non-OCR pdf with a warning header
                pass

        set_progress(task_id, 95, "Almost done…")
        output_pdf_path = working_path
        set_progress(task_id, 100, "Done!")

        return send_file(output_pdf_path, as_attachment=True,
                         download_name="converted.pdf", mimetype="application/pdf")

    finally:
        for f in temp_files:
            if f != output_pdf_path:
                try:
                    if os.path.exists(f): os.remove(f)
                except Exception: pass
        # Clean up output after send (Flask handles this for send_file)
        threading.Timer(30, lambda: os.remove(output_pdf_path)
                        if output_pdf_path and os.path.exists(output_pdf_path) else None).start()
        clear_progress(task_id)


@app.route("/split", methods=["POST"])
def split_pdf():
    """PDF → ZIP of JPEG images (one per page), sorted by page."""
    task_id   = request.form.get("task_id", "split_default")
    pdf_file  = request.files.get("split_pdf")
    dpi       = int(request.form.get("dpi", 150))
    sort_order= request.form.get("sort", "asc")   # "asc" or "desc"

    if not pdf_file or not pdf_file.filename:
        return jsonify({"error": "No PDF file provided"}), 400

    if not is_pdf(pdf_file.filename):
        return jsonify({"error": "File must be a PDF"}), 400

    temp_files = []
    try:
        set_progress(task_id, 5, "Reading PDF…")
        pdf_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        pdf_file.save(pdf_tmp.name)
        pdf_tmp.close()
        temp_files.append(pdf_tmp.name)

        set_progress(task_id, 15, "Converting pages to images…")
        images = convert_from_path(pdf_tmp.name, dpi=dpi)

        if sort_order == "desc":
            images = list(reversed(images))

        # Build ZIP in memory
        zip_buf = io.BytesIO()
        total   = len(images)
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, img in enumerate(images):
                set_progress(task_id, 20 + int(75 * i / total),
                             f"Saving page {i+1} of {total}…")
                page_num = (total - i) if sort_order == "desc" else (i + 1)
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=88, optimize=True)
                zf.writestr(f"page_{page_num:03d}.jpg", buf.getvalue())

        set_progress(task_id, 100, "Done!")
        zip_buf.seek(0)
        return send_file(zip_buf, as_attachment=True,
                         download_name="pdf_pages.zip", mimetype="application/zip")

    finally:
        for f in temp_files:
            try:
                if os.path.exists(f): os.remove(f)
            except Exception: pass
        clear_progress(task_id)


if __name__ == "__main__":
    app.run(debug=True)
