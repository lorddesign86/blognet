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

# 1. CORS 강제 통과
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

SPREADSHEET_ID = "1F21WMM5DBPfvDVOrNTHL7mDSLSZQnYsaJNHBjg_FXZs"

# 2. 로컬 JSON 파일 직접 로드 (줄바꿈 오류 완전 배제)
def get_gspread_client():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets"
    ]
    
    # 1순위: GitHub에 업로드된 프로젝트 내 파일
    for filename in ["google_creds.json", "/etc/secrets/google_creds.json"]:
        if os.path.exists(filename):
            try:
                creds = ServiceAccountCredentials.from_json_keyfile_name(filename, scope)
                return gspread.authorize(creds), None
            except Exception as e:
                return None, f"파일 인증 실패({filename}): {str(e)}"
                
    # 2순위: 환경변수 파싱
    creds_raw = os.getenv("GOOGLE_CREDS_JSON", "").strip()
    if creds_raw:
        try:
            creds_dict = json.loads(creds_raw)
            if "private_key" in creds_dict:
                creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            return gspread.authorize(creds), None
        except Exception as e:
            return None, f"환경변수 파싱 실패: {str(e)}"

    return None, "google_creds.json 파일을 찾을 수 없습니다."

# 3. 워크시트 탐색
def get_target_sheet(gc):
    try:
        ss = gc.open_by_key(SPREADSHEET_ID)
        return ss.get_worksheet(0), None
    except Exception as e:
        id_err = str(e)

    try:
        ss = gc.open("블로그넷_회원관리")
        return ss.get_worksheet(0), None
    except Exception as e:
        name_err = str(e)

    try:
        all_s = gc.openall()
        if all_s:
            return all_s[0].get_worksheet(0), None
    except Exception:
        pass

    return None, f"시트 탐색 실패 (ID: {id_err})"

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

# ⭐️ 포인트 실시간 조회
@app.post("/api/get-point")
def get_user_point(req: PointRequest):
    gc, auth_err = get_gspread_client()
    if not gc:
        return {"point": 0, "err_msg": auth_err}

    sheet, sheet_err = get_target_sheet(gc)
    if not sheet:
        return {"point": 0, "err_msg": sheet_err}

    try:
        records = sheet.get_all_values()
        search_email = req.email.strip().lower()
        
        for row in records[1:]:
            if len(row) >= 1:
                cell_email = row[0].strip().lower()
                if cell_email and (cell_email == search_email or cell_email == search_email.split('@')[0] or search_email.startswith(cell_email)):
                    raw_val = row[1] if len(row) > 1 else "0"
                    clean_num = re.sub(r"[^\d]", "", str(raw_val))
                    return {"point": int(clean_num or 0), "status": "success", "user": cell_email}
        
        if len(records) >= 2 and len(records[1]) >= 2:
            raw_val = records[1][1]
            clean_num = re.sub(r"[^\d]", "", str(raw_val))
            return {"point": int(clean_num or 0), "status": "fallback_row2", "user": records[1][0]}

        return {"point": 0, "err_msg": "시트에 데이터 없음"}
    except Exception as e:
        return {"point": 0, "err_msg": f"데이터 파싱 실패: {str(e)}"}

# ⭐️ 원고 생성 및 차감
@app.post("/api/generate")
def generate_content(req: GenerateRequest):
    task_id = str(uuid.uuid4())
    task_dir = os.path.join(STORAGE_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    gc, _ = get_gspread_client()
    sheet = None
    target_row = 2
    current_p = 0
    search_email = req.user_email.strip().lower()

    if gc:
        try:
            sheet, _ = get_target_sheet(gc)
            if sheet:
                records = sheet.get_all_values()
                for row_idx, row in enumerate(records[1:], start=2):
                    if len(row) >= 1:
                        cell_email = row[0].strip().lower()
                        if cell_email and (cell_email == search_email or cell_email == search_email.split('@')[0] or search_email.startswith(cell_email)):
                            target_row = row_idx
                            raw_val = row[1] if len(row) > 1 else "0"
                            current_p = int(re.sub(r"[^\d]", "", str(raw_val)) or 0)
                            break
                
                if target_row == 2 and len(records) >= 2:
                    raw_val = records[1][1] if len(records[1]) > 1 else "0"
                    current_p = int(re.sub(r"[^\d]", "", str(raw_val)) or 0)
        except Exception as e:
            print(f"[ERROR] 시트 조회 실패: {e}")

    if current_p < req.cost:
        raise HTTPException(status_code=400, detail=f"보유 포인트가 부족합니다. (현재: {current_p}P / 필요: {req.cost}P)")

    reference_text = ""
    if req.url:
        reference_text = crawl_naver_blog(req.url)

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

    [제공된 레퍼런스 본문 내용]:
    \"\"\"
    {reference_text if reference_text else '레퍼런스 없음 (키워드 및 업체명 기반 창작)'}
    \"\"\"

    [필수 작성 규칙 - 엄격 준수]
    1. 제목 형식: 반드시 맨 첫 줄에 '제목: [ 클릭을 유도하는 매력적인 헤드라인 ]' 형식으로 작성
    2. 마크다운 특수문자 금지: '**', '###', '---' 사용 금지, 소제목은 '■ 소제목' 형태 사용
    3. 분량: 공백 제외 순수 한글 1,500자 이상 및 문단별 줄바꿈 2회
    4. 하단 해시태그: 맨 끝에 #키워드 #업체명 형태로 8~10개 제공
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

    images_created = 0
    if req.image_count > 0:
        for i in range(req.image_count):
            try:
                img_prompt = f"A clean, realistic, aesthetic commercial photo for {req.brand or req.keyword}, high quality photography"
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

    remaining_point = max(0, current_p - req.cost)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    title_text = f"[{req.brand}] {req.keyword}" if req.brand else f"[{req.keyword}] 마케팅 원고"

    try:
        if sheet and target_row:
            sheet.update_cell(target_row, 2, remaining_point)
            raw_used = sheet.cell(target_row, 3).value or "0"
            prev_used = int(re.sub(r"[^\d]", "", str(raw_used)) or 0)
            sheet.update_cell(target_row, 3, prev_used + req.cost)
            sheet.update_cell(target_row, 4, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:
        print(f"[ERROR] 시트 포인트 차감 에러: {e}")

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
    if os.path.exists(STORAGE_DIR):
        for t_id in os.listdir(STORAGE_DIR):
            meta_path = os.path.join(STORAGE_DIR, t_id, "meta.json")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
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
