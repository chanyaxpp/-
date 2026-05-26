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

# ── เลือกโมเดลตาม env var WHISPER_MODEL (default: medium สมดุลความเร็ว/แม่น) ──
# large-v3 = แม่นสุด แต่ช้า ~4x  |  medium = แนะนำ  |  small = เร็วสุด
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "medium")

# ── fp16 อัตโนมัติ: เปิดถ้ามี CUDA, ปิดบน CPU ──
USE_FP16 = torch.cuda.is_available()

print(f"กำลังโหลดโมเดล Whisper ({WHISPER_MODEL}) | fp16={USE_FP16} | device={'cuda' if USE_FP16 else 'cpu'}")
model = whisper.load_model(WHISPER_MODEL)
print("โมเดลพร้อมใช้งานแล้ว!")

# ถอดเสียงทีละ chunk (ป้องกัน OOM บนไฟล์ใหญ่)
# parallel ถูกปิด — large model ใช้ RAM มาก 2 chunks พร้อมกันทำให้หน่วยความจำเต็ม

ALLOWED_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.mp4', '.mov', '.ogg', '.flac', '.webm'}

THAI_PROMPT = (
    "ต่อไปนี้เป็นการบันทึกเสียงภาษาไทย กรุณาเขียนด้วยตัวอักษรไทยที่ถูกต้อง "
    "ใช้วรรณยุกต์และสระให้ครบถ้วน ไม่ต้องแปลเป็นภาษาอื่น "
    "คำที่พบบ่อย: ครับ ค่ะ นะครับ นะคะ ได้เลย ขอบคุณ สวัสดี "
    "น้ำเต้าหู้ ถั่วเหลือง กาแฟ ข้าวต้ม ผัดไทย ส้มตำ ต้มยำกุ้ง แกงเขียวหวาน "
    "บริษัท ประชุม รายงาน โครงการ งบประมาณ แผนการ ผลิตภัณฑ์ "
)

TH_WORD_REPAIR = {
    "น้ำต่อหู": "น้ำเต้าหู้",
    "ทวนลือง": "ถั่วเหลือง",
    "น้ำต้มหู้": "น้ำเต้าหู้",
    "ทัวเหลือง": "ถั่วเหลือง",
    "ทวนเหลือง": "ถั่วเหลือง",
}

# ==========================================
# CHUNKING — แบ่งไฟล์เสียงตามช่วงเงียบ
# ==========================================
def get_audio_duration(wav_path):
    """คืนค่าความยาวเสียงเป็นวินาที"""
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", wav_path
    ], capture_output=True, text=True)
    try:
        return float(probe.stdout.strip())
    except:
        return 0.0


def detect_silence_splits(wav_path, target_chunk=30, min_silence_len=0.5, silence_thresh=-35):
    """
    ใช้ ffmpeg silencedetect หาจุดเงียบ แล้วเลือก split point ที่ใกล้ทุก target_chunk วินาที
    คืน list ของ (start, end) วินาที สำหรับแต่ละ chunk
    """
    duration = get_audio_duration(wav_path)
    if duration == 0:
        return [(0, None)]

    # ถ้าสั้นกว่า target_chunk*1.5 ไม่ต้องแบ่ง
    if duration <= target_chunk * 1.5:
        return [(0, None)]

    # ตรวจหาช่วงเงียบด้วย ffmpeg
    result = subprocess.run([
        "ffmpeg", "-i", wav_path,
        "-af", f"silencedetect=noise={silence_thresh}dB:d={min_silence_len}",
        "-f", "null", "-"
    ], capture_output=True, text=True)

    stderr = result.stderr

    # parse silence_end (จุดที่เงียบสิ้นสุด = จุดที่เหมาะแบ่ง)
    silence_ends = []
    for m in re.finditer(r"silence_end: ([\d.]+)", stderr):
        t = float(m.group(1))
        if t < duration - 2:
            silence_ends.append(t)

    if not silence_ends:
        # ไม่เจอช่วงเงียบ — แบ่งทุก target_chunk วินาที (hard cut)
        splits = []
        t = 0.0
        while t < duration:
            end = min(t + target_chunk, duration)
            splits.append((t, end if end < duration else None))
            t = end
        return splits

    # เลือก split point ที่ใกล้กับ target_chunk ที่สุด
    splits = []
    current_start = 0.0
    next_target = target_chunk

    while current_start < duration - 5:
        # หาช่วงเงียบที่อยู่ใกล้ next_target มากที่สุด (ยอมรับ ±15 วินาที)
        window_start = next_target - 15
        window_end = next_target + 15
        candidates = [t for t in silence_ends if window_start < t < window_end and t > current_start + 5]

        if candidates:
            # เลือกจุดที่ใกล้ next_target ที่สุด
            best = min(candidates, key=lambda t: abs(t - next_target))
        else:
            # ไม่มีในหน้าต่าง — ขยายหาใกล้สุดหลัง current_start
            candidates = [t for t in silence_ends if t > current_start + 10]
            if candidates:
                # หาจุดแรกที่เลยครึ่ง target
                after_half = [t for t in candidates if t >= current_start + target_chunk * 0.5]
                best = after_half[0] if after_half else candidates[-1]
            else:
                # ไม่มีเลย — hard cut
                best = min(current_start + target_chunk, duration)

        splits.append((current_start, best))
        current_start = best
        next_target = current_start + target_chunk

    # ส่วนที่เหลือ
    if current_start < duration - 1:
        splits.append((current_start, None))

    return splits


