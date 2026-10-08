import os
import re
import fitz  # PyMuPDF
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)

UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Harta e fushave inteligjente per te identifikuar cfare kerkon PDF-ja
FIELD_MAPPING_RULES = [
    # (Keywords te mundshme ne PDF, Label per perdoruesin, Kategoria)
    (['name', 'full_name', 'first', 'last', 'emri', 'mbiemri', 'applicant'], 'Emri dhe Mbiemri / Full Name', 'personal'),
    (['business', 'company', 'companyname', 'emri_biznesit', 'firm'], 'Emri i Biznesit / Business Name', 'personal'),
    (['address', 'street', 'adresa', 'street_address', 'line1'], 'Adresa (Rruga, Ndërtesa)', 'address'),
    (['city', 'town', 'qyteti'], 'Qyteti / City', 'address'),
    (['state', 'province', 'shteti'], 'Shteti / State / Rajoni', 'address'),
    (['zip', 'postal', 'kodi_postar', 'zipcode'], 'Kodi Postar / ZIP Code', 'address'),
    (['ssn', 'social', 'ssn1', 'ssn2', 'ssn3'], 'Numri i Sigurimit Shoqëror (SSN)', 'identification'),
    (['ein', 'employer', 'tin', 'nipt', 'tax_id', 'vat'], 'Numri i Identifikimit Tatimor (EIN / TIN / NIPT)', 'identification'),
    (['date', 'data', 'dt'], 'Data / Date', 'general'),
    (['email', 'e-mail'], 'E-mail', 'personal'),
    (['phone', 'mobile', 'tel', 'celular'], 'Numri i Telefonit / Phone', 'personal'),
    (['account', 'bank', 'llogari'], 'Numri i Llogarisë / Account Number', 'general')
]

def map_field_to_smart_label(raw_name):
    """Përkthen fushën teknike të PDF-së në një pyetje të qartë për përdoruesin"""
    clean_name = raw_name.lower().replace('.', '_').replace('-', '_')
    
    for keywords, human_label, category in FIELD_MAPPING_RULES:
        for kw in keywords:
            if kw in clean_name:
                return human_label, category
                
    # Nëse nuk gjen përputhje me rregullat, krijon një emër të pastër
    clean = re.sub(r'topmostSubform\[\d+\]|Page\d+\[\d+\]|ReadOrder\[\d+\]|\w+\[\d+\]', '', raw_name, flags=re.IGNORECASE)
    clean = clean.replace('.', ' ').replace('_', ' ').replace('-', ' ').strip()
    clean = re.sub(r'^\d+\s*', '', clean).strip()
    
    if not clean or len(clean) < 2 or 'box' in clean.lower():
        return None, None
        
    return clean.title(), 'general'

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
        smart_fields = []
        seen_labels = set()

        for page_idx, page in enumerate(doc):
            for widget in page.widgets():
                fname = widget.field_name
                if not fname:
                    continue
                
                label, category = map_field_to_smart_label(fname)
                
                # Anashkalojmë fushat teknike ose të padobishme (si Boxes3A)
                if not label or label in seen_labels:
                    continue
                    
                seen_labels.add(label)
                
                smart_fields.append({
                    "id": fname,
                    "key": label,
                    "section": category,
                    "page": page_idx + 1
                })

        doc.close()

        # Nëse PDF është statike ose nuk u gjetën dritare interaktive
        if not smart_fields:
            smart_fields = [
                {"id": "custom_name", "key": "Emri dhe Mbiemri / Full Name", "section": "personal"},
                {"id": "custom_address", "key": "Adresa / Address", "section": "address"},
                {"id": "custom_tin", "key": "Numri i Identifikimit (TIN / SSN / NIPT)", "section": "identification"},
                {"id": "custom_date", "key": "Data / Date", "section": "general"},
                {"id": "custom_notes", "key": "Shënime ose Tekst shtesë", "section": "general"}
            ]

        return jsonify({
            "success": True,
            "filename": os.path.basename(filepath),
            "fields": smart_fields
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

        # Plotësojmë fushat në PDF
        for page in doc:
            for widget in page.widgets():
                fname = widget.field_name
                if fname in user_inputs and user_inputs[fname]:
                    widget.field_value = str(user_inputs[fname])
                    widget.update()
                    filled_any = True

        # Nëse është PDF statike (pa forma interaktive)
        if not filled_any:
            page = doc[0]
            y_pos = 90
            for key, val in user_inputs.items():
                if val and str(val).strip():
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
