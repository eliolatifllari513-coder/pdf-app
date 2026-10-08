import os
import re
import fitz  # PyMuPDF
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)

UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Emërtimet e sakta në shqip për fushat e formës W-9
W9_LABELS = {
    "f1_01": "1. Emri dhe Mbiemri (Name of entity/individual)",
    "f1_02": "2. Emri i Biznesit (Business name / disregarded entity)",
    "f1_03": "3a. Kodi i klasifikimit LLC (C, S, ose P)",
    "f1_04": "4. Kodi i përjashtimit nga taksat (Exempt payee code)",
    "f1_05": "4. Kodi i përjashtimit FATCA",
    "f1_06": "5. Adresa (Rruga, Ndërtesa, Hyrja)",
    "f1_07": "6. Qyteti, Shteti dhe Kodi Postar",
    "f1_08": "Emri dhe adresa e kërkuesit (Opsionale)",
    "f1_09": "7. Numri i llogarisë (Opsionale)",
    "f1_10": "SSN - Numri i Sigurimit Shoqëror (pjesa 1)",
    "f1_11": "SSN - Numri i Sigurimit Shoqëror (pjesa 2)",
    "f1_12": "SSN - Numri i Sigurimit Shoqëror (pjesa 3)",
    "f1_13": "EIN - Numri i Identifikimit të Biznesit (pjesa 1)",
    "f1_14": "EIN - Numri i Identifikimit të Biznesit (pjesa 2)",
}

def get_clean_label(raw_name, index):
    # Kontrollojmë nëse përputhet me emrat e fushave të W-9
    for key, human_label in W9_LABELS.items():
        if key in raw_name:
            return human_label

    # Pastrim i përgjithshëm për PDF-të e tjera
    clean = re.sub(r'topmostSubform\[\d+\]|Page\d+\[\d+\]|ReadOrder\[\d+\]|\w+\[\d+\]', '', raw_name)
    clean = clean.replace('.', ' ').replace('_', ' ').strip()

    if "Boxes3a" in raw_name or "c1" in raw_name:
        return f"Kutia e zgjedhjes (Option {index})"

    if not clean or len(clean) < 3 or clean.startswith("f1"):
        return f"Fusha {index}"

    return clean.capitalize()

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
                    label = get_clean_label(raw_name, count)
                    
                    field_list.append({
                        "id": raw_name,
                        "label": label
                    })

        doc.close()

        if not field_list:
            field_list = [
                {"id": "text_1", "label": "Emri dhe Mbiemri"},
                {"id": "text_2", "label": "Adresa"},
                {"id": "text_3", "label": "NIF / Numri i Identifikimit"}
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
