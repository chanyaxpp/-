function goBack() { window.location.href = "index.html"; }

function setStatus(type, text) {
  const badge = document.getElementById("statusBadge");
  const span  = document.getElementById("statusText");
  if (!badge || !span) return;
  badge.className = "status-badge st-" + type;
  span.textContent = text;
  const dot = badge.querySelector(".dot");
  if (dot) dot.className = (type === "listening" || type === "processing") ? "dot pulse" : "dot";
}
const PYTHON_URL = "http://127.0.0.1:5001";
let recognition  = null;

// ==========================================
// 1. ตรวจสอบ Python Server ตอนโหลดหน้า
// ==========================================
window.addEventListener("DOMContentLoaded", () => {
  const input = document.getElementById("mediaFile");
  if (input) {
    input.addEventListener("change", (e) => {
      const file = e.target.files[0];
      if (!file) return;
      document.getElementById("mediaName").innerText = file.name;
      document.getElementById("speechResult").innerText = "📁 เลือกไฟล์สำเร็จ — กดปุ่มด้านล่างเพื่อเริ่มถอดเสียง";
      setStatus("idle", "มีไฟล์พร้อมแปลง");
    });
  }
  checkServer();
});

async function checkServer() {
  const box = document.getElementById("speechResult");
  try {
    const res = await fetch(PYTHON_URL + "/ping", { signal: AbortSignal.timeout(3000) });
    const d   = await res.json();
    if (d.status === "ok") {
      box.innerText = "✅ เชื่อมต่อ Python Server สำเร็จ — พร้อมถอดเสียงแม่นยำสูงด้วย Whisper (medium)";
      setStatus("idle", "พร้อมใช้งาน");
      return;
    }
  } catch (_) {}
  box.innerText = "⚠️ ไม่พบ Python Server\nกรุณาเปิด Terminal แล้วรัน: python3 app.py";
  setStatus("error", "ไม่พบ Python Server");
}

// ==========================================
// 2. แปลงไฟล์ทุกประเภท → WAV 16kHz mono
// ==========================================
async function convertToWav16k(file) {
  const arrayBuffer = await file.arrayBuffer();
  const audioCtx    = new AudioContext({ sampleRate: 16000 });
  const audioBuffer = await audioCtx.decodeAudioData(arrayBuffer);
  await audioCtx.close();

  // รวมทุก channel → mono
  const numCh = audioBuffer.numberOfChannels;
  const len   = audioBuffer.length;
  const mono  = new Float32Array(len);
  for (let ch = 0; ch < numCh; ch++) {
    const d = audioBuffer.getChannelData(ch);
    for (let i = 0; i < len; i++) mono[i] += d[i];
  }
  for (let i = 0; i < len; i++) mono[i] /= numCh;

  // Float32 → PCM Int16
  const pcm = new Int16Array(len);
  for (let i = 0; i < len; i++) {
    const s = Math.max(-1, Math.min(1, mono[i]));
    pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }

  // เขียน WAV header
  const wav = new ArrayBuffer(44 + pcm.byteLength);
  const v   = new DataView(wav);
  const w   = (off, str) => [...str].forEach((c, i) => v.setUint8(off + i, c.charCodeAt(0)));
  w(0, "RIFF"); v.setUint32(4,  36 + pcm.byteLength, true);
  w(8, "WAVE"); w(12, "fmt ");
  v.setUint32(16, 16,    true);
  v.setUint16(20, 1,     true);  // PCM
  v.setUint16(22, 1,     true);  // mono
  v.setUint32(24, 16000, true);
  v.setUint32(28, 32000, true);
  v.setUint16(32, 2,     true);
  v.setUint16(34, 16,    true);
  w(36, "data"); v.setUint32(40, pcm.byteLength, true);
  new Int16Array(wav, 44).set(pcm);

  return new File([wav], "audio_16k.wav", { type: "audio/wav" });
}

