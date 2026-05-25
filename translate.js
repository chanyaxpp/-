// ================= ปุ่มย้อนกลับ =================
function goBack() {
  window.location.href = "index.html";
}

// ตั้งค่าให้ PDF.js รู้จักตำแหน่ง Worker (ช่วยให้ประมวลผลเร็วขึ้น ไม่ค้าง)
if (typeof pdfjsLib !== 'undefined') {
  pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/2.16.105/pdf.worker.min.js';
}

const PYTHON_SERVER_URL = "http://127.0.0.1:5001";

// ================= ตรวจจับการอัปโหลดไฟล์ =================
document.getElementById("fileInput").addEventListener("change", function(event) {
  const file = event.target.files[0];
  if (!file) return;

  // แสดงชื่อไฟล์บนหน้าจอทันที
  document.getElementById("fileName").innerText = file.name;
  
  // เรียกฟังก์ชันประมวลผลแยกต่างหากเพื่อป้องกันหน้าเว็บค้างหน่วง (ล้า)
  processUploadedFile(file);
});

// ================= ฟังก์ชันแกะข้อความจากไฟล์ (รองรับเอกสาร รูปภาพ วิดีโอ และไฟล์เสียง) =================
async function processUploadedFile(file) {
  const targetTextarea = document.getElementById("inputText");
  const fileNameLower = file.name.toLowerCase();

  targetTextarea.value = "⏳ กำลังดึงข้อมูลข้อความจากไฟล์... กรุณารอสักครู่ (ระบบจะไม่ค้าง)";

  try {
    // ---- เคสที่ 1: ไฟล์เสียง หรือไฟล์วิดีโอ (ใช้ Local AI Whisper ดึงข้อความ) ----
    if (
      file.type.includes("audio") || 
      file.type.includes("video") || 
      fileNameLower.endsWith('.mp3') || 
      fileNameLower.endsWith('.wav') || 
      fileNameLower.endsWith('.m4a') || 
      fileNameLower.endsWith('.mp4') || 
      fileNameLower.endsWith('.mov')
    ) {
      targetTextarea.value = "🧠 กำลังส่งไฟล์สื่อไปให้ Local AI ถอดเสียงเป็นข้อความ... (ขั้นตอนนี้อาจใช้เวลาสักครู่ตามขนาดไฟล์)";
      
      // เตรียมข้อมูล FormData ส่งให้ Flask Server (app.py) พอร์ต 5001 ของคุณ
      const formData = new FormData();
      formData.append("audio", file); 

      const response = await fetch(`${PYTHON_SERVER_URL}/transcribe`, {
        method: "POST",
        body: formData
      });
      
      const data = await response.json();
      
      if (data && data.text) {
        targetTextarea.value = data.text.trim();
        // แปลภาษาให้อัตโนมัติทันทีหลังถอดเสียงจากไฟล์เสียง/วิดีโอสำเร็จ
        await translateText();
      } else {
        targetTextarea.value = "❌ เกิดข้อผิดพลาด: เซิร์ฟเวอร์ไม่สามารถถอดเสียงจากไฟล์นี้ได้";
      }
    }
    
    // ---- เคสที่ 2: ไฟล์ข้อความทั่วไป (.txt) ----
    else if (file.type === "text/plain" || fileNameLower.endsWith('.txt')) {
      const reader = new FileReader();
      reader.onload = async function(e) {
        targetTextarea.value = e.target.result.trim();
        await translateText();
      };
      reader.readAsText(file, 'UTF-8');
    } 
    
    // ---- เคสที่ 3: ไฟล์รูปภาพ (ทำ OCR ดึงข้อความ) ----
    else if (file.type.includes("image") || fileNameLower.match(/\.(jpg|jpeg|png)$/i)) {
      const result = await Tesseract.recognize(file, 'tha+eng');
      const text = result.data.text.trim();
      targetTextarea.value = text || "⚠️ ไม่พบข้อความภาษาไทยหรืออังกฤษในรูปภาพนี้";
      
      if (text) {
        await translateText();
      }
    } 
    
    // ---- เคสที่ 4: ไฟล์เอกสาร PDF ----
    else if (file.type === "application/pdf" || fileNameLower.endsWith('.pdf')) {
      const reader = new FileReader();
      reader.onload = function() {
        const typedarray = new Uint8Array(this.result);
        
        pdfjsLib.getDocument({ data: typedarray }).promise.then(async function(pdf) {
          let maxPages = pdf.numPages;
          let fullText = "";
          
          for (let j = 1; j <= maxPages; j++) {
            const page = await pdf.getPage(j);
            const textContent = await page.getTextContent();
            const pageText = textContent.items.map(item => item.str).join(" ");
            fullText += pageText + "\n\n";
          }
          
          targetTextarea.value = fullText.trim() || "⚠️ ไม่พบเลเยอร์ข้อความดิจิทัลในเอกสาร PDF นี้";
          
          if (fullText.trim()) {
            await translateText();
          }
        }).catch(err => {
          console.error(err);
          targetTextarea.value = "❌ เกิดข้อผิดพลาดในการแกะข้อมูลไฟล์ PDF";
        });
      };
      reader.readAsArrayBuffer(file);
    } 
    
    // ---- เคสที่ 5: ไฟล์ไม่รองรับ ----
    else {
      targetTextarea.value = "⚠️ ไม่รองรับฟอร์แมตไฟล์นี้ (กรุณาใช้ .txt, .pdf, รูปภาพ, เสียง หรือวิดีโอ)";
    }

  } catch (error) {
    console.error("File processing error:", error);
    targetTextarea.value = "❌ เกิดข้อผิดพลาดในการเชื่อมต่อกับ Local AI Server (กรุณาตรวจสอบว่าคุณเปิด python app.py ไว้แล้ว)";
  }
}

