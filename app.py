import os
import re
import json
import uuid

import fitz  # PyMuPDF
from flask import Flask, render_template, request, send_file, jsonify, abort
from groq import Groq
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # max 20 MB per skedar

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
OUTPUT_FOLDER = os.path.join(BASE_DIR, "outputs")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
# Modeli ndryshohet nga Render (Environment -> GROQ_MODEL) pa prekur kodin.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
CHUNK_SIZE = 60  # sa fusha i dërgohen AI-së në një kërkesë

TRUE_VALUES = {"po", "yes", "y", "x", "1", "true", "ok", "si", "sì"}
SKIP_TYPES = {"Button", "Signature"}
DEFAULT_SECTION = "Informacion Përgjithshëm"


# ---------------------------------------------------------------- helpers
def clean_field_name(raw_name):
    """Topmostsubform[0].Page1[0].F1_01[0] -> F1_01"""
    name = re.sub(r"\[\d+\]", "", raw_name)
    parts = name.split(".")
    return parts[-1] if parts and parts[-1] else raw_name


def fallback_label(field_id):
    label = clean_field_name(field_id).replace("_", " ").strip().title()
    return label if len(label) > 2 else field_id


def extract_json(text):
    """Merr JSON edhe nëse modeli shton tekst ose ```json rreth tij."""
    if not text:
        raise ValueError("Përgjigje bosh nga AI")
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("AI nuk ktheu JSON të vlefshëm")


def call_groq(client, messages):
    """Provon disa konfigurime; ndalon menjëherë nëse key/modeli janë gabim."""
    configs = [
        {"response_format": {"type": "json_object"},
         "extra_body": {"reasoning_effort": "low"}},
        {"response_format": {"type": "json_object"}},
        {},
    ]
    last_error = None
    for cfg in configs:
        try:
            result = client.chat.completions.create(
                messages=messages,
                model=GROQ_MODEL,
                temperature=0.1,
                max_tokens=8000,
                **cfg,
            )
            return result.choices[0].message.content
        except Exception as e:
            last_error = e
            msg = str(e)
            if "model_not_found" in msg or "invalid_api_key" in msg or "401" in msg:
                break
    raise last_error


def label_chunk(client, chunk, raw_text):
    simplified = [
        {"id": f["id"], "short_name": clean_field_name(f["id"]), "type": f["type"]}
        for f in chunk
    ]
    prompt = f"""
You are a smart document analysis AI.
Below is a sample text extract from a PDF form and a list of technical form fields.

PDF TEXT SAMPLE:
{raw_text[:3000]}

TECHNICAL FIELDS TO MAP:
{json.dumps(simplified, indent=2, ensure_ascii=False)}

YOUR TASK:
1. Figure out what document this is.
2. Give every field a short, natural, user-friendly label (Albanian if the document
   is Albanian, otherwise Albanian + English like "Emri / Name").
3. For fields whose type is CheckBox or RadioButton, end the label with " (Po/Jo)".
4. Group related fields into logical sections (e.g. "Të dhënat Personale", "Adresa", "Tjetër").

CRITICAL:
Return ONLY a JSON object with one key "fields" holding an array of objects.
Every object MUST keep the EXACT "id" from the input and have "key" and "section".

Example:
{{"fields": [{{"id": "Topmostsubform[0].Page1[0].F1_01[0]", "key": "Emri dhe Mbiemri / Name", "section": "Të dhënat Personale"}}]}}
"""
    content = call_groq(client, [
        {"role": "system", "content": "You are a helpful JSON generator. Output valid JSON only."},
        {"role": "user", "content": prompt},
    ])
    data = extract_json(content)
    if isinstance(data, dict):
        items = data.get("fields")
        if not isinstance(items, list):
            items = next((v for v in data.values() if isinstance(v, list)), [])
    else:
        items = data if isinstance(data, list) else []
    return items


def analyze_pdf_with_groq(fields_list, raw_text=""):
    """Kthon (fields, warning). Nëse AI dështon, përdor emra të pastruar automatikisht."""
    labeled = {}
    warning = None

    if not GROQ_API_KEY:
        warning = "GROQ_API_KEY nuk është vendosur; po përdoren emrat teknikë të fushave."
        print("KUJDES:", warning)
    else:
        client = Groq(api_key=GROQ_API_KEY)
        for i in range(0, len(fields_list), CHUNK_SIZE):
            chunk = fields_list[i:i + CHUNK_SIZE]
            valid_ids = {f["id"] for f in chunk}
            try:
                for item in label_chunk(client, chunk, raw_text):
                    if isinstance(item, dict) and item.get("id") in valid_ids:
                        labeled[item["id"]] = item
            except Exception as e:
                print(f"Groq AI Error (chunk {i // CHUNK_SIZE + 1}): {e}")
                warning = "AI nuk u përgjigj plotësisht; disa fusha kanë emra të përafërt."

    # Ruaj rendin origjinal dhe plotëso çdo fushë që AI e humbi
    result = []
    for f in fields_list:
        item = labeled.get(f["id"], {})
        result.append({
            "id": f["id"],
            "key": item.get("key") or fallback_label(f["id"]),
            "section": item.get("section") or DEFAULT_SECTION,
        })
    return result, warning


