import os
import uvicorn
import json
import io
import time
from pathlib import Path
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
from dotenv import load_dotenv

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

# --- PDF/DOCX Kütüphaneleri ---
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None
    print("⚠️ pypdf yüklü değil")

try:
    from docx import Document
except ImportError:
    Document = None
    print("⚠️ python-docx yüklü değil")

# FastAPI App
app = FastAPI(title="Hirelytics Backend API", version="2.0.0")

# CORS - Claude.ai için genişletilmiş
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "https://claude.ai",
        "*"  # Geliştirme için - production'da kaldır
    ],
    allow_credentials=False,  # * ile credentials=True çakışır
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- MODELLER ---
class AnalysisRequest(BaseModel):
    job_description: str
    candidate_name: str
    cv_content: str


class AnalysisResponse(BaseModel):
    candidate_name: str
    scores: dict
    analysis: dict


# --- AGENT SINIFI ---
class HirelyticsAgent:
    def __init__(self):
        self.api_key = os.getenv("GOOGLE_API_KEY")

        if not self.api_key:
            print("❌ GOOGLE_API_KEY environment variable bulunamadı!")
            self.client = None
            return

        if ADK_AVAILABLE:
            try:
                self.client = Client(api_key=self.api_key)
                print("✅ Google GenAI Client başlatıldı")
            except Exception as e:
                print(f"❌ Client başlatma hatası: {e}")
                self.client = None
        else:
            self.client = None

    def analyze(self, job_desc: str, cv_text: str, candidate_name: str):
        """CV analizi yapar ve JSON döner"""

        if not self.client:
            return self._mock_response(candidate_name)

        prompt = f"""
Sen bir işe alım uzmanısın. Aşağıdaki iş tanımı ve CV'yi analiz edip puanlama yap.

İŞ TANIMI:
{job_desc}

ADAY CV'Sİ ({candidate_name}):
{cv_text}

ÇIKTI FORMATI (Sadece JSON döndür, açıklama yapma):
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
        "missing_skills": ["eksik yetenek 1", "eksik yetenek 2"]
    }}
}}
"""

        try:
            # Retry mekanizması (3 deneme)
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    response = self.client.models.generate_content(
                        model="gemini-2.5-flagit initsh",
                        contents=prompt
                    )

                    text = response.text.strip()
                    text = text.replace("```json", "").replace("```", "").strip()

                    result = json.loads(text)
                    print(f"✅ Analiz tamamlandı: {candidate_name}")
                    return result

                except Exception as api_error:
                    error_str = str(api_error)

                    # Rate limit hatası mı?
                    if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                        if attempt < max_retries - 1:
                            wait_time = 30 * (attempt + 1)  # 30s, 60s, 90s
                            print(f"⏳ Rate limit! {wait_time}s bekleniyor... (Deneme {attempt + 1}/{max_retries})")
                            time.sleep(wait_time)
                            continue
                        else:
                            print(f"❌ Rate limit aşıldı, mock döndürülüyor")
                            return self._mock_response(candidate_name, error="API Kota Doldu")
                    else:
                        raise api_error

        except Exception as e:
            print(f"❌ Agent hatası: {e}")
            return self._mock_response(candidate_name, error=str(e))

    def _mock_response(self, name: str, error: str = None):
        """Test için mock response"""
        return {
            "candidate_name": name,
            "scores": {
                "skill_match": 75,
                "experience_match": 80,
                "keyword_match": 70,
                "total_score": 76
            },
            "analysis": {
                "summary": f"Mock analiz ({error or 'ADK yok'})",
                "strengths": ["Python", "FastAPI"],
                "missing_skills": ["Kubernetes", "CI/CD"]
            }
        }


# Global Agent
agent = HirelyticsAgent()


# --- ENDPOINTS ---

@app.get("/")
def health_check():
    return {
        "status": "Hirelytics API Çalışıyor",
        "adk_available": ADK_AVAILABLE,
        "agent_ready": agent.client is not None
    }


@app.post("/api/upload-cv")
async def upload_cv(file: UploadFile = File(...)):
    """PDF/DOCX/TXT dosya yükleme"""

    print(f"📂 Dosya alındı: {file.filename} ({file.content_type})")

    filename = file.filename.lower()
    content = ""

    try:
        if filename.endswith(".pdf"):
            if not PdfReader:
                raise HTTPException(500, "pypdf kütüphanesi yüklü değil")

            file_bytes = await file.read()
            reader = PdfReader(io.BytesIO(file_bytes))

            for page in reader.pages:
                text = page.extract_text()
                if text:
                    content += text + "\n"

        elif filename.endswith(".docx"):
            if not Document:
                raise HTTPException(500, "python-docx kütüphanesi yüklü değil")

            file_bytes = await file.read()
            doc = Document(io.BytesIO(file_bytes))

            for para in doc.paragraphs:
                content += para.text + "\n"

        elif filename.endswith(".txt"):
            file_bytes = await file.read()
            content = file_bytes.decode("utf-8")

        else:
            raise HTTPException(400, "Desteklenmeyen format. PDF, DOCX veya TXT yükleyin.")

        if not content.strip():
            raise HTTPException(400, "Dosyadan metin okunamadı (taranmış PDF olabilir)")

        print(f"✅ Dosya işlendi: {len(content)} karakter")

        return {
            "filename": file.filename,
            "content": content.strip()
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Dosya hatası: {e}")
        raise HTTPException(500, f"Dosya işlenemedi: {str(e)}")


@app.post("/api/analyze", response_model=AnalysisResponse)
async def analyze_candidate(request: AnalysisRequest):
    """CV analizi"""

    print(f"📥 Analiz isteği: {request.candidate_name}")

    try:
        result = agent.analyze(
            job_desc=request.job_description,
            cv_text=request.cv_content,
            candidate_name=request.candidate_name
        )

        # Validation
        if "scores" not in result:
            result["scores"] = {
                "total_score": 0,
                "skill_match": 0,
                "experience_match": 0,
                "keyword_match": 0
            }

        if "analysis" not in result:
            result["analysis"] = {
                "summary": "Analiz verisi eksik",
                "strengths": [],
                "missing_skills": []
            }

        return result

    except Exception as e:
        print(f"❌ Analiz hatası: {e}")
        raise HTTPException(500, f"Analiz başarısız: {str(e)}")


if __name__ == "__main__":
    uvicorn.run(
        "agent:app",  # Dosya adınız 'agent.py' ise
        host="0.0.0.0",
        port=8000,
        reload=True
    )