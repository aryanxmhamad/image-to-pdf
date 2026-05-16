from flask import Flask, render_template, request, send_file
from fpdf import FPDF
from PIL import Image
from werkzeug.utils import secure_filename
import tempfile
import os

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/convert", methods=["POST"])
def convert():
    files = request.files.getlist("images")

    if not files or files[0].filename == "":
        return "No files selected"

    pdf = FPDF()

    temp_files = []

    try:
        for file in files:
            filename = secure_filename(file.filename)

            # Save temporarily
            temp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            file.save(temp.name)
            temp_files.append(temp.name)

            # Open image for scaling
            img = Image.open(temp.name)
            width, height = img.size

            pdf.add_page()

            # Fit image nicely inside page
            max_width = 190
            new_height = (max_width * height) / width

            if new_height > 260:
                new_height = 260
                max_width = (new_height * width) / height

            pdf.image(temp.name, x=10, y=20, w=max_width, h=new_height)

        # Create temporary output PDF
        output_pdf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        pdf.output(output_pdf.name)

        return send_file(
            output_pdf.name,
            as_attachment=True,
            download_name="converted.pdf"
        )

    finally:
        # Auto cleanup
        for f in temp_files:
            if os.path.exists(f):
                os.remove(f)

if __name__ == "__main__":
    app.run()