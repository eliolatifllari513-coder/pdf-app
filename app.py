import os
import re
import json
import uuid
import base64

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
# Modelet ndryshohen nga Render (Environment) pa prekur kodin.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_VISION_MODEL = os.environ.get("GROQ_VISION_MODEL", "qwen/qwen3.6-27b")
MAX_VISION_PAGES = int(os.environ.get("MAX_VISION_PAGES", "5"))
ADD_SUMMARY_PAGE = os.environ.get("ADD_SUMMARY_PAGE", "1") == "1"
CHUNK_SIZE = 60

ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png"}
TRUE_VALUES = {"po", "yes", "y", "x", "1", "true", "ok", "si", "sì"}
SKIP_TYPES = {"Button", "Signature"}
DEFAULT_SECTION = "Informacion Përgjithshëm"
CUSTOM_LABELS = {
    "custom_name": "Emri dhe Mbiemri",
    "custom_address": "Adresa",
    "custom_id": "Numri i Identifikimit",
}


# ---------------------------------------------------------------- helpers
def get_client():
    return Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None


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


def call_groq(client, messages, model, vision=False):
    """Provon disa konfigurime; ndalon menjëherë nëse key/modeli janë gabim."""
    if vision:
        configs = [{"response_format": {"type": "json_object"}}, {}]
    else:
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
                model=model,
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


def items_from(data):
    if isinstance(data, dict):
        items = data.get("fields")
        if not isinstance(items, list):
            items = next((v for v in data.values() if isinstance(v, list)), [])
        return items
    return data if isinstance(data, list) else []


def save_as_pdf(file, ext, dest_path):
    """PDF ruhet direkt; foto (jpg/png) kthehet në PDF me një faqe."""
    if ext == ".pdf":
        file.save(dest_path)
        return
    tmp_path = dest_path + ext
    file.save(tmp_path)
    try:
        img = fitz.open(tmp_path)
        pdf_bytes = img.convert_to_pdf()
        img.close()
        pdf = fitz.open("pdf", pdf_bytes)
        pdf.save(dest_path)
        pdf.close()
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def meta_path_for(pdf_path):
    return pdf_path + ".json"


def load_meta(pdf_path):
    path = meta_path_for(pdf_path)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


# ------------------------------------------- PDF me fusha (rruga e vjetër)
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
    ], GROQ_MODEL)
    return items_from(extract_json(content))


def analyze_pdf_with_groq(fields_list, raw_text=""):
    """Kthon (fields, warning). Nëse AI dështon, përdor emra të pastruar automatikisht."""
    labeled = {}
    warning = None
    client = get_client()

    if client is None:
        warning = "GROQ_API_KEY nuk është vendosur; po përdoren emrat teknikë të fushave."
        print("KUJDES:", warning)
    else:
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

    result = []
    for f in fields_list:
        item = labeled.get(f["id"], {})
        result.append({
            "id": f["id"],
            "key": item.get("key") or fallback_label(f["id"]),
            "section": item.get("section") or DEFAULT_SECTION,
        })
    return result, warning


# ------------------------------- Foto / PDF të skanuara (vision + overlay)
def render_page_jpeg_b64(page, max_side=1600):
    rect = page.rect
    scale = min(3.0, max_side / max(rect.width, rect.height))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    return base64.b64encode(pix.tobytes("jpeg")).decode("ascii")


def clean_box(box):
    """Kthen [x0,y0,x1,y1] në shkallë 0-1000, ose None nëse është i pavlefshëm."""
    try:
        x0, y0, x1, y1 = [max(0.0, min(1000.0, float(v))) for v in box]
    except Exception:
        return None
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    if x1 - x0 < 5 or y1 - y0 < 3:
        return None
    return [x0, y0, x1, y1]


def read_page_with_vision(client, page, page_no):
    b64 = render_page_jpeg_b64(page)
    prompt = f"""
You are looking at page {page_no} of a form or document (a scan, a photo or a PDF).
Find every blank the person is expected to fill in: empty lines, empty boxes,
table cells to complete, checkboxes. Ignore text that is already filled in
and ignore printed instructions.

For each blank return an object with:
- "key": a short, user-friendly label in Albanian. If the document is not in
  Albanian, write "Albanian / Original", e.g. "Emri / Name".
- "section": a logical group, e.g. "Të dhënat Personale", "Adresa", "Tjetër".
- "type": "text" or "checkbox".
- "box": [x0, y0, x1, y1] as integers from 0 to 1000, relative to the image
  width and height, (0,0) = top-left. The box must cover the EMPTY space where
  the answer should be written, not the label.

Return ONLY a JSON object: {{"fields": [ ... ]}}. At most 80 fields.
"""
    content = call_groq(client, [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ],
    }], GROQ_VISION_MODEL, vision=True)
    return items_from(extract_json(content))


def vision_extract(doc):
    """Kthon (fields, meta, warning) për dokument pa fusha të plotësueshme."""
    client = get_client()
    if client is None:
        return [], {}, "GROQ_API_KEY nuk është vendosur."

    fields, meta, warning = [], {}, None
    pages_to_read = min(doc.page_count, MAX_VISION_PAGES)
    if doc.page_count > MAX_VISION_PAGES:
        warning = f"U lexuan vetëm {MAX_VISION_PAGES} faqet e para nga {doc.page_count}."

    for p in range(pages_to_read):
        try:
            items = read_page_with_vision(client, doc[p], p + 1)
        except Exception as e:
            print(f"Vision Error (faqja {p + 1}): {e}")
            warning = "AI nuk e lexoi dot të gjithë dokumentin."
            continue

        n = 0
        for item in items:
            if not isinstance(item, dict) or not item.get("key"):
                continue
            n += 1
            fid = f"p{p + 1}_f{n}"
            ftype = "checkbox" if str(item.get("type", "")).lower() == "checkbox" else "text"
            key = str(item["key"]).strip()
            if ftype == "checkbox" and "(Po/Jo)" not in key:
                key += " (Po/Jo)"
            section = str(item.get("section") or DEFAULT_SECTION).strip()
            fields.append({"id": fid, "key": key, "section": section})
            meta[fid] = {
                "page": p,
                "type": ftype,
                "key": key,
                "box": clean_box(item.get("box") or []),
            }
    return fields, meta, warning


def overlay_value(page, field, value):
    r = page.rect
    x0, y0, x1, y1 = field["box"]
    rect = fitz.Rect(
        r.x0 + x0 / 1000 * r.width, r.y0 + y0 / 1000 * r.height,
        r.x0 + x1 / 1000 * r.width, r.y0 + y1 / 1000 * r.height,
    )

    if field["type"] == "checkbox":
        if value.lower() in TRUE_VALUES:
            size = max(8, min(rect.width, rect.height, 14))
            cx, cy = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
            page.insert_text((cx - size * 0.3, cy + size * 0.35), "X",
                             fontsize=size, fontname="helv", color=(0, 0, 0))
        return

    fontsize = max(6, min(12, rect.height * 0.8))
    while fontsize >= 6:
        if page.insert_textbox(rect, value, fontsize=fontsize,
                               fontname="helv", color=(0, 0, 0)) >= 0:
            return
        fontsize -= 1
    # Nuk ngjitet: e lëmë të zgjerohet pak poshtë kutisë
    bigger = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y1 + 30)
    page.insert_textbox(bigger, value, fontsize=7, fontname="helv", color=(0, 0, 0))


def fill_with_overlay(doc, meta, user_inputs):
    rows, missing_box, filled_any = [], False, False
    for fid, f in meta.items():
        value = user_inputs.get(fid)
        if value is None or not str(value).strip():
            continue
        value = str(value).strip()
        rows.append((f.get("key") or fid, value))
        filled_any = True
        page_index = f.get("page", 0)
        if not f.get("box") or page_index >= doc.page_count:
            missing_box = True
            continue
        try:
            overlay_value(doc[page_index], f, value)
        except Exception as e:
            print(f"Nuk u shkrua fusha {fid}: {e}")
            missing_box = True
    return filled_any, rows, missing_box


def add_summary_page(doc, rows):
    """Faqe e re me përgjigjet e renditura (rrjeti i sigurisë)."""
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 60), "Përmbledhje e përgjigjeve", fontsize=16, fontname="helv")
    y = 95
    for label, val in rows:
        text = f"{label}: {val}"
        height = (len(text) // 80 + 1) * 15 + 6
        if y + height > 800:
            page = doc.new_page(width=595, height=842)
            y = 60
        page.insert_textbox(fitz.Rect(50, y, 545, y + height), text,
                            fontsize=11, fontname="helv", color=(0, 0, 0))
        y += height


# --------------------------------------------- plotësimi i fushave PDF
def fill_widgets(doc, user_inputs):
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
    return filled_any


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

    base, ext = os.path.splitext(file.filename)
    ext = ext.lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"success": False,
                        "error": "Lejohen vetëm PDF, JPG ose PNG."}), 400

    safe_base = secure_filename(base) or "document"
    stored_name = f"{uuid.uuid4().hex[:8]}_{safe_base}.pdf"
    filepath = os.path.join(UPLOAD_FOLDER, stored_name)

    try:
        save_as_pdf(file, ext, filepath)
    except Exception as e:
        print(f"Gabim ruajtje: {e}")
        return jsonify({"success": False, "error": "Skedari nuk u lexua dot. Provo një tjetër."}), 400

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

        warning, mode = None, "form"

        if raw_fields:
            smart_fields, warning = analyze_pdf_with_groq(raw_fields, raw_text)
        else:
            mode = "vision"
            smart_fields, meta, warning = vision_extract(doc)
            if smart_fields:
                with open(meta_path_for(filepath), "w", encoding="utf-8") as fh:
                    json.dump(meta, fh, ensure_ascii=False)
            else:
                warning = (warning or "AI nuk gjeti fusha.") + " Po shfaqen pyetje bazë."
                smart_fields = [
                    {"id": k, "key": v, "section": "Të dhënat Personale"}
                    for k, v in CUSTOM_LABELS.items()
                ]
        doc.close()

        return jsonify({
            "success": True,
            "filename": stored_name,
            "fields": smart_fields,
            "warning": warning,
            "mode": mode,
        })

    except Exception as e:
        print(f"Gabim PDF: {e}")
        return jsonify({"success": False, "error": f"Gabim gjatë leximit të dokumentit: {e}"}), 500


@app.route("/fill-pdf", methods=["POST"])
def fill_pdf():
    data = request.get_json(silent=True) or {}
    filename = os.path.basename(data.get("filename") or "")
    user_inputs = data.get("inputs") or {}

    input_path = os.path.join(UPLOAD_FOLDER, filename)
    if not filename or not os.path.isfile(input_path):
        return jsonify({"success": False, "error": "Skedari nuk u gjet. Ngarkoje përsëri."}), 404

    output_filename = f"completed_{filename}"
    output_path = os.path.join(OUTPUT_FOLDER, output_filename)

    try:
        doc = fitz.open(input_path)
        meta = load_meta(input_path)

        if meta:
            filled_any, rows, missing_box = fill_with_overlay(doc, meta, user_inputs)
            if rows and (ADD_SUMMARY_PAGE or missing_box):
                add_summary_page(doc, rows)
        else:
            filled_any = fill_widgets(doc, user_inputs)
            if not filled_any:
                rows = [
                    (CUSTOM_LABELS.get(k, k), str(v).strip())
                    for k, v in user_inputs.items() if v and str(v).strip()
                ]
                if rows:
                    add_summary_page(doc, rows)

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

