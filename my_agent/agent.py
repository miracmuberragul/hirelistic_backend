import os
import uvicorn
import json
import io
import time
import uuid
from datetime import datetime
from typing import List, Optional
import traceback

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# --- FIREBASE ---
import firebase_admin
from firebase_admin import credentials, firestore, storage
import firebase_admin
from firebase_admin import credentials

base_path = os.path.dirname(os.path.abspath(__file__))
key_path = os.path.join(base_path, "serviceAccountKey.json")


# .env dosyasını yükle
load_dotenv()

# --- GOOGLE ADK IMPORT ---
try:
    from google.genai import Client

    ADK_AVAILABLE = True
    print("✅ Google GenAI kütüphanesi yüklendi")
except ImportError:
    ADK_AVAILABLE = False
    print("⚠️ Google GenAI bulunamadı - Mock mode aktif")

# --- PDF/DOCX ---
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    from docx import Document
except ImportError:
    Document = None

# --- FIREBASE BAŞLATMA ---
# serviceAccountKey.json dosyasının main.py ile aynı yerde olduğundan emin ol
if not firebase_admin._apps:
    try:
        cred = credentials.Certificate(key_path)
        firebase_admin.initialize_app(cred, {
            'storageBucket': 'hirelistic.firebasestorage.app'
        })
        print("✅ Firebase bağlantısı başarılı")
    except Exception as e:
        print(f"⚠️ Firebase hatası: {e} (Mock modunda çalışamaz, serviceAccountKey.json gerekli)")

db = firestore.client()
bucket = storage.bucket()

