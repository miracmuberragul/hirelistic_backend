import os
import uvicorn
import json
import io
import time
import requests  # YENİ: Firebase REST API çağrıları için
from datetime import datetime
from typing import List, Optional
import traceback

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Body, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

# --- FIREBASE IMPORTLARI ---
import firebase_admin
from firebase_admin import credentials, firestore, storage, auth  # auth EKLENDİ

# --- AYARLAR VE PATH ---
base_path = os.path.dirname(os.path.abspath(__file__))
key_path = os.path.join(base_path, "serviceAccountKey.json")

load_dotenv()

# DİKKAT: Backend'in şifre doğrulaması yapabilmesi için Web API Key gereklidir.
FIREBASE_WEB_API_KEY = os.getenv("FIREBASE_WEB_API_KEY")

# --- GOOGLE GENAI (AI) IMPORT ---
try:
    from google.genai import Client

    ADK_AVAILABLE = True
except ImportError:
    ADK_AVAILABLE = False
    print("⚠️ Google GenAI bulunamadı - Mock mode aktif")

# --- PDF/DOCX IMPORT ---
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None
try:
    from docx import Document
except ImportError:
    Document = None

# --- FIREBASE BAŞLATMA ---
if not firebase_admin._apps:
    try:
        if os.path.exists(key_path):
            cred = credentials.Certificate(key_path)
            firebase_admin.initialize_app(cred, {
                'storageBucket': 'hirelistic.appspot.com'
            })
            print(f"✅ Firebase bağlantısı başarılı")
        else:
            print(f"❌ HATA: serviceAccountKey.json bulunamadı!")
    except Exception as e:
        print(f"⚠️ Firebase başlatma hatası: {e}")

db = firestore.client()
bucket = storage.bucket()

app = FastAPI(title="Hirelytics Backend API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- VERİ MODELLERİ ---

# 1. Auth Modelleri (YENİ)
class UserRegisterRequest(BaseModel):
    email: str
    password: str
    role: str  # 'employer' veya 'candidate'


class UserLoginRequest(BaseModel):
    email: str
    password: str


# 2. İş İlanı Modeli
class JobCreate(BaseModel):
    title: str
    company: str
    location: str
    type: str
    description: str
    employer_id: str


# 3. Analiz İsteği Modeli
class AnalysisRequest(BaseModel):
    job_id: str
    candidate_id: str
    job_description: str
    candidate_name: str
    cv_content: str


# --- AI AGENT SINIFI (Aynı Kalıyor) ---
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
                model="gemini-2.5-flash",
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
            "scores": {
                "total_score": 75,      # snake_case (get_jobs bunu camelCase'e çevirecek)
                "skill_match": 70,
                "experience_match": 80,
                "keyword_match": 75
            },
            "analysis": {
                "summary": f"Mock analiz (API hatası veya yok: {error})",
                "strengths": ["Python", "Analitik"],
                "missing_skills": ["Docker"] # snake_case
            }
        }


agent = HirelyticsAgent()


# --- ENDPOINTS ---

@app.get("/")
def health_check():
    return {"status": "Hirelytics Backend V3 Çalışıyor"}


# --- 1. AUTH İŞLEMLERİ (TAMAMEN BACKEND) ---

@app.post("/api/auth/register")
async def register_user(request: UserRegisterRequest):
    """
    1. Firebase Auth'da kullanıcı oluşturur (Admin SDK).
    2. Firestore'a kullanıcı rolünü kaydeder.
    """
    try:
        # 1. Firebase Auth Kullanıcısı Oluştur
        try:
            user_record = auth.create_user(
                email=request.email,
                password=request.password
            )
        except ValueError as e:
            # Şifre kısa veya email formatı bozuksa buraya düşer
            print(f"❌ Geçersiz Veri Hatası: {e}")
            raise HTTPException(status_code=400, detail=f"Giriş bilgileri geçersiz: {str(e)}")

        except firebase_admin.exceptions.FirebaseError as fe:
            # Firebase tarafında bir çakışma veya sunucu hatası varsa
            print(f"❌ Firebase Hatası: {fe}")
            # Hata mesajını stringe çevirip detay verelim
            error_message = str(fe)
            if "EMAIL_EXISTS" in error_message or "already exists" in error_message:
                raise HTTPException(status_code=400, detail="Bu e-posta adresi zaten kullanımda.")
            else:
                raise HTTPException(status_code=400, detail=f"Firebase Kayıt Hatası: {error_message}")

        # 2. Firestore'a Rol Kaydet
        user_data = {
            "uid": user_record.uid,
            "email": request.email,
            "role": request.role,
            "createdAt": datetime.now().isoformat()
        }
        db.collection('users').document(user_record.uid).set(user_data)

        return {"message": "Kayıt başarılı", "uid": user_record.uid, "role": request.role}

    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"❌ Genel Register Hatası: {e}")
        traceback.print_exc()  # Terminalde tam hatayı gör
        raise HTTPException(status_code=500, detail=f"Sunucu hatası: {str(e)}")

