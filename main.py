import os
import json
import uuid
import zipfile
from io import BytesIO
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
import requests
from bs4 import BeautifulSoup
from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = FastAPI()

# --- 1. CORS 통과 미들웨어 ---
class ForceCORSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            response = Response(status_code=200)
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "*"
            return response
        
        try:
            response = await call_next(request)
        except Exception as e:
            response = Response(content=json.dumps({"detail": str(e)}), status_code=500, media_type="application/json")
            
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        return response

app.add_middleware(ForceCORSMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

@app.options("/{rest_of_path:path}")
async def preflight_handler(rest_of_path: str):
    response = Response(status_code=200)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response

# --- 2. 설정 및 초기화 ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "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")
client = OpenAI(api_key=OPENAI_API_KEY)

STORAGE_DIR = "task_storage"
os.makedirs(STORAGE_DIR, exist_ok=True)

def get_gspread_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_path = "google_creds.json"
    if os.path.exists(creds_path):
        creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
        return gspread.authorize(creds)
    return None

# --- 3. 데이터 모델 ---
class PointRequest(BaseModel):
    email: str

class GenerateRequest(BaseModel):
    user_email: str
    cost: int
    url: Optional[str] = None
    topic: Optional[str] = None
    keyword: str
    channel: str
    image_count: int

# --- 4. 엔드포인트 ---

@app.get("/")
def read_root():
    return {"status": "ok", "message": "BlogNet API Server is running"}

@app.post("/api/get-point")
def get_user_point(req: PointRequest):
    try:
        gc = get_gspread_client()
        if gc:
            try:
                sheet = gc.open("블로그넷_회원관리").sheet1
                cell = sheet.find(req.email)
                if cell:
                    point_val = sheet.cell(cell.row, 2).value
                    return {"point": int(point_val)}
            except Exception:
                pass
        return {"point": 5000}
    except Exception as e:
        return {"point": 5000, "error": str(e)}

@app.post("/api/generate")
def generate_content(req: GenerateRequest):
    task_id = str(uuid.uuid4())
    task_dir = os.path.join(STORAGE_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    # 1. 포인트 차감 처리 (구글 시트 연동)
    remaining_point = 0
    try:
        gc = get_gspread_client()
        if gc:
            sheet = gc.open("블로그넷_회원관리").sheet1
            cell = sheet.find(req.user_email)
            if cell:
                current_p = int(sheet.cell(cell.row, 2).value or 0)
                remaining_point = max(0, current_p - req.cost)
                sheet.update_cell(cell.row, 2, remaining_point)
            
            # 장부 기록 (블로그넷_포인트장부 시트가 있을 경우 기록)
            try:
                log_sheet = gc.open("블로그넷_포인트장부").sheet1
                log_sheet.append_row([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    req.user_email,
                    f"원고 생성 ({req.keyword})",
                    -req.cost,
                    remaining_point
                ])
            except Exception:
                pass
    except Exception as e:
        print(f"포인트 차감 에러: {e}")

    # 2. 레퍼런스 크롤링
    reference_text = ""
    if req.url:
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            resp = requests.get(req.url, headers=headers, timeout=7)
            soup = BeautifulSoup(resp.text, "html.parser")
            main_frame = soup.find("iframe", id="mainFrame")
            if main_frame:
                frame_url = "https://blog.naver.com" + main_frame["src"]
                resp = requests.get(frame_url, headers=headers, timeout=7)
                soup = BeautifulSoup(resp.text, "html.parser")
            reference_text = soup.get_text()[:2500]
        except Exception as e:
            reference_text = f"URL 참조 내용 없음 ({str(e)})"

    # 3. AI 원고 생성
    channel_prompts = {
        "blog_info": "전문적이고 신뢰도 높은 네이버 블로그 정보성 포스팅 어조",
        "blog_review": "직접 체험하고 작성한 듯한 자연스럽고 생생한 블로그 후기 어조",
        "cafe": "네이버 카페 침투 마케팅용 일상적이고 자연스러운 추천 질문/답변 어조",
        "insta": "인스타그램 피드용 트렌디하고 감성적인 줄글과 핵심 해시태그 어조"
    }
    tone = channel_prompts.get(req.channel, "자연스러운 블로그 포스팅 어조")

    system_prompt = f"""
    당신은 대한민국 최고의 바이럴 마케팅 전문 작가입니다.
    선택된 채널/스타일: {tone}
    타겟 메인 키워드: '{req.keyword}' (제목 및 본문에 4~6회 자연스럽게 녹여낼 것)
    추가 요청 내용: {req.topic if req.topic else '키워드 맞춤형 고품질 포스팅 작성'}
    참고 레퍼런스: {reference_text if reference_text else '없음'}

    [작성 가이드]
    1. 제목은 사람들의 클릭을 유도하는 매력적인 헤드라인으로 뽑아주세요.
    2. 본문은 소제목(###), 문단 나누기, 이모지를 적절히 사용하여 가독성 높게 작성해 주세요.
    3. 네이버/인스타 검색 최적화(SEO)를 고려해 풍부한 분량(최소 1,000자 이상)으로 작성해 주세요.
    """

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"키워드 '{req.keyword}'에 맞춰 실제 업로드 가능한 완벽한 원고를 작성해줘."}
            ]
        )
        content = completion.choices[0].message.content
    except Exception as e:
        content = f"[{req.keyword}] 원고 생성 중 API 응답 오류 발생: {str(e)}\n\nAPI 키 크레딧 및 상태를 확인해 주세요."

    txt_path = os.path.join(task_dir, "원고.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(content)

    # 4. 이미지 생성
    images_created = 0
    if req.image_count > 0:
        for i in range(req.image_count):
            try:
                img_resp = client.images.generate(
                    model="dall-e-3",
                    prompt=f"A clean, aesthetic marketing photo for {req.keyword}, commercial photography, high quality",
                    size="1024x1024",
                    n=1
                )
                img_data = requests.get(img_resp.data[0].url).content
                with open(os.path.join(task_dir, f"image_{i+1}.jpg"), "wb") as f:
                    f.write(img_data)
                images_created += 1
            except Exception:
                pass

    # 5. 메타데이터 저장
    meta = {
        "task_id": task_id,
        "title": f"[{req.keyword}] 마케팅 원고",
        "email": req.user_email,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "image_count": images_created
    }
    with open(os.path.join(task_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)

    return {
        "task_id": task_id,
        "content": content,
        "remaining_point": remaining_point
    }

@app.get("/api/history/{user_email}")
def get_user_history(user_email: str):
    history = []
    if os.path.exists(STORAGE_DIR):
        for t_id in os.listdir(STORAGE_DIR):
            meta_path = os.path.join(STORAGE_DIR, t_id, "meta.json")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                        if meta.get("email") == user_email:
                            history.append(meta)
                except Exception:
                    pass
    history.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"history": history}

@app.get("/api/download/{task_id}")
def download_result(task_id: str):
    task_dir = os.path.join(STORAGE_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="결과물을 찾을 수 없습니다.")

    meta_path = os.path.join(task_dir, "meta.json")
    image_count = 0
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                image_count = json.load(f).get("image_count", 0)
        except Exception:
            pass

    if image_count == 0:
        txt_path = os.path.join(task_dir, "원고.txt")
        return FileResponse(path=txt_path, filename=f"원고_{task_id[:8]}.txt", media_type="text/plain")

    memory_file = BytesIO()
    with zipfile.ZipFile(memory_file, "w") as zf:
        for root, _, files in os.walk(task_dir):
            for file in files:
                if file != "meta.json":
                    file_path = os.path.join(root, file)
                    zf.write(file_path, arcname=file)
    memory_file.seek(0)
    return StreamingResponse(
        memory_file,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=result_{task_id[:8]}.zip"}
    )
