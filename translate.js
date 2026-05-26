function goBack() { window.location.href = "index.html"; }

if (typeof pdfjsLib !== 'undefined') {
  pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/2.16.105/pdf.worker.min.js';
}

const PYTHON_SERVER_URL = "http://127.0.0.1:5001";

document.getElementById("fileInput").addEventListener("change", function(event) {
  const file = event.target.files[0];
  if (!file) return;
  document.getElementById("fileName").innerText = file.name;
  processUploadedFile(file);
});

async function processUploadedFile(file) {
  const targetTextarea = document.getElementById("inputText");
  const fileNameLower = file.name.toLowerCase();
  const sizeMB = (file.size / 1024 / 1024).toFixed(1);
  targetTextarea.value = `⏳ กำลังดึงข้อมูลจากไฟล์ ${sizeMB} MB...`;

  try {
    if (fileNameLower.endsWith(".txt")) {
      const reader = new FileReader();
      reader.onload = function(e) { targetTextarea.value = e.target.result; };
      reader.readAsText(file);

    } else if (fileNameLower.endsWith(".pdf")) {
      const reader = new FileReader();
      reader.onload = async function(e) {
        const typedarray = new Uint8Array(e.target.result);
        const pdf = await pdfjsLib.getDocument(typedarray).promise;
        let fullText = "";
        for (let i = 1; i <= pdf.numPages; i++) {
          targetTextarea.value = `⏳ กำลังอ่าน PDF... หน้า ${i}/${pdf.numPages}`;
          const page = await pdf.getPage(i);
          const txtContent = await page.getTextContent();
          fullText += txtContent.items.map(item => item.str).join(" ") + "\n";
        }
        targetTextarea.value = fullText.trim();
      };
      reader.readAsArrayBuffer(file);

    } else if (/\.(jpg|jpeg|png)$/i.test(fileNameLower)) {
      targetTextarea.value = "⏳ กำลังอ่านข้อความจากรูปภาพ (OCR)...";
      const result = await Tesseract.recognize(file, 'tha+eng', {
        logger: m => {
          if (m.status === 'recognizing text') {
            targetTextarea.value = `⏳ OCR: ${Math.round(m.progress * 100)}%...`;
          }
        }
      });
      targetTextarea.value = result.data.text;

    } else if (/\.(mp3|wav|m4a|mp4|mov|ogg|flac|webm)$/i.test(fileNameLower)) {
      await transcribeFileForTranslate(file, targetTextarea);

    } else {
      targetTextarea.value = "❌ ไม่รองรับรูปแบบไฟล์นี้";
    }
  } catch (error) {
    targetTextarea.value = "❌ เกิดข้อผิดพลาด: " + error.message;
  }
}