@app.post("/api/auth/login")
async def login_user(request: UserLoginRequest):
    """
    1. Firebase REST API kullanarak şifre doğrular.
    2. Firestore'dan kullanıcının rolünü çeker.
    """
    if not FIREBASE_WEB_API_KEY:
        raise HTTPException(status_code=500, detail="Backend configuration error: FIREBASE_WEB_API_KEY eksik.")

    # 1. Firebase REST API ile Şifre Doğrulama
    login_url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_WEB_API_KEY}"
    payload = {
        "email": request.email,
        "password": request.password,
        "returnSecureToken": True
    }

    try:
        response = requests.post(login_url, json=payload)
        res_json = response.json()

        if "error" in res_json:
            error_msg = res_json['error']['message']
            if "INVALID_PASSWORD" in error_msg or "EMAIL_NOT_FOUND" in error_msg:
                raise HTTPException(status_code=401, detail="E-posta veya şifre hatalı.")
            raise HTTPException(status_code=400, detail=f"Giriş başarısız: {error_msg}")

        local_id = res_json['localId']  # UID
        id_token = res_json['idToken']

        # 2. Firestore'dan Rolü Getir
        user_doc = db.collection('users').document(local_id).get()
        if not user_doc.exists:
            raise HTTPException(status_code=404, detail="Kullanıcı profili bulunamadı.")

        user_data = user_doc.to_dict()
        role = user_data.get('role', 'candidate')  # Varsayılan candidate

        return {
            "uid": local_id,
            "email": request.email,
            "token": id_token,
            "role": role,
            "message": "Giriş başarılı"
        }

    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Login Error: {e}")
        raise HTTPException(status_code=500, detail="Sunucu hatası")


# --- 2. İŞ İLANI İŞLEMLERİ (Aynı) ---
@app.get("/api/jobs")
# --- agent.py dosyasındaki get_jobs fonksiyonunu bununla değiştir ---

@app.get("/api/jobs")
async def get_jobs():
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

            # --- DÜZELTME BURADA YAPILDI ---
            # Frontend (React) camelCase beklerken, AI snake_case üretiyor.
            # Bu yüzden burada manuel eşleştirme (mapping) yapıyoruz.
            results = []
            for c in candidates:
                if c.get('analysis_result'):
                    res = c['analysis_result']

                    # Güvenli veri çekme (Hata almamak için boş sözlük {})
                    scores = res.get("scores", {})
                    analysis = res.get("analysis", {})

                    results.append({
                        "candidateName": res.get("candidate_name", c.get('name')),
                        "scores": {
                            # Python (total_score) -> React (totalScore) çevirimi
                            "totalScore": scores.get("total_score", 0),
                            "skillMatch": scores.get("skill_match", 0),
                            "experienceMatch": scores.get("experience_match", 0),
                            "keywordMatch": scores.get("keyword_match", 0)
                        },
                        "analysis": {
                            "summary": analysis.get("summary", "Özet yok"),
                            "strengths": analysis.get("strengths", []),
                            # Python (missing_skills) -> React (missingSkills) çevirimi
                            "missingSkills": analysis.get("missing_skills", [])
                        },
                        "isError": False
                    })

            job_data['analysisResults'] = results
            all_jobs.append(job_data)

        return all_jobs
    except Exception as e:
        print(f"Get Jobs Hatası: {e}")
        # Detaylı hata görmek için:
        traceback.print_exc()
        return []


@app.post("/api/jobs")
async def create_job(job: JobCreate):
    try:
        new_job = job.dict()
        new_job['created_at'] = datetime.now().isoformat()
        new_job['status'] = "Açık"
        _, ref = db.collection('jobs').add(new_job)
        return {"id": ref.id, "message": "İş oluşturuldu", "status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- 3. CV YÜKLEME VE ANALİZ (Aynı) ---
@app.post("/api/upload-cv")
async def upload_cv(
        file: UploadFile = File(...),
        job_id: str = Form(...),
        candidate_id: str = Form(default="unknown"),
        candidate_email: str = Form(default="unknown")
):
    try:
        content = ""
        file_bytes = await file.read()
        filename = file.filename.lower()

        if filename.endswith(".pdf") and PdfReader:
            reader = PdfReader(io.BytesIO(file_bytes))
            for page in reader.pages:
                if page.extract_text(): content += page.extract_text() + "\n"
        elif filename.endswith(".docx") and Document:
            doc = Document(io.BytesIO(file_bytes))
            for para in doc.paragraphs: content += para.text + "\n"
        elif filename.endswith(".txt"):
            content = file_bytes.decode("utf-8")
        else:
            content = "Metin okunamadı."

        fake_url = "https://text-only-mode.com"
        new_candidate = {
            "candidate_id": candidate_id,
            "email": candidate_email,
            "name": file.filename,
            "cv_url": fake_url,
            "content": content.strip(),
            "isParsed": True,
            "appliedAt": datetime.now().isoformat(),
            "analysis_result": None
        }
        db.collection('jobs').document(job_id).collection('candidates').add(new_candidate)
        return {"message": "Başvuru başarılı", "url": fake_url}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/analyze")
async def analyze_candidate_endpoint(request: AnalysisRequest):
    """Analiz yap ve kaydet"""
    try:
        result = agent.analyze(request.job_description, request.cv_content, request.candidate_name)

        if request.job_id and request.candidate_id:
            db.collection('jobs').document(request.job_id) \
                .collection('candidates').document(request.candidate_id) \
                .update({"analysis_result": result})

        return result
    except Exception as e:
        print(f"Analiz Endpoint Hatası: {e}")
        raise HTTPException(500, str(e))


if __name__ == "__main__":
    uvicorn.run("agent:app", host="0.0.0.0", port=8000, reload=True)