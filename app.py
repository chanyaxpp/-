from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import whisper
import os
import tempfile
import subprocess
import json
import re
import torch

app = Flask(__name__)
CORS(app)

app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

# ── เลือกโมเดลตาม env var WHISPER_MODEL (default: medium) ──
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "medium")

# ── fp16 อัตโนมัติ: เปิดถ้ามี CUDA, ปิดบน CPU ──
USE_FP16 = torch.cuda.is_available()

print(f"กำลังโหลดโมเดล Whisper ({WHISPER_MODEL}) | fp16={USE_FP16} | device={'cuda' if USE_FP16 else 'cpu'}")
model = whisper.load_model(WHISPER_MODEL)
print("โมเดลพร้อมใช้งานแล้ว!")

ALLOWED_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.mp4', '.mov', '.ogg', '.flac', '.webm'}

# Prompt ที่เปิดกว้างขึ้น ไม่บังคับเฉพาะภาษาไทย
GENERAL_PROMPT = (
    "ต่อไปนี้เป็นการบันทึกเสียง กรุณาเขียนถอดความด้วยภาษาที่ถูกต้องและเป็นทางการตามภาษาต้นฉบับที่ได้ยิน "
    "พิมพ์คำสะกดให้ถูกต้อง เว้นวรรคประโยคให้เป็นธรรมชาติ หากมีคำศัพท์เฉพาะทางให้เขียนให้ถูกต้องตามความนิยม"
)

TH_WORD_REPAIR = {
    "น้ำต่อหู": "น้ำเต้าหู้",
    "ทวนลือง": "ถั่วเหลือง",
    "น้ำต้มหู้": "น้ำเต้าหู้",
    "ทัวเหลือง": "ถั่วเหลือง",
    "ทวนเหลือง": "ถั่วเหลือง",
}

def get_audio_duration(wav_path):
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", wav_path
    ], capture_output=True, text=True)
    try:
        return float(probe.stdout.strip())
    except:
        return 0.0

def detect_silence_splits(wav_path, target_chunk=30, min_silence_len=0.5, silence_thresh=-35):
    duration = get_audio_duration(wav_path)
    if duration == 0 or duration <= target_chunk * 1.5:
        return [(0, None)]

    result = subprocess.run([
        "ffmpeg", "-i", wav_path,
        "-af", f"silencedetect=noise={silence_thresh}dB:d={min_silence_len}",
        "-f", "null", "-"
    ], capture_output=True, text=True)

    silence_ends = []
    for m in re.finditer(r"silence_end: ([\d.]+)", result.stderr):
        t = float(m.group(1))
        if t < duration - 2:
            silence_ends.append(t)

    if not silence_ends:
        splits = []
        t = 0.0
        while t < duration:
            end = min(t + target_chunk, duration)
            splits.append((t, end if end < duration else None))
            t = end
        return splits

    splits = []
    current_start = 0.0
    next_target = target_chunk

    while current_start < duration - 5:
        window_start = next_target - 15
        window_end = next_target + 15
        candidates = [t for t in silence_ends if window_start < t < window_end and t > current_start + 5]

        if candidates:
            best = min(candidates, key=lambda t: abs(t - next_target))
        else:
            candidates = [t for t in silence_ends if t > current_start + 10]
            if candidates:
                after_half = [t for t in candidates if t >= current_start + target_chunk * 0.5]
                best = after_half[0] if after_half else candidates[-1]
            else:
                best = min(current_start + target_chunk, duration)

        splits.append((current_start, best))
        current_start = best
        next_target = current_start + target_chunk

    if current_start < duration - 1:
        splits.append((current_start, None))
    return splits

def extract_chunk(wav_path, start, end, out_path):
    cmd = ["ffmpeg", "-y", "-i", wav_path, "-ss", str(start)]
    if end is not None:
        cmd += ["-t", str(end - start)]
    cmd += ["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", out_path]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

def repair_text(text):
    for w, r in TH_WORD_REPAIR.items():
        text = text.replace(w, r)
    return text

def google_translate(text, target_lang):
    import urllib.request
    import urllib.parse
    if not text.strip():
        return ""
    try:
        params = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": target_lang, "dt": "t", "q": text})
        url = f"https://translate.googleapis.com/translate_a/single?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        translated = ""
        if raw and isinstance(raw[0], list):
            for part in raw[0]:
                if part and part[0]:
                    translated += part[0]
        return translated
    except Exception as e:
        print(f"Google Translate Error: {e}")
        return text

