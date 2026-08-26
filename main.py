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

class PointRequest(BaseModel):
    email: str

class GenerateRequest(BaseModel):
    user_email: str
    cost: int
    url: Optional[str] = None
    brand: Optional[str] = None
    topic: Optional[str] = None
    keyword: str
    channel: str
    image_count: int

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

    # 1. 포인트 차감
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
            
            try:
                log_sheet = gc.open("블로그넷_포인트장부").sheet1
                brand_info = f"[{req.brand}] " if req.brand else ""
                log_sheet.append_row([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    req.user_email,
                    f"원고 생성 ({brand_info}{req.keyword})",
                    -req.cost,
                    remaining_point
                ])
            except Exception:
                pass
    except Exception as e:
        print(f"포인트 차감 에러: {e}")

    # 2. URL 레퍼런스 크롤링
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

    # 3. AI 프롬프트 구성 (업체명 필수 강조)
    channel_prompts = {
        "blog_info": "전문적이고 신뢰도 높은 네이버 블로그 정보성 포스팅 어조",
        "blog_review": "직접 방문/이용하고 작성한 듯한 자연스럽고 생생한 솔직 후기 어조",
        "cafe": "맘카페/지역 커뮤니티용 일상적이고 자연스러운 추천 글/답변 어조",
        "insta": "인스타그램 피드용 트렌디하고 감성적인 줄글과 해시태그 어조"
    }
    tone = channel_prompts.get(req.channel, "자연스러운 블로그 포스팅 어조")

    brand_section = f"홍보 타겟 업체명: '{req.brand}' (★매우 중요: 제목 및 본문 전체에 브랜드명/상호명을 핵심으로 강조하여 자연스럽게 반복 언급)" if req.brand else ""

    system_prompt = f"""
    당신은 대한민국 최고의 1타 바이럴 마케팅 전문 작가입니다.
    선택된 채널/스타일: {tone}
    타겟 메인 키워드: '{req.keyword}'
    {brand_section}
    상세 특징 및 안내사항: {req.topic if req.topic else '키워드와 상호명을 부각한 고품질 포스팅 작성'}
    참고 레퍼런스: {reference_text if reference_text else '없음'}

    [원고 작성 필수 지침]
    1. 제목: 타겟 키워드와 업체명({req.brand or '업체'})이 매끄럽게 어우러진 시선 집중 클릭 유도형 헤드라인
    2. 본문:
       - 업체명/상호명을 주요 포인트마다 자연스럽게 4~6회 이상 노출
       - 소제목(###), 문단 분리, 이모지를 적극 활용해 가독성 최적화
       - 실제 이용자가 느끼는 장점, 방문 팁, 추천 이유를 1,000자 이상 풍부하게 구성
    3. 하단: 타겟 키워드 및 상호명 관련 추천 해시태그 5~10개 제공
    """

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"업체명 '{req.brand or ''}', 키워드 '{req.keyword}'에 맞춰 완성도 높은 마케팅 원고를 작성해줘."}
            ]
        )
        content = completion.choices[0].message.content
    except Exception as e:
        content = f"[{req.brand or ''} / {req.keyword}] 원고 생성 중 오류 발생: {str(e)}"

    txt_path = os.path.join(task_dir, "원고.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(content)

    # 4. 이미지 생성
    images_created = 0
    if req.image_count > 0:
        for i in range(req.image_count):
            try:
                img_prompt = f"A clean, aesthetic commercial photo for {req.brand or req.keyword}, high quality photography, professional lighting"
                img_resp = client.images.generate(
                    model="dall-e-3",
                    prompt=img_prompt,
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
    title_text = f"[{req.brand}] {req.keyword}" if req.brand else f"[{req.keyword}] 마케팅 원고"
    meta = {
        "task_id": task_id,
        "title": title_text,
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
