import os
import re
import fitz  # PyMuPDF
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)

UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Hartëzimi i fushave të W-9 me çelësa unikë për përkthim në frontend
W9_FIELD_MAPPING = {
    "f1_01": {"key": "name", "section": "personal"},
    "f1_02": {"key": "businessName", "section": "personal"},
    "f1_03": {"key": "llcCode", "section": "tax"},
    "f1_04": {"key": "taxExemptCode", "section": "tax"},
    "f1_05": {"key": "fatcaCode", "section": "tax"},
    "f1_06": {"key": "address", "section": "address"},
    "f1_07": {"key": "cityStateZip", "section": "address"},
    "f1_08": {"key": "requesterInfo", "section": "address"},
    "f1_09": {"key": "accountNumber", "section": "identification"},
    "f1_10": {"key": "ssn1", "section": "identification"},
    "f1_11": {"key": "ssn2", "section": "identification"},
    "f1_12": {"key": "ssn3", "section": "identification"},
    "f1_13": {"key": "ein1", "section": "identification"},
    "f1_14": {"key": "ein2", "section": "identification"},
}

def parse_widget_info(raw_name, index):
    for field_id, info in W9_FIELD_MAPPING.items():
        if field_id in raw_name:
            return info["key"], info["section"]

    clean = re.sub(r'topmostSubform\[\d+\]|Page\d+\[\d+\]|ReadOrder\[\d+\]|\w+\[\d+\]', '', raw_name)
    clean = clean.replace('.', ' ').replace('_', ' ').strip()

    if "Boxes3a" in raw_name or "c1" in raw_name:
        return f"option_{index}", "tax"

    if not clean or len(clean) < 3 or clean.startswith("f1"):
        return f"field_{index}", "general"

    return clean, "general"

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
        
        if not doc.is_pdf:
            pdf_bytes = doc.convert_to_pdf()
            doc.close()
            doc = fitz.open("pdf", pdf_bytes)
            filepath = filepath.rsplit('.', 1)[0] + ".pdf"
            doc.save(filepath)

        field_list = []
        count = 0

        for page in doc:
            for widget in page.widgets():
                if widget.field_name:
                    count += 1
                    raw_name = widget.field_name
                    field_key, section = parse_widget_info(raw_name, count)
                    
                    field_list.append({
                        "id": raw_name,
                        "key": field_key,
                        "section": section
                    })

        doc.close()

        if not field_list:
            field_list = [
                {"id": "text_1", "key": "name", "section": "personal"},
                {"id": "text_2", "key": "address", "section": "address"},
                {"id": "text_3", "key": "ssn", "section": "identification"}
            ]

        return jsonify({
            "success": True,
            "filename": os.path.basename(filepath),
            "fields": field_list
        })

    except Exception as e:
        return jsonify({"error": f"Gabim gjatë leximit: {str(e)}"}), 500

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

        filled_widgets = False
        for p in doc:
            for widget in p.widgets():
                if widget.field_name in user_inputs:
                    widget.field_value = str(user_inputs[widget.field_name])
                    widget.update()
                    filled_widgets = True

        if not filled_widgets:
            page = doc[0]
            rect = page.rect
            height = rect.height

            if user_inputs.get("text_1"):
                page.insert_text((50, 100), user_inputs["text_1"], fontsize=14, color=(0, 0, 0))
            if user_inputs.get("text_2"):
                page.insert_text((50, height / 2), user_inputs["text_2"], fontsize=14, color=(0, 0, 0))
            if user_inputs.get("text_3"):
                page.insert_text((50, height - 100), user_inputs["text_3"], fontsize=14, color=(0, 0, 0))

        doc.save(output_path)
        doc.close()

        return jsonify({
            "success": True,
            "download_url": f"/download/{output_filename}"
        })

    except Exception as e:
        return jsonify({"error": f"Gabim gjatë përpunimit: {str(e)}"}), 500

@app.route("/download/<filename>")
def download(filename):
    file_path = os.path.join(OUTPUT_FOLDER, filename)
    return send_file(file_path, as_attachment=True)

if __name__ == "__main__":
    app.run(debug=True)
