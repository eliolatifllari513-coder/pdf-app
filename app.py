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

# Vendos Groq API Key (Mund ta vendosësh edhe si Environment Variable ne Render)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "KOPJO_KETU_GROQ_API_KEY_TUAN")

def analyze_pdf_with_groq(fields_list, raw_text=""):
    """Dërgon fushat teknike te Groq AI dhe merr mbrapsht pyetje të kuptueshme për përdoruesin."""
    try:
        client = Groq(api_key=GROQ_API_KEY)
        
        prompt = f"""
        You are an intelligent PDF Form Assistant.
        Analyzed PDF Fields (Technical names): {json.dumps(fields_list)}
        PDF Context Text Sample: {raw_text[:1000]}

        Task:
        1. Identify what this PDF document is about.
        2. Map the technical field names into clear, natural, human-friendly questions/labels (e.g. "Emri dhe Mbiemri", "Adresa e banimit", "Numri i Identifikimit (TIN/SSN/NIPT)").
        3. Ignore duplicate fields, signature fields, or irrelevant layout boxes.
        4. Group them into logical sections: "personal", "address", "identification", or "general".

        Return ONLY a JSON array of objects with this EXACT structure (no markdown, no explanations):
        [
          {{
            "id": "technical_field_id_from_input",
            "key": "Human friendly label in Albanian or English",
            "section": "personal"
          }}
        ]
        """

        chat_completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": "You output strictly valid JSON format."},
                {"role": "user", "content": prompt}
            ],
            model="llama-3.3-70b-versatile",
            temperature=0.1,
            response_format={"type": "json_object"}
        )

        response_content = chat_completion.choices[0].message.content
        data = json.loads(response_content)
        
        # Nëse përgjigjja është e mbështjellë në një çelës si "fields"
        if isinstance(data, dict):
            return data.get("fields", list(data.values())[0] if data else [])
        return data

    except Exception as e:
        print(f"Groq AI Error: {e}")
        # Fallback nëse AI dështon ose nuk ka API Key valid
        return [
            {"id": f["id"], "key": f["id"].replace('_', ' ').title(), "section": "general"}
            for f in fields_list[:8]
        ]

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

        # Skanojmë faqet dhe nxjerrim tekstin si dhe fushat interaktive
        for page in doc:
            raw_text += page.get_text() + "\n"
            for widget in page.widgets():
                if widget.field_name:
                    raw_fields.append({"id": widget.field_name})

        doc.close()

        # Dërgojmë të dhënat te Groq AI për analizë inteligjente
        if raw_fields:
            smart_fields = analyze_pdf_with_groq(raw_fields, raw_text)
        else:
            # Nëse është PDF e skanuar / statike (pa forma interaktive)
            smart_fields = [
                {"id": "custom_name", "key": "Emri dhe Mbiemri / Full Name", "section": "personal"},
                {"id": "custom_address", "key": "Adresa / Address", "section": "address"},
                {"id": "custom_id", "key": "Numri i Identifikimit (SSN/EIN/NIPT)", "section": "identification"},
                {"id": "custom_date", "key": "Data / Date", "section": "general"}
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

        # Plotësojmë fushat ekzakte të PDF-së
        for page in doc:
            for widget in page.widgets():
                fname = widget.field_name
                if fname in user_inputs and user_inputs[fname]:
                    widget.field_value = str(user_inputs[fname])
                    widget.update()
                    filled_any = True

        # Nëse nuk kishte fusha interaktive (PDF statike), shkruajmë tekstin në dokument
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
