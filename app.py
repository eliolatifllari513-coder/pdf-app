import os
import re
import fitz  # PyMuPDF
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)

UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

def format_label(field_name):
    """Krijon një emër të lexueshëm nga emri teknik i fushës në PDF"""
    clean = re.sub(r'topmostSubform\[\d+\]|Page\d+\[\d+\]|ReadOrder\[\d+\]|\w+\[\d+\]', '', field_name)
    clean = clean.replace('.', ' ').replace('_', ' ').replace('-', ' ').strip()
    
    if not clean or len(clean) < 2:
        return field_name
    return clean.title()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/extract-fields", methods=["POST"])
def extract_fields():
    if 'file' not in request.files:
        return jsonify({"error": "Nuk u zgjodh asnjë skedar."}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "Skedar i pavlefshëm."}), 400

    filepath = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(filepath)

    try:
        doc = fitz.open(filepath)
        field_list = []
        seen_names = set()
        count = 0

        # Kontrollojmë të gjitha faqet për fusha interaktive
        for page_idx, page in enumerate(doc):
            for widget in page.widgets():
                fname = widget.field_name
                if fname and fname not in seen_names:
                    seen_names.add(fname)
                    count += 1
                    
                    # Caktojmë një label të lexueshëm
                    label = format_label(fname)
                    
                    field_list.append({
                        "id": fname,
                        "key": label,
                        "section": "general",
                        "page": page_idx + 1
                    })

        doc.close()

        # Nëse PDF nuk ka fusha interaktive (eshte PDF statike ose e skanuar)
        is_interactive = True
        if not field_list:
            is_interactive = False
            field_list = [
                {"id": "custom_name", "key": "Emri dhe Mbiemri / Full Name", "section": "general"},
                {"id": "custom_date", "key": "Data / Date", "section": "general"},
                {"id": "custom_text", "key": "Teksti / Shenime", "section": "general"}
            ]

        return jsonify({
            "success": True,
            "filename": os.path.basename(filepath),
            "is_interactive": is_interactive,
            "fields": field_list
        })

    except Exception as e:
        return jsonify({"error": f"Gabim gjatë leximit të PDF: {str(e)}"}), 500

@app.route("/fill-pdf", methods=["POST"])
def fill_pdf():
    data = request.json
    filename = data.get("filename")
    user_inputs = data.get("inputs", {})

    input_path = os.path.join(UPLOAD_FOLDER, filename)
    output_filename = f"completed_{filename}"
    output_path = os.path.join(OUTPUT_FOLDER, output_filename)

    try:
        doc = fitz.open(input_path)
        filled_any = False

        # 1. Plotësojmë fushat interaktive (AcroForms)
        for page in doc:
            for widget in page.widgets():
                fname = widget.field_name
                if fname in user_inputs and user_inputs[fname]:
                    widget.field_value = str(user_inputs[fname])
                    widget.update()
                    filled_any = True

        # 2. Nëse PDF nuk kishte fusha interaktive, i shkruajmë tekstet ne krye te faqes se pare
        if not filled_any:
            page = doc[0]
            y_pos = 70
            for key, val in user_inputs.items():
                if val.strip():
                    page.insert_text((50, y_pos), f"{val}", fontsize=11, color=(0, 0, 0))
                    y_pos += 25

        doc.save(output_path)
        doc.close()

        return jsonify({
            "success": True,
            "download_url": f"/download/{output_filename}"
        })

    except Exception as e:
        return jsonify({"error": f"Gabim gjatë plotësimit: {str(e)}"}), 500

@app.route("/download/<filename>")
def download(filename):
    file_path = os.path.join(OUTPUT_FOLDER, filename)
    return send_file(file_path, as_attachment=True)

if __name__ == "__main__":
    app.run(debug=True)