# ----------------------------------------------------------------- routes
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/extract-fields", methods=["POST"])
def extract_fields():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "Nuk u zgjodh asnjë skedar."}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"success": False, "error": "Skedar i pavlefshëm."}), 400
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "Lejohen vetëm skedarë .pdf"}), 400

    safe_name = secure_filename(file.filename) or "document.pdf"
    stored_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
    filepath = os.path.join(UPLOAD_FOLDER, stored_name)
    file.save(filepath)

    try:
        doc = fitz.open(filepath)
        raw_fields, seen, raw_text = [], set(), ""

        for page in doc:
            raw_text += page.get_text() + "\n"
            for widget in page.widgets():
                name = widget.field_name
                ftype = widget.field_type_string
                if not name or name in seen or ftype in SKIP_TYPES:
                    continue
                seen.add(name)
                raw_fields.append({"id": name, "type": ftype})
        doc.close()

        warning = None
        if raw_fields:
            smart_fields, warning = analyze_pdf_with_groq(raw_fields, raw_text)
        else:
            # PDF pa fusha: pyetje bazë, teksti shtohet sipër faqes së parë
            smart_fields = [
                {"id": "custom_name", "key": "Emri dhe Mbiemri", "section": "Të dhënat Personale"},
                {"id": "custom_address", "key": "Adresa", "section": "Adresa"},
                {"id": "custom_id", "key": "Numri i Identifikimit", "section": "Identifikimi"},
            ]

        return jsonify({
            "success": True,
            "filename": stored_name,
            "fields": smart_fields,
            "warning": warning,
        })

    except Exception as e:
        print(f"Gabim PDF: {e}")
        return jsonify({"success": False, "error": f"Gabim gjatë leximit të PDF: {e}"}), 500


@app.route("/fill-pdf", methods=["POST"])
def fill_pdf():
    data = request.get_json(silent=True) or {}
    filename = os.path.basename(data.get("filename") or "")
    user_inputs = data.get("inputs") or {}

    input_path = os.path.join(UPLOAD_FOLDER, filename)
    if not filename or not os.path.isfile(input_path):
        return jsonify({"success": False, "error": "Skedari nuk u gjet. Ngarkoje PDF-në përsëri."}), 404

    output_filename = f"completed_{filename}"
    output_path = os.path.join(OUTPUT_FOLDER, output_filename)

    try:
        doc = fitz.open(input_path)
        filled_any = False
        radio_done = set()

        for page in doc:
            for widget in page.widgets():
                fname = widget.field_name
                value = user_inputs.get(fname)
                if value is None or not str(value).strip():
                    continue
                value = str(value).strip()
                ftype = widget.field_type_string

                try:
                    if ftype in ("CheckBox", "RadioButton"):
                        if ftype == "RadioButton" and fname in radio_done:
                            continue
                        if value.lower() in TRUE_VALUES:
                            widget.field_value = widget.on_state()
                            if ftype == "RadioButton":
                                radio_done.add(fname)
                        else:
                            widget.field_value = widget.off_state() or "Off"
                    else:
                        widget.field_value = value
                    widget.update()
                    filled_any = True
                except Exception as we:
                    print(f"Nuk u plotësua fusha {fname}: {we}")

        if not filled_any:
            labels = {
                "custom_name": "Emri dhe Mbiemri",
                "custom_address": "Adresa",
                "custom_id": "Numri i Identifikimit",
            }
            page = doc[0]
            y_pos = 100
            for key, val in user_inputs.items():
                if val and str(val).strip():
                    label = labels.get(key, key)
                    page.insert_text((50, y_pos), f"{label}: {val}", fontsize=11, color=(0, 0, 0))
                    y_pos += 25

        doc.save(output_path)
        doc.close()

        return jsonify({"success": True, "download_url": f"/download/{output_filename}"})

    except Exception as e:
        print(f"Gabim plotësimi: {e}")
        return jsonify({"success": False, "error": f"Gabim gjatë plotësimit: {e}"}), 500


@app.route("/download/<path:filename>")
def download(filename):
    safe = os.path.basename(filename)
    file_path = os.path.join(OUTPUT_FOLDER, safe)
    if not os.path.isfile(file_path):
        abort(404)
    return send_file(file_path, as_attachment=True)


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"success": False, "error": "Skedari është shumë i madh (maksimumi 20 MB)."}), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
