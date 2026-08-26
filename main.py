import os
import json
import uuid
import zipfile
import re
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

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
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

def crawl_naver_blog(url: str) -> str:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        target_url = url
        blog_id_match = re.search(r"blog\.naver\.com/([^/?&]+)/(\d+)", url)
        if blog_id_match:
            blog_id, log_no = blog_id_match.group(1), blog_id_match.group(2)
            target_url = f"https://m.blog.naver.com/{blog_id}/{log_no}"

        resp = requests.get(target_url, headers=headers, timeout=8)
        soup = BeautifulSoup(resp.text, "html.parser")

        main_content = soup.find("div", class_=re.compile(r"(se-main-container|se_component_wrap|post_ct)"))
        if main_content:
            return main_content.get_text(separator="\n", strip=True)[:4000]

        main_frame = soup.find("iframe", id="mainFrame")
        if main_frame:
            frame_url = "https://blog.naver.com" + main_frame["src"]
            resp2 = requests.get(frame_url, headers=headers, timeout=8)
            soup2 = BeautifulSoup(resp2.text, "html.parser")
            return soup2.get_text(separator="\n", strip=True)[:4000]

        return soup.get_text(separator="\n", strip=True)[:3000]
    except Exception as e:
        return f"레퍼런스 본문 추출 실패: {str(e)}"

# 특수문자 정제 (해시태그 #은 그대로 유지)
def clean_markdown_text(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"###\s*", "■ ", text)
    text = re.sub(r"##\s*", "■ ", text)
    text = re.sub(r"---", "", text)
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        if line.startswith("# ") and not line.startswith("#"):
            cleaned_lines.append(line[2:])
        else:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()

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

    # 1. 레퍼런스 크롤링
    reference_text = ""
    if req.url:
        reference_text = crawl_naver_blog(req.url)

    # 2. 프롬프트 세팅
    channel_prompts = {
        "blog_info": "전문적이고 신뢰도 높은 네이버 블로그 정보성 포스팅 어조",
        "blog_review": "직접 체험/방문하고 작성한 듯한 자연스럽고 생생한 솔직 후기 어조",
        "cafe": "맘카페/지역 커뮤니티용 일상적이고 자연스러운 추천 글/답변 어조",
        "insta": "인스타그램 피드용 감성적인 줄글과 해시태그 어조"
    }
    tone = channel_prompts.get(req.channel, "자연스러운 네이버 블로그 포스팅")
    brand_section = f"★ 홍보 대상 업체명(상호명): '{req.brand}' (본문 전반에 4~6회 이상 자연스럽게 강조)" if req.brand else ""

    system_prompt = f"""
    당신은 대한민국 1위 바이럴 마케팅 전문 원고 작가입니다.
    선택된 채널/스타일: {tone}
    타겟 메인 키워드: '{req.keyword}'
    {brand_section}
    추가 안내사항: {req.topic if req.topic else '제공된 레퍼런스 및 키워드 기반 작성'}

    [제공된 레퍼런스 본문 내용 (반드시 이 내용을 분석하여 매장 정보, 메뉴, 특장점을 반영할 것)]:
    \"\"\"
    {reference_text if reference_text else '레퍼런스 없음 (키워드 및 업체명 기반 창작)'}
    \"\"\"

    [필수 작성 규칙 - 엄격 준수]
    1. 제목 형식:
       - 반드시 맨 첫 줄에 '제목: [ 클릭을 유도하는 매력적인 헤드라인 ]' 형식으로 작성하세요.
    2. 마크다운 특수문자 금지:
       - 본문에 '**', '###', '---' 같은 마크다운 기호를 절대 쓰지 마세요.
       - 소제목은 '■ 소제목' 형태로만 깔끔하게 작성하세요.
    3. 분량 및 구성:
       - 공백 제외 순수 한글 1,500자 이상의 풍부한 분량으로 작성하세요.
       - 문단과 문단 사이에는 빈 줄을 넣어 쾌적한 가독성을 제공하세요.
    4. 하단 해시태그 형식:
       - 본문 맨 끝에 반드시 #키워드 #업체명 형태로 8~10개의 해시태그를 작성하세요. (예: #을지로맛집 #명동케이갈비 #을지로회식)
    """

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"키워드 '{req.keyword}'와 업체 정보를 완벽히 반영하여 1,500자 이상의 고품질 원고를 작성해줘."}
            ],
            temperature=0.7
        )
        raw_content = completion.choices[0].message.content
        content = clean_markdown_text(raw_content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OpenAI 생성 실패: {str(e)}")

    txt_path = os.path.join(task_dir, "원고.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(content)

    # 3. 이미지 생성 (최대 10장)
    images_created = 0
    if req.image_count > 0:
        for i in range(req.image_count):
            try:
                img_prompt = f"A clean, realistic, aesthetic photo for {req.brand or req.keyword}, commercial photography, appetizing or attractive interior"
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

    # 4. 구글 시트 포인트 차감 및 영구 누적 저장
    remaining_point = 0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    title_text = f"[{req.brand}] {req.keyword}" if req.brand else f"[{req.keyword}] 마케팅 원고"

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

            # 보관함에 계속 누적 저장
            try:
                hist_sheet = gc.open("블로그넷_원고보관함").sheet1
                hist_sheet.append_row([
                    task_id,
                    req.user_email,
                    title_text,
                    images_created,
                    now_str,
                    content[:500]
                ])
            except Exception:
                pass
    except Exception as e:
        print(f"구글 시트 연동 에러: {e}")

    meta = {
        "task_id": task_id,
        "title": title_text,
        "email": req.user_email,
        "created_at": now_str,
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
    # 구글 시트에서 전체 히스토리 영구 로드 (제한 없이 누적)
    try:
        gc = get_gspread_client()
        if gc:
            hist_sheet = gc.open("블로그넷_원고보관함").sheet1
            records = hist_sheet.get_all_values()
            for row in reversed(records[1:]):
                if len(row) >= 5 and row[1] == user_email:
                    history.append({
                        "task_id": row[0],
                        "email": row[1],
                        "title": row[2],
                        "image_count": int(row[3] or 0),
                        "created_at": row[4]
                    })
    except Exception:
        pass

    if not history and os.path.exists(STORAGE_DIR):
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
        raise HTTPException(status_code=404, detail="다운로드 대상이 존재하지 않습니다.")

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
        return FileResponse(path=txt_path, filename=f"원고_{task_id[:8]}.txt", media_type="text/plain; charset=utf-8")

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
