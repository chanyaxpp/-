from flask import Flask, request, jsonify
from flask_cors import CORS
import whisper
import os
import tempfile

app = Flask(__name__)
CORS(app)

print("กำลังโหลดโมเดล Whisper (medium)...")
model = whisper.load_model("medium")
print("โมเดลพร้อมใช้งานแล้ว!")

ALLOWED_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.mp4', '.mov', '.ogg', '.flac', '.webm'}

THAI_PROMPT = (
    "ต่อไปนี้เป็นคำบอกเล่าภาษาไทย เขียนด้วยอักษรไทยที่ถูกต้อง "
    "น้ำเต้าหู้ ถั่วเหลือง กาแฟ ข้าวต้ม ผัดไทย ส้มตำ ต้มยำกุ้ง "
    "บริษัท โรงพยาบาล มหาวิทยาลัย สถานี รถไฟฟ้า ห้างสรรพสินค้า "
)

# พจนานุกรมซ่อมแซมคำผิดภาษาไทย (ดักจับคำที่สระแฝงก้ำกึ่ง)
TH_WORD_REPAIR = {
    "น้ำต่อหู": "น้ำเต้าหู้",
    "ทวนลือง": "ถั่วเหลือง",
    "น้ำต้มหู้": "น้ำเต้าหู้",
    "ทัวเหลือง": "ถั่วเหลือง",
    "ทวนเหลือง": "ถั่วเหลือง"
}

@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok"})

@app.route("/transcribe", methods=["POST"])
def transcribe_audio():
    if "audio" not in request.files:
        return jsonify({"error": "ไม่พบไฟล์เสียงในคำขอ"}), 400

    audio_file = request.files["audio"]
    if not audio_file.filename:
        return jsonify({"error": "ชื่อไฟล์ว่างเปล่า"}), 400

    ext = os.path.splitext(audio_file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"ไม่รองรับไฟล์ประเภท {ext}"}), 400

    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    tmp_path = tmp.name
    tmp.close()
    audio_file.save(tmp_path)

    try:
        result = model.transcribe(
            tmp_path,
            language="th",
            initial_prompt=THAI_PROMPT,
            beam_size=5,
            best_of=5,
            temperature=0.0,
            condition_on_previous_text=True,
            fp16=False,
        )
        
        # 1. จัดระเบียบและยุบวรรคตอนข้อความดิบ
        text_result = result["text"].strip()
        
        # 2. ทำกระบวนการตรวจคำสะกดและซ่อมแซมคำผิดอัตโนมัติ (Heuristic Word Repair)
        for wrong_word, right_word in TH_WORD_REPAIR.items():
            text_result = text_result.replace(wrong_word, right_word)

        # 3. จัดการโครงสร้างประโยคย่อยใน segments ให้ตรงกันด้วย
        segments = []
        for seg in result.get("segments", []):
            seg_text = seg["text"].strip()
            for wrong_word, right_word in TH_WORD_REPAIR.items():
                seg_text = seg_text.replace(wrong_word, right_word)
            
            segments.append({
                "start": round(seg["start"], 2),
                "end":   round(seg["end"], 2),
                "text":  seg_text
            })
            
        return jsonify({"text": text_result, "segments": segments, "language": result.get("language", "th")})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)