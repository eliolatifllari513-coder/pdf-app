import os
from flask import Flask, render_template, request, send_file, jsonify
from pypdf import PdfReader, PdfWriter

app = Flask(__name__)

UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

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
        reader = PdfReader(filepath)
        fields = reader.get_fields()

        if not fields:
            return jsonify({
                "error": "Ky PDF nuk përmban fusha interaktive për plotësim."
            }), 400

        field_list = []
        for key, field_info in fields.items():
            clean_label = key.split('.')[-1].split('[')[0].replace('_', ' ')
            field_list.append({
                "id": key,
                "label": clean_label if clean_label else key
            })

        return jsonify({
            "success": True,
            "filename": file.filename,
            "fields": field_list
        })
    except Exception as e:
        return jsonify({"error": f"Gabim gjatë leximit: {str(e)}"}), 500

@app.route("/fill-pdf", methods=["POST"])
def fill_pdf():
    data = request.json
    filename = data.get("filename")
    user_inputs = data.get("inputs")

    input_path = os.path.join(UPLOAD_FOLDER, filename)
    output_filename = f"completed_{filename}"
    output_path = os.path.join(OUTPUT_FOLDER, output_filename)

    try:
        reader = PdfReader(input_path)
        writer = PdfWriter()

        writer.append(reader)

        for page in writer.pages:
            writer.update_page_form_field_values(page, user_inputs)

        with open(output_path, "wb") as output_stream:
            writer.write(output_stream)

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