// ================= ฟังก์ชันสำหรับส่งคำขอแปลภาษา =================
async function translateText() {
  const text = document.getElementById("inputText").value.trim();
  const targetLang = document.getElementById("language").value;
  const resultBox = document.getElementById("translateResult");
  const resultText = document.getElementById("translatedText");

  // ป้องกันการทำงานซ้ำซ้อนขณะโหลดข้อมูล
  if (!text || text.startsWith("⏳") || text.startsWith("🧠") || text.startsWith("❌")) {
    return;
  }

  resultBox.style.display = "block";
  resultText.innerText = "🧠 กำลังประมวลผลแปลภาษาด้วยระบบ Neural AI... กรุณารอสักครู่";

  try {
    const url = `https://lingva.ml/api/v1/auto/${targetLang}/${encodeURIComponent(text)}`;
    const response = await fetch(url);
    const data = await response.json();

    if (data && data.translation) {
      resultText.innerText = data.translation;
    } else {
      resultText.innerText = "❌ เซิร์ฟเวอร์ปฏิเสธการแปลข้อมูลชั่วคราว กรุณาลองอีกครั้ง";
    }
  } catch (error) {
    console.error("Translation API Error:", error);
    
    // ระบบสำรองกรณี API ตัวแรกช้า
    try {
      const fallbackUrl = `https://translate.terraprint.co/translate`;
      const fallbackResponse = await fetch(fallbackUrl, {
        method: "POST",
        body: JSON.stringify({ q: text, source: "auto", target: targetLang, format: "text" }),
        headers: { "Content-Type": "application/json" }
      });
      const fallbackData = await fallbackResponse.json();
      if (fallbackData && fallbackData.translatedText) {
        resultText.innerText = fallbackData.translatedText;
        return;
      }
    } catch (e) {
      console.error(e);
    }
    resultText.innerText = "❌ เกิดข้อผิดพลาดในการเชื่อมต่อเครือข่ายอินเทอร์เน็ต (Network Error)";
  }
}

// ================= ฟังก์ชันล้างข้อมูล (Reset) =================
function resetTranslate() {
  document.getElementById("inputText").value = "";
  document.getElementById("fileInput").value = "";
  document.getElementById("fileName").innerText = "ยังไม่ได้เลือกไฟล์";
  
  const resultBox = document.getElementById("translateResult");
  const resultText = document.getElementById("translatedText");
  if (resultBox) resultBox.style.display = "none";
  if (resultText) resultText.innerText = "";
}