def transcribe_chunk(chunk_path, offset=0.0, task="transcribe", source_lang=None):
    """ถอดเสียง 1 chunk — รองรับภาษา auto เพื่อให้ตรวจจับภาษาเอง"""
    import gc
    try:
        lang_param = None if source_lang == "auto" else source_lang

        result = model.transcribe(
            chunk_path,
            language=lang_param,
            task=task,
            initial_prompt=GENERAL_PROMPT,
            condition_on_previous_text=False,
            temperature=(0.0, 0.2),
            no_speech_threshold=0.6,
            logprob_threshold=-1.0,
            compression_ratio_threshold=2.4,
            fp16=USE_FP16,
            verbose=False,
            beam_size=5,
            patience=1.0
        )
        
        raw_text = result["text"].strip()
        text = repair_text(raw_text) if lang_param == "th" else raw_text
        detected_language = result.get("language", "unknown")
        
        segments = []
        for seg in result.get("segments", []):
            seg_text = repair_text(seg["text"].strip()) if lang_param == "th" else seg["text"].strip()
            segments.append({
                "start": round(seg["start"] + offset, 2),
                "end":   round(seg["end"] + offset, 2),
                "text":  seg_text,
            })
        return text, segments, detected_language
    finally:
        gc.collect()
        if USE_FP16 and torch.cuda.is_available():
            torch.cuda.empty_cache()

@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok"})

@app.route("/translate", methods=["POST"])
def translate_text():
    data = request.get_json()
    if not data or "text" not in data or "target" not in data:
        return jsonify({"error": "ต้องระบุ text และ target"}), 400
    res = google_translate(data["text"], data["target"])
    return jsonify({"translation": res, "engine": "google"})

@app.route("/transcribe-stream", methods=["POST"])
def transcribe_stream():
    if "audio" not in request.files:
        return jsonify({"error": "ไม่พบไฟล์"}), 400

    audio_file = request.files["audio"]
    source_lang = request.form.get("source_lang", "auto") # รับค่าภาษาต้นทาง (ค่าเริ่มต้นเป็น auto เพื่อสแกนหาภาษา)
    mode = request.form.get("mode", "original")
    target_lang = request.form.get("target_lang", "th")

    ext = os.path.splitext(audio_file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"ไม่รองรับ {ext}"}), 400

    tmp_upload = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    tmp_upload_path = tmp_upload.name
    audio_file.save(tmp_upload_path)
    tmp_upload.close()
    tmp_wav_path = tmp_upload_path + "_converted.wav"

    def generate():
        chunk_paths = []
        try:
            yield f"data: {json.dumps({'type':'progress','pct':3,'msg':'กำลังแปลงไฟล์เสียง...'})}\n\n"

            subprocess.run([
                "ffmpeg", "-y", "-i", tmp_upload_path,
                "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", tmp_wav_path
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            duration = get_audio_duration(tmp_wav_path)
            splits = detect_silence_splits(tmp_wav_path, target_chunk=30)
            total_chunks = len(splits)

            yield f"data: {json.dumps({'type':'progress','pct':8,'msg':f'แบ่งออกเป็น {total_chunks} ส่วนเพื่อประมวลผล...'})}\n\n"

            for i, (start, end) in enumerate(splits):
                chunk_path = tmp_wav_path + f"_chunk{i}.wav"
                chunk_paths.append(chunk_path)
                extract_chunk(tmp_wav_path, start, end, chunk_path)

            all_texts = []
            all_segments = []
            final_detected_lang = source_lang

            for i, (start, end) in enumerate(splits):
                pct = 12 + int(((i + 0.5) / total_chunks) * 83)
                yield f"data: {json.dumps({'type':'progress','pct':pct,'msg':f'กำลังวิเคราะห์และถอดเสียงส่วนที่ {i+1}/{total_chunks}...'})}\n\n"

                whisper_task = "translate" if (mode == "translate" and target_lang == "en") else "transcribe"
                text, segs, det_lang = transcribe_chunk(chunk_paths[i], offset=start, task=whisper_task, source_lang=source_lang)
                
                if i == 0:
                    final_detected_lang = det_lang

                if mode == "translate" and target_lang != "en":
                    text = google_translate(text, target_lang)
                    for seg in segs:
                        seg["text"] = google_translate(seg["text"], target_lang)

                all_texts.append(text)
                all_segments.extend(segs)

                done_pct = 12 + int(((i + 1) / total_chunks) * 83)
                for seg in segs:
                    yield f"data: {json.dumps({'type':'segment','pct':done_pct,'seg':seg,'chunk_index':i})}\n\n"
                yield f"data: {json.dumps({'type':'chunk_done','pct':done_pct,'chunk_index':i,'chunk_text':text})}\n\n"

            final_text = " ".join(t for t in all_texts if t).strip()
            yield f"data: {json.dumps({'type':'done','pct':100,'text':final_text,'segments':all_segments,'language':final_detected_lang,'total_chunks':total_chunks})}\n\n"

        except Exception as e:
            import traceback
            err_detail = traceback.format_exc()
            yield f"data: {json.dumps({'type':'error','msg':str(e),'detail':err_detail[:200]})}\n\n"
        finally:
            for p in [tmp_upload_path, tmp_wav_path] + chunk_paths:
                if p and os.path.exists(p):
                    try: os.remove(p)
                    except: pass

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)