// ==========================================
// ถอดเสียงสำหรับแปลภาษา — Chunk-based + SSE
// ==========================================
async function transcribeFileForTranslate(file, textarea) {
  const sizeMB = (file.size / 1024 / 1024).toFixed(1);
  textarea.value = `⏳ กำลังส่งไฟล์ (${sizeMB} MB)...`;

  const formData = new FormData();
  formData.append("audio", file);

  let partialText = "";
  let chunkTexts = {};
  let totalChunks = 0;

  try {
    const response = await fetch(`${PYTHON_SERVER_URL}/transcribe-stream`, {
      method: "POST", body: formData
    });
    if (!response.ok) throw new Error(`Server error ${response.status}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        let evt;
        try { evt = JSON.parse(line.slice(6)); } catch { continue; }

        if (evt.type === "progress") {
          if (evt.total_chunks) totalChunks = evt.total_chunks;
          textarea.value = `⏳ ${evt.msg} (${evt.pct}%)`;

        } else if (evt.type === "segment") {
          partialText += evt.seg.text + " ";
          textarea.value = `⏳ ถอดเสียงส่วนที่ ${(evt.chunk_index||0)+1}/${totalChunks||'?'} [${fmt(evt.seg.end)}]\n\n${partialText.trim()}`;

        } else if (evt.type === "chunk_done") {
          if (evt.total_chunks) totalChunks = evt.total_chunks;
          chunkTexts[evt.chunk_index] = evt.chunk_text;
          const doneCount = Object.keys(chunkTexts).length;
          textarea.value = `⏳ เสร็จส่วนที่ ${doneCount}/${totalChunks} — ${evt.pct||0}%\n\n${Object.values(chunkTexts).join(" ").trim()}`;

        } else if (evt.type === "done") {
          textarea.value = evt.text;

        } else if (evt.type === "error") {
          throw new Error(evt.msg);
        }
      }
    }
  } catch (err) {
    // Fallback: ไม่มี streaming
    textarea.value = "⏳ กำลังถอดเสียง (โหมดปกติ)...";
    const formData2 = new FormData();
    formData2.append("audio", file);
    try {
      const res = await fetch(`${PYTHON_SERVER_URL}/transcribe`, { method: "POST", body: formData2 });
      const data = await res.json();
      textarea.value = data.text || "❌ ถอดเสียงไม่สำเร็จ";
    } catch (e2) {
      textarea.value = "❌ เกิดข้อผิดพลาด: " + e2.message;
    }
  }
}

function fmt(s) {
  return `${Math.floor(s/60).toString().padStart(2,'0')}:${Math.floor(s%60).toString().padStart(2,'0')}`;
}

// ==========================================
// TRANSLATE — แบ่ง chunk แปลทีละส่วน
// ==========================================
function splitTextIntoChunks(text, maxLength = 1000) {
  const lines = text.split(/\n+/);
  const chunks = [];
  let current = "";

  for (const line of lines) {
    if ((current + "\n" + line).length > maxLength && current) {
      chunks.push(current.trim());
      current = line;
    } else {
      current = current ? current + "\n" + line : line;
    }
  }
  if (current.trim()) chunks.push(current.trim());
  return chunks.filter(c => c.length > 0);
}

async function translateText() {
  const inputText = document.getElementById("inputText").value;
  const targetLang = document.getElementById("language").value;
  const resultBox = document.getElementById("translateResult");
  const resultText = document.getElementById("translatedText");

  if (!inputText.trim()) {
    alert("กรุณากรอกข้อความหรืออัปโหลดไฟล์ก่อนแปลภาษา");
    return;
  }

  if (resultBox) resultBox.style.display = "block";
  resultText.innerHTML = buildProgress(5, "กำลังเตรียมแปลภาษา...");

  const chunks = splitTextIntoChunks(inputText, 1000);
  let finalTranslation = "";

  try {
    for (let i = 0; i < chunks.length; i++) {
      const pct = Math.round(((i + 0.5) / chunks.length) * 100);
      resultText.innerHTML = buildProgress(pct, `กำลังแปลส่วนที่ ${i + 1} / ${chunks.length}...`);

      const res = await fetch(`${PYTHON_SERVER_URL}/translate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: chunks[i], target: targetLang })
      });

      const data = await res.json();

      if (data && data.translation) {
        finalTranslation += data.translation + "\n";
      } else {
        finalTranslation += `[แปลส่วนที่ ${i+1} ไม่สำเร็จ: ${data.error || "unknown"}]\n`;
      }
    }

    resultText.innerHTML = `
      <div style="font-size:13px; color:#388e3c; margin-bottom:10px;">✅ แปลสำเร็จ (Google Translate)</div>
      <div style="font-size:15px; color:#222; line-height:1.8; white-space:pre-wrap;">${finalTranslation.trim()}</div>
      <button onclick="navigator.clipboard.writeText(this.dataset.t).then(()=>this.textContent='✅ คัดลอกแล้ว!')"
        data-t="${finalTranslation.trim().replace(/"/g,'&quot;')}"
        style="margin-top:12px; padding:6px 14px; border:1px solid #ccc; border-radius:6px;
        cursor:pointer; font-size:13px; background:#fff;">📋 คัดลอกคำแปล</button>`;

  } catch (error) {
    resultText.innerHTML = `<span style="color:#e53935;">❌ ${error.message}</span>`;
  }
}

function buildProgress(pct, msg) {
  return `
    <div style="text-align:center; padding:8px 0;">
      <div style="font-size:13px; color:#555; margin-bottom:8px;">⏳ ${msg}</div>
      <div style="background:#eee; border-radius:20px; height:10px; overflow:hidden; margin-bottom:4px;">
        <div style="height:100%; width:${pct}%; background:linear-gradient(90deg,#4CAF50,#2196F3);
             border-radius:20px; transition:width 0.5s ease;"></div>
      </div>
      <div style="font-size:11px; color:#aaa;">${pct}%</div>
    </div>`;
}

function resetTranslate() {
  document.getElementById("inputText").value = "";
  document.getElementById("fileInput").value = "";
  document.getElementById("fileName").innerText = "ยังไม่ได้เลือกไฟล์";
  const resultBox = document.getElementById("translateResult");
  const resultText = document.getElementById("translatedText");
  if (resultBox) resultBox.style.display = "none";
  if (resultText) resultText.innerHTML = "";
}