// ==========================================
// 3. ถอดเสียงจากไฟล์ → ส่ง Python Server
// ==========================================
async function confirmFile() {
  const file = document.getElementById("mediaFile").files[0];
  if (!file) { alert("กรุณาเลือกไฟล์ก่อนครับ"); return; }

  const box = document.getElementById("speechResult");

  // ขั้นตอน 1: แปลงเป็น WAV 16kHz
  box.innerHTML = '<span style="color:#aaa;">⏳ กำลังเตรียมและปรับความถี่ไฟล์เสียงแบบดิจิทัล...</span>';
  setStatus("processing", "กำลังเตรียมไฟล์...");
  let wavFile;
  try {
    wavFile = await convertToWav16k(file);
  } catch (e) {
    box.innerHTML = '<div style="color:#e53935;">❌ แปลงไฟล์ไม่ได้: ' + e.message + '</div>';
    setStatus("error", "แปลงไฟล์ล้มเหลว");
    return;
  }

  // ขั้นตอน 2: ส่ง Python Server
  box.innerHTML = '<span style="color:#aaa;">⏳ ไฟล์เสียงพร้อมแล้ว! เซิร์ฟเวอร์กำลังใช้โมเดล Medium ถอดความความแม่นยำสูง...</span>';
  setStatus("processing", "Python กำลังถอดความ...");
  try {
    const form = new FormData();
    form.append("audio", wavFile);
    const res = await fetch(PYTHON_URL + "/transcribe", { method: "POST", body: form });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    const text = data.text || "";
    if (!text) throw new Error("ได้ข้อความว่างเปล่า");

    box.innerHTML =
      '<div style="font-size:13px;color:#888;margin-bottom:8px;">📁 ' + file.name + ' · Python Server (Whisper medium)</div>' +
      '<div style="font-size:16px;color:#222;line-height:1.8;white-space:pre-wrap;border-left:3px solid #4caf50;padding-left:12px;">' + text + '</div>' +
      '<button onclick="navigator.clipboard.writeText(this.dataset.t).then(()=>this.textContent=\'✅ คัดลอกแล้ว!\')" ' +
      'data-t="' + text.replace(/"/g, "&quot;") + '" ' +
      'style="margin-top:10px;padding:6px 14px;border:1px solid #ccc;border-radius:6px;cursor:pointer;font-size:13px;background:#fff;">📋 คัดลอกข้อความ</button>';
    setStatus("done", "ถอดความสำเร็จ");

  } catch (e) {
    box.innerHTML = '<div style="color:#e53935;">⚠️ ' + e.message + '<br>กรุณาตรวจสอบว่ารัน app.py อยู่ใน Terminal ครับ</div>';
    setStatus("error", "ถอดความล้มเหลว");
  }
}

// ==========================================
// 4. อัดเสียงสด (Web Speech API)
// ==========================================
function startSpeech() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { alert("กรุณาใช้ Google Chrome ครับ"); return; }
  recognition = new SR();
  recognition.lang = "th-TH";
  recognition.continuous = true;
  recognition.interimResults = true;
  const box = document.getElementById("speechResult");
  box.innerHTML = '<span style="color:#aaa;">🎙️ กำลังฟัง... พูดได้เลยครับ</span>';
  setStatus("listening", "กำลังฟังเสียง...");
  let finalText = "";
  recognition.onresult = function(e) {
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const t = e.results[i][0].transcript;
      e.results[i].isFinal ? (finalText += t) : (interim += t);
    }
    box.innerHTML =
      '<div style="font-size:13px;color:#888;margin-bottom:8px;">🎙️ อัดเสียงสด</div>' +
      '<div style="font-size:16px;color:#222;line-height:1.6;">' + finalText +
      '<span style="color:#aaa;">' + interim + '</span></div>';
  };
  recognition.onerror = (e) => {
    box.innerHTML = '<span style="color:#e53935;">❌ ข้อผิดพลาด: ' + e.error + '</span>';
    setStatus("error", "เกิดข้อผิดพลาด");
  };
  recognition.onend = () => {
    if (document.getElementById("statusText").textContent === "กำลังฟังเสียง...") {
      setStatus("done", "เสร็จสิ้น");
    }
  };
  recognition.start();
}

function stopSpeech() {
  if (recognition) { recognition.stop(); setStatus("done", "หยุดบันทึกแล้ว"); }
}

// ==========================================
// 5. Reset
// ==========================================
function resetSpeech() {
  if (recognition) recognition.stop();
  document.getElementById("speechResult").innerText = "ข้อความจะแสดงตรงนี้";
  document.getElementById("mediaName").innerText = "ยังไม่ได้เลือกไฟล์";
  document.getElementById("mediaFile").value = "";
  setStatus("idle", "พร้อมใช้งาน");
}