# FastAPI App
app = FastAPI(title="Hirelytics Backend API", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Geliştirme için *
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- MODELLER ---
class JobCreate(BaseModel):
    title: str
    company: str
    location: str
    type: str
    description: str


class AnalysisRequest(BaseModel):
    job_id: str
    candidate_id: str
    job_description: str
    candidate_name: str
    cv_content: str


# --- AGENT SINIFI ---
class HirelyticsAgent:
    def __init__(self):
        self.api_key = os.getenv("GOOGLE_API_KEY")
        if ADK_AVAILABLE and self.api_key:
            self.client = Client(api_key=self.api_key)
        else:
            self.client = None

    def analyze(self, job_desc: str, cv_text: str, candidate_name: str):
        if not self.client:
            return self._mock_response(candidate_name)

        prompt = f"""
Sen bir işe alım uzmanısın. Aşağıdaki iş tanımı ve CV'yi analiz et.

İŞ TANIMI:
{job_desc}

ADAY CV'Sİ ({candidate_name}):
{cv_text}

ÇIKTI FORMATI (Sadece saf JSON döndür, markdown kullanma):
{{
    "candidate_name": "{candidate_name}",
    "scores": {{
        "skill_match": 0-100,
        "experience_match": 0-100,
        "keyword_match": 0-100,
        "total_score": 0-100
    }},
    "analysis": {{
        "summary": "Kısa özet",
        "strengths": ["güçlü yön 1", "güçlü yön 2"],
        "missing_skills": ["eksik 1", "eksik 2"]
    }}
}}
"""
        try:
            response = self.client.models.generate_content(
                model="gemini-2.5-flash",  # Model adını kendine göre güncelle
                contents=prompt
            )
            text = response.text.strip()
            # Markdown temizliği
            if text.startswith("```json"):
                text = text[7:]
            if text.endswith("```"):
                text = text[:-3]
            return json.loads(text.strip())
        except Exception as e:
            print(f"Agent Error: {e}")
            return self._mock_response(candidate_name, str(e))

    def _mock_response(self, name, error=None):
        return {
            "candidate_name": name,
            "scores": {"total_score": 75, "skill_match": 70, "experience_match": 80, "keyword_match": 75},
            "analysis": {
                "summary": f"Mock analiz (API hatası veya yok: {error})",
                "strengths": ["Python", "Analitik"],
                "missing_skills": ["Docker"]
            }
        }


agent = HirelyticsAgent()


# --- ENDPOINTS ---

@app.get("/")
def health_check():
    return {"status": "Hirelytics Firebase API Çalışıyor"}


@app.get("/api/jobs")
async def get_jobs():
    """Tüm işleri ve altındaki adayları getir"""
    try:
        jobs_ref = db.collection('jobs')
        docs = jobs_ref.stream()

        all_jobs = []
        for doc in docs:
            job_data = doc.to_dict()
            job_data['id'] = doc.id

            # Adayları çek
            candidates = []
            cand_ref = jobs_ref.document(doc.id).collection('candidates')
            for c in cand_ref.stream():
                c_data = c.to_dict()
                c_data['id'] = c.id
                candidates.append(c_data)

            job_data['candidates'] = candidates

            # Frontend için analiz sonuçlarını derle
            results = []
            for c in candidates:
                if c.get('analysis_result'):
                    res = c['analysis_result']
                    # Frontend yapısına uydurma
                    results.append({
                        "candidateName": res.get("candidate_name"),
                        "scores": {
                            "totalScore": res["scores"].get("total_score", 0),
                            "skillMatch": res["scores"].get("skill_match", 0),
                            "experienceMatch": res["scores"].get("experience_match", 0),
                            "keywordMatch": res["scores"].get("keyword_match", 0)
                        },
                        "analysis": {
                            "summary": res["analysis"].get("summary", ""),
                            "strengths": res["analysis"].get("strengths", []),
                            "missingSkills": res["analysis"].get("missing_skills", [])
                        },
                        "isError": False
                    })
            job_data['analysisResults'] = results

            all_jobs.append(job_data)

        return all_jobs
    except Exception as e:
        print(f"Get Jobs Hatası: {e}")
        return []  # Hata olursa boş liste dön



@app.post("/api/jobs")
async def create_job(job: JobCreate):
    """Yeni iş ilanı ekle"""
    print("📥 İlan Ekleme İsteği Geldi...")
    print(f"📦 Veri: {job}")

    try:
        # Veritabanı bağlantısı var mı kontrol et
        if db is None:
            raise Exception("Veritabanı bağlantısı (db) başlatılamadı. serviceAccountKey.json dosyasını kontrol edin.")

        new_job = job.dict()
        new_job['created_at'] = datetime.now().isoformat()
        new_job['status'] = "Açık"

        # Adaylar listesi boş olarak başlatılsın (Frontend hatasını önlemek için)
        new_job['candidates'] = []
        new_job['analysisResults'] = []

        print("🔥 Firestore'a yazılıyor...")
        _, ref = db.collection('jobs').add(new_job)

        print(f"✅ Başarılı! ID: {ref.id}")
        return {"id": ref.id, "message": "İş oluşturuldu", "status": "success"}

    except Exception as e:
        print("❌ HATA OLUŞTU (create_job):")
        print("-" * 60)
        traceback.print_exc()  # Hatanın tüm detayını terminale basar
        print("-" * 60)
        # Frontend'e hatayı string olarak dönüyoruz ki alert'te görebilesin
        raise HTTPException(status_code=500, detail=f"Sunucu Hatası: {str(e)}")


@app.post("/api/upload-cv")
async def upload_cv(file: UploadFile = File(...), job_id: str = Form(...)):
    """
    DEĞİŞİKLİK: Dosyayı Cloud Storage'a yüklemek yerine
    sadece metni okuyup Firestore'a kaydeder.
    Böylece 'Billing/Upgrade' sorunu çözülür.
    """
    try:
        # 1. Metin Çıkarma (Burası aynı kalıyor)
        content = ""
        file_bytes = await file.read()
        filename = file.filename.lower()

        if filename.endswith(".pdf") and PdfReader:
            reader = PdfReader(io.BytesIO(file_bytes))
            for page in reader.pages:
                text = page.extract_text()
                if text: content += text + "\n"
        elif filename.endswith(".docx") and Document:
            doc = Document(io.BytesIO(file_bytes))
            for para in doc.paragraphs: content += para.text + "\n"
        elif filename.endswith(".txt"):
            content = file_bytes.decode("utf-8")
        else:
            content = "Metin okunamadı."

        # EĞER METİN BOŞSA HATA VERELİM
        if not content.strip():
            return {"message": "Dosyadan metin okunamadı, resim formatında olabilir.", "url": "#"}

        # 2. STORAGE ADIMINI ATLIYORUZ (İptal edilen kısım)
        # blob = bucket.blob(...)  <-- BU SATIRLARI SİLDİK
        # blob.upload_from_string(...)

        # Onun yerine sahte bir URL veriyoruz (Frontend hata vermesin diye)
        fake_url = "https://dosya-yuklenmedi-sadece-metin-analizi.com"

        # 3. Firestore'a Ekle (Metni kaydediyoruz, bu bize yeter)
        new_candidate = {
            "name": file.filename,
            "email": "belirsiz@ornek.com",
            "cv_url": fake_url,  # Gerçek dosya yok, ama sorun değil
            "content": content.strip(),  # ASIL ÖNEMLİ OLAN BU
            "isParsed": True,
            "appliedAt": datetime.now().isoformat(),
            "analysis_result": None
        }

        # Veritabanına yaz
        db.collection('jobs').document(job_id).collection('candidates').add(new_candidate)

        return {"message": "Başarılı (Depolama atlandı)", "url": fake_url}

    except Exception as e:
        print(f"Upload Hatası: {e}")
        # Detaylı hata görelim
        traceback.print_exc()
        raise HTTPException(500, str(e))


@app.post("/api/analyze")
async def analyze_candidate_endpoint(request: AnalysisRequest):
    """Analiz yap ve kaydet"""
    try:
        result = agent.analyze(request.job_description, request.cv_content, request.candidate_name)

        # Firestore güncelle
        if request.job_id and request.candidate_id:
            ref = db.collection('jobs').document(request.job_id) \
                .collection('candidates').document(request.candidate_id)
            ref.update({"analysis_result": result})

        return result
    except Exception as e:
        print(f"Analiz Endpoint Hatası: {e}")
        raise HTTPException(500, str(e))


if __name__ == "__main__":
    uvicorn.run("agent:app", host="0.0.0.0", port=8000, reload=True)