def extract_chunk(wav_path, start, end, out_path):
    """ตัด wav ตาม start-end วินาที บันทึกลง out_path"""
    cmd = ["ffmpeg", "-y", "-i", wav_path, "-ss", str(start)]
    if end is not None:
        cmd += ["-t", str(end - start)]
    cmd += ["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", out_path]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def repair_text(text):
    for w, r in TH_WORD_REPAIR.items():
        text = text.replace(w, r)
    return text


def transcribe_chunk(chunk_path, offset=0.0):
    """ถอดเสียง 1 chunk — ทีละอัน ป้องกัน OOM"""
    import gc
    try:
        result = model.transcribe(
            chunk_path,
            language="th",
            initial_prompt=THAI_PROMPT,
            condition_on_previous_text=False,
            temperature=(0.0, 0.2),
            no_speech_threshold=0.6,
            logprob_threshold=-1.0,
            compression_ratio_threshold=2.4,
            fp16=USE_FP16,
            verbose=False,
        )
        text = repair_text(result["text"].strip())
        segments = []
        for seg in result.get("segments", []):
            seg_text = repair_text(seg["text"].strip())
            segments.append({
                "start": round(seg["start"] + offset, 2),
                "end":   round(seg["end"] + offset, 2),
                "text":  seg_text,
            })
        return text, segments
    finally:
        # เคลียร์ memory หลังแต่ละ chunk ป้องกัน OOM สะสม
        gc.collect()
        if USE_FP16 and torch.cuda.is_available():
            torch.cuda.empty_cache()


# ==========================================
# PING
# ==========================================
@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok"})


# ==========================================
# TRANSLATE
# ==========================================
@app.route("/translate", methods=["POST"])
def translate_text():
    import urllib.request
    import urllib.parse

    data = request.get_json()
    if not data or "text" not in data or "target" not in data:
        return jsonify({"error": "ต้องระบุ text และ target"}), 400

    text = data["text"].strip()
    target = data["target"]
    if not text:
        return jsonify({"error": "ข้อความว่างเปล่า"}), 400

    try:
        params = urllib.parse.urlencode({
            "client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text
        })
        url = f"https://translate.googleapis.com/translate_a/single?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode("utf-8"))

        translated = ""
        if raw and isinstance(raw[0], list):
            for part in raw[0]:
                if part and part[0]:
                    translated += part[0]

        if translated:
            return jsonify({"translation": translated, "engine": "google"})
        else:
            return jsonify({"error": "แปลไม่สำเร็จ"}), 500

    except Exception as e:
        return jsonify({"error": f"แปลไม่สำเร็จ: {str(e)}"}), 500


