import os
import json
import fitz  # PyMuPDF
from flask import Flask, render_template, request, send_file, jsonify
from groq import Groq

app = Flask(__name__)

UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

def clean_field_name(raw_name):
    """Largon kllapat dhe pikat teknike nga emrat e PDF (p.sh. Topmostsubform.Page1.F1 -> F1)"""
    parts = raw_name.replace('[0]', '').split('.')
    return parts[-1] if parts else raw_name

def analyze_pdf_with_groq(fields_list, raw_text=""):
    """Dërgon fushat teknike te Groq AI dhe detyron përkthimin në pyetje njerëzore."""
    if not GROQ_API_KEY:
        print("KUJDES: GROQ_API_KEY nuk është vendosur!")

    try:
        client = Groq(api_key=GROQ_API_KEY)
        
        # Përgatitim një listë më të pastër për AI
        simplified_fields = [
            {"id": f["id"], "short_name": clean_field_name(f["id"])}
            for f in fields_list
        ]

        prompt = f"""
You are a smart document analysis AI.
Below is a list of technical PDF form field IDs and a sample text extract from the PDF document.

PDF TEXT SAMPLE:
{raw_text[:1500]}

TECHNICAL FIELDS TO MAP:
{json.dumps(simplified_fields, indent=2)}

YOUR TASK:
1. Identify what document this is (e.g. IRS Form W-9, Application Form, Invoice, Contract, etc.).
2. Translate each technical field ID into a natural, user-friendly label (in Albanian or English based on text).
3. Group related fields into logical sections (e.g., "Personal Info", "Address", "Taxpayer Identification", "Other").

CRITICAL REQUIREMENT:
Return ONLY a JSON object with a single key "fields", containing an array of objects.
Each object MUST preserve the EXACT "id" from the input.

Example JSON output format:
{{
  "fields": [
    {{
      "id": "Topmostsubform[0].Page1[0].F1_01[0]",
      "key": "Emri dhe Mbiemri / Name",
      "section": "Të dhënat Personale"
    }}
  ]
}}
"""

        chat_completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": "You are a helpful JSON generator. Output valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            model="llama-3.3-70b-versatile",
            temperature=0.1,
            response_format={"type": "json_object"}
        )

        response_text = chat_completion.choices[0].message.content
        data = json.loads(response_text)

        # Nxjerrim listën nga përgjigjja e AI
        if isinstance(data, dict):
            if "fields" in data:
                return data["fields"]
            for v in data.values():
                if isinstance(v, list):
                    return v

        return data if isinstance(data, list) else []

    except Exception as e:
        print(f"Groq AI Exception: {e}")
        # Nëse AI dështon, bëjmë një pastrim automatik që të mos shfaqen 'Topmostsubform...'
        cleaned_fallback = []
        for f in fields_list:
            clean_label = clean_field_name(f["id"]).replace('_', ' ').title()
            cleaned_fallback.append({
                "id": f["id"],
                "key": clean_label if len(clean_label) > 2 else f["id"],
                "section": "Informacion Përgjithshëm"
            })
        return cleaned_fallback

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
        raw_fields = []
        raw_text = ""

        for page in doc:
            raw_text += page.get_text() + "\n"
            for widget in page.widgets():
                if widget.field_name:
                    raw_fields.append({"id": widget.field_name})

        doc.close()

        if raw_fields:
            smart_fields = analyze_pdf_with_groq(raw_fields, raw_text)
        else:
            smart_fields = [
                {"id": "custom_name", "key": "Emri dhe Mbiemri", "section": "Të dhënat Personale"},
                {"id": "custom_address", "key": "Adresa", "section": "Adresa"},
                {"id": "custom_id", "key": "Numri i Identifikimit", "section": "Identifikimi"}
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

        for page in doc:
            for widget in page.widgets():
                fname = widget.field_name
                if fname in user_inputs and user_inputs[fname]:
                    widget.field_value = str(user_inputs[fname])
                    widget.update()
                    filled_any = True

        if not filled_any:
            page = doc[0]
            y_pos = 100
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