# ==========================================
# TRANSCRIBE (ไม่มี streaming — fallback)
# ==========================================
@app.route("/transcribe", methods=["POST"])
def transcribe_audio():
    if "audio" not in request.files:
        return jsonify({"error": "ไม่พบไฟล์เสียงในคำขอ"}), 400

    audio_file = request.files["audio"]
    ext = os.path.splitext(audio_file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"ไม่รองรับไฟล์ประเภท {ext}"}), 400

    tmp_upload = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    tmp_upload_path = tmp_upload.name
    audio_file.save(tmp_upload_path)
    tmp_upload.close()
    tmp_wav_path = tmp_upload_path + "_converted.wav"

    chunk_paths = []
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", tmp_upload_path,
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", tmp_wav_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        splits = detect_silence_splits(tmp_wav_path, target_chunk=30)
        all_text = ""
        all_segments = []

        for i, (start, end) in enumerate(splits):
            chunk_path = tmp_wav_path + f"_chunk{i}.wav"
            chunk_paths.append(chunk_path)
            extract_chunk(tmp_wav_path, start, end, chunk_path)

        for i, (start, end) in enumerate(splits):
            text, segs = transcribe_chunk(chunk_paths[i], offset=start)
            all_text += text + " "
            all_segments.extend(segs)

        return jsonify({
            "text": all_text.strip(),
            "segments": all_segments,
            "language": "th",
            "chunks": len(splits)
        })

    except Exception as e:
        return jsonify({"error": f"เกิดข้อผิดพลาด: {str(e)}"}), 500
    finally:
        for p in [tmp_upload_path, tmp_wav_path] + chunk_paths:
            if p and os.path.exists(p):
                os.remove(p)


# ==========================================
# TRANSCRIBE STREAM — SSE + chunk-based
# ==========================================
@app.route("/transcribe-stream", methods=["POST"])
def transcribe_stream():
    if "audio" not in request.files:
        return jsonify({"error": "ไม่พบไฟล์"}), 400

    audio_file = request.files["audio"]
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
            dur_min = int(duration // 60)
            dur_sec = int(duration % 60)

            yield f"data: {json.dumps({'type':'progress','pct':6,'msg':f'กำลังวิเคราะห์ช่วงเงียบ... (ความยาว {dur_min} นาที {dur_sec} วินาที)','duration':duration})}\n\n"

            # แบ่ง chunk ตามช่วงเงียบ
            splits = detect_silence_splits(tmp_wav_path, target_chunk=30)
            total_chunks = len(splits)

            yield f"data: {json.dumps({'type':'progress','pct':8,'msg':f'แบ่งออกเป็น {total_chunks} ส่วน — เริ่มถอดเสียง...','total_chunks':total_chunks})}\n\n"

            # ตัด chunk ทั้งหมดด้วย ffmpeg ก่อน (เร็วมาก ใช้ RAM น้อย)
            for i, (start, end) in enumerate(splits):
                chunk_path = tmp_wav_path + f"_chunk{i}.wav"
                chunk_paths.append(chunk_path)
                extract_chunk(tmp_wav_path, start, end, chunk_path)

            yield f"data: {json.dumps({'type':'progress','pct':12,'msg':f'แบ่งไฟล์เสร็จ {total_chunks} ส่วน — เริ่มถอดเสียง...','total_chunks':total_chunks})}\n\n"

            # ถอดเสียงทีละ chunk ตามลำดับ (ป้องกัน OOM)
            all_texts = []
            all_segments = []

            for i, (start, end) in enumerate(splits):
                pct = 12 + int(((i + 0.5) / total_chunks) * 83)
                t_start = f"{int(start//60):02d}:{int(start%60):02d}"
                t_end_str = f"{int((end or duration)//60):02d}:{int((end or duration)%60):02d}"

                yield f"data: {json.dumps({'type':'progress','pct':pct,'msg':f'ถอดเสียงส่วนที่ {i+1}/{total_chunks} [{t_start}–{t_end_str}]...','chunk_index':i,'total_chunks':total_chunks})}\n\n"

                text, segs = transcribe_chunk(chunk_paths[i], offset=start)
                all_texts.append(text)
                all_segments.extend(segs)

                done_pct = 12 + int(((i + 1) / total_chunks) * 83)
                for seg in segs:
                    yield f"data: {json.dumps({'type':'segment','pct':done_pct,'seg':seg,'chunk_index':i})}\n\n"
                yield f"data: {json.dumps({'type':'chunk_done','pct':done_pct,'chunk_index':i,'total_chunks':total_chunks,'chunk_text':text,'chunk_start':start,'chunk_end':end or duration})}\n\n"

            final_text = " ".join(t for t in all_texts if t).strip()
            yield f"data: {json.dumps({'type':'done','pct':100,'text':final_text,'segments':all_segments,'language':'th','total_chunks':total_chunks})}\n\n"

        except Exception as e:
            import traceback
            err_detail = traceback.format_exc()
            print("TRANSCRIBE ERROR:", err_detail)
            yield f"data: {json.dumps({'type':'error','msg':str(e),'detail':err_detail[:300]})}\n\n"
        finally:
            for p in [tmp_upload_path, tmp_wav_path] + chunk_paths:
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except:
                        pass

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)