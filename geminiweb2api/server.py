from typing import List, Optional, Union, Dict, Any
from fastapi import FastAPI, HTTPException, Depends, Header, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import time
import uuid
import os
import asyncio
import json
import base64
import hashlib
import secrets
import requests
import random

from .client import GeminiClient
from .conversation import ChatSession

app = FastAPI(title="GeminiWeb2API", version="0.2.0")

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Setup templates and static
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
IMAGES_DIR = os.path.join(STATIC_DIR, "images")

os.makedirs(IMAGES_DIR, exist_ok=True)

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

security = HTTPBearer(auto_error=False)

# Data file path - use /app/data for Docker, or local for development
DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "cookies.json")
COOKIE_REFRESH_INTERVAL = 1800  # 30 minutes

# =============================================================================
# Data Models
# =============================================================================

class LoginRequest(BaseModel):
    username: str
    password: str

class AddCookieRequest(BaseModel):
    cookie_str: str
    note: str = ""

class SettingsUpdate(BaseModel):
    admin_username: str = ""
    admin_password: str = ""
    api_key: str = ""
    image_mode: str = "url"
    base_url: str = ""
    image_cache_max_size: int = 512
    proxy_url: str = ""
    timeout: int = 120

# =============================================================================
# Data Storage
# =============================================================================

def load_data() -> Dict:
    """Load data from JSON file, migrating old format if needed"""
    default = {"cookies": {}, "settings": {"admin_username": "admin", "admin_password": "admin", "api_key": "sk-123456", "image_mode": "url", "base_url": "", "plugin_token": ""}}
    
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
            
            # Check if old format (has "psid" at root level)
            if "psid" in data and "cookies" not in data:
                print("Migrating old cookies.json format to new multi-cookie format...")
                old_psid = data.get("psid", "")
                old_psidts = data.get("psidts", "")
                old_api_key = data.get("api_key", "")
                old_image_mode = data.get("image_mode", "url")
                
                new_data = {
                    "cookies": {},
                    "settings": {
                        "admin_username": "admin",
                        "admin_password": "admin",
                        "api_key": old_api_key,
                        "image_mode": old_image_mode,
                        "base_url": ""
                    }
                }
                
                # Migrate existing cookie if valid
                if old_psid:
                    import hashlib
                    cookie_id = hashlib.md5(old_psid.encode()).hexdigest()[:16]
                    new_data["cookies"][cookie_id] = {
                        "psid": old_psid,
                        "psidts": old_psidts,
                        "parsed": {},
                        "status": "正常",
                        "use_count": 0,
                        "note": "从旧配置迁移",
                        "created_time": int(time.time())
                    }
                
                save_data(new_data)
                return new_data
            
            # Ensure required keys exist
            if "cookies" not in data:
                data["cookies"] = {}
            if "settings" not in data:
                data["settings"] = default["settings"]
            
            # Ensure plugin_token exists
            if "plugin_token" not in data["settings"] or not data["settings"]["plugin_token"]:
                data["settings"]["plugin_token"] = secrets.token_urlsafe(32)
                save_data(data)
            
            return data
        except Exception as e:
            print(f"Error loading data: {e}")
    
    # Generate token for new install
    default["settings"]["plugin_token"] = secrets.token_urlsafe(32)
    return default

def save_data(data: Dict):
    """Save data to JSON file"""
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_settings() -> Dict:
    return load_data().get("settings", {})

def get_cookies() -> Dict:
    return load_data().get("cookies", {})

# =============================================================================
# Authentication
# =============================================================================

# Simple token store (in production, use proper sessions/JWT)
admin_tokens: Dict[str, float] = {}  # token -> expiry timestamp

def generate_token():
    return secrets.token_hex(32)

def verify_admin_token(creds: HTTPAuthorizationCredentials = Depends(security)):
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = creds.credentials
    if token not in admin_tokens or admin_tokens[token] < time.time():
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    # Extend token validity
    admin_tokens[token] = time.time() + 3600
    return token

def verify_api_key(creds: HTTPAuthorizationCredentials = Depends(security)):
    settings = get_settings()
    api_key = settings.get("api_key", "")
    if not api_key:
        return  # No key configured
    if not creds:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    if creds.credentials != api_key:
        raise HTTPException(status_code=401, detail="Invalid API Key")

def verify_plugin_token(creds: HTTPAuthorizationCredentials = Depends(security)):
    """Verify plugin connection token"""
    settings = get_settings()
    plugin_token = settings.get("plugin_token", "")
    if not plugin_token:
        raise HTTPException(status_code=401, detail="Plugin token not configured")
    if not creds:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    if creds.credentials != plugin_token:
        raise HTTPException(status_code=401, detail="Invalid plugin token")

# =============================================================================
# Cookie Management
# =============================================================================

def parse_cookie_string(cookie_str: str) -> Dict[str, str]:
    """Parse cookie header string into dict"""
    cookies = {}
    for part in cookie_str.split(';'):
        part = part.strip()
        if '=' in part:
            key, value = part.split('=', 1)
            cookies[key.strip()] = value.strip()
    return cookies

def get_active_cookie() -> Optional[Dict]:
    """Get a random active cookie for API requests"""
    cookies = get_cookies()
    active = [c for c in cookies.values() if c.get("status") == "正常"]
    if not active:
        return None
    return random.choice(active)

def increment_cookie_usage(cookie_id: str):
    """Increment usage count for a cookie"""
    data = load_data()
    if cookie_id in data["cookies"]:
        data["cookies"][cookie_id]["use_count"] = data["cookies"][cookie_id].get("use_count", 0) + 1
        save_data(data)

def mark_cookie_failed(cookie_id: str):
    """Mark a cookie as failed"""
    data = load_data()
    if cookie_id in data["cookies"]:
        data["cookies"][cookie_id]["status"] = "失效"
        save_data(data)

# =============================================================================
# Page Routes
# =============================================================================

@app.get("/", response_class=HTMLResponse)
async def home():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin")

@app.get("/admin", response_class=HTMLResponse)
async def admin_login(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request})

@app.get("/manage", response_class=HTMLResponse)
async def manage_page(request: Request):
    return templates.TemplateResponse(request, "admin.html", {"request": request})

# =============================================================================
# Admin API
# =============================================================================

@app.post("/api/login")
def api_login(req: LoginRequest):
    settings = get_settings()
    expected_user = settings.get("admin_username", "admin")
    expected_pass = settings.get("admin_password", "admin")
    
    if req.username == expected_user and req.password == expected_pass:
        token = generate_token()
        admin_tokens[token] = time.time() + 3600  # 1 hour
        return {"success": True, "token": token}
    return {"success": False, "message": "用户名或密码错误"}

@app.get("/api/cookies")
def api_list_cookies(_: str = Depends(verify_admin_token)):
    cookies = get_cookies()
    data = []
    for cookie_id, cookie_data in cookies.items():
        data.append({
            "cookie_id": cookie_id,
            "status": cookie_data.get("status", "正常"),
            "use_count": cookie_data.get("use_count", 0),
            "note": cookie_data.get("note", ""),
            "created_time": cookie_data.get("created_time", 0)
        })
    return {"success": True, "data": data}

@app.post("/api/cookies/add")
def api_add_cookie(req: AddCookieRequest, _: str = Depends(verify_admin_token)):
    parsed = parse_cookie_string(req.cookie_str)
    psid = parsed.get("__Secure-1PSID", "")
    psidts = parsed.get("__Secure-1PSIDTS", "")
    
    if not psid:
        return {"success": False, "message": "Cookie 中缺少 __Secure-1PSID"}
    
    # Generate ID from PSID hash
    cookie_id = hashlib.md5(psid.encode()).hexdigest()[:16]
    
    data = load_data()
    data["cookies"][cookie_id] = {
        "psid": psid,
        "psidts": psidts,
        "parsed": parsed,
        "status": "正常",
        "use_count": 0,
        "note": req.note,
        "created_time": int(time.time())
    }
    save_data(data)
    
    return {"success": True, "message": "Cookie 添加成功", "cookie_id": cookie_id}

@app.delete("/api/cookies/{cookie_id}")
def api_delete_cookie(cookie_id: str, _: str = Depends(verify_admin_token)):
    data = load_data()
    if cookie_id in data["cookies"]:
        del data["cookies"][cookie_id]
        save_data(data)
        return {"success": True, "message": "删除成功"}
    return {"success": False, "message": "Cookie 不存在"}

@app.get("/api/stats")
def api_stats(_: str = Depends(verify_admin_token)):
    cookies = get_cookies()
    total = len(cookies)
    active = sum(1 for c in cookies.values() if c.get("status") == "正常")
    failed = sum(1 for c in cookies.values() if c.get("status") == "失效")
    unused = sum(1 for c in cookies.values() if c.get("use_count", 0) == 0)
    return {"success": True, "total": total, "active": active, "failed": failed, "unused": unused}

def get_cache_size() -> str:
    """Calculate current image cache size"""
    total = 0
    if os.path.exists(IMAGES_DIR):
        for f in os.listdir(IMAGES_DIR):
            fp = os.path.join(IMAGES_DIR, f)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
    mb = total / (1024 * 1024)
    return f"{mb:.1f} MB"

@app.get("/api/settings")
def api_get_settings(_: str = Depends(verify_admin_token)):
    settings = get_settings()
    # Don't return password
    return {"success": True, "data": {
        "admin_username": settings.get("admin_username", "admin"),
        "api_key": settings.get("api_key", ""),
        "image_mode": settings.get("image_mode", "url"),
        "base_url": settings.get("base_url", ""),
        "image_cache_max_size": settings.get("image_cache_max_size", 512),
        "proxy_url": settings.get("proxy_url", ""),
        "timeout": settings.get("timeout", 120),
        "plugin_token": settings.get("plugin_token", ""),
        "current_cache_size": get_cache_size()
    }}

@app.post("/api/settings")
def api_save_settings(req: SettingsUpdate, _: str = Depends(verify_admin_token)):
    data = load_data()
    if "settings" not in data:
        data["settings"] = {}
    
    if req.admin_username:
        data["settings"]["admin_username"] = req.admin_username
    if req.admin_password:  # Only update if provided
        data["settings"]["admin_password"] = req.admin_password
    data["settings"]["api_key"] = req.api_key
    data["settings"]["image_mode"] = req.image_mode
    data["settings"]["base_url"] = req.base_url
    data["settings"]["image_cache_max_size"] = req.image_cache_max_size
    data["settings"]["proxy_url"] = req.proxy_url
    data["settings"]["timeout"] = req.timeout
    
    save_data(data)
    return {"success": True, "message": "设置已保存"}

@app.post("/api/settings/plugin-token/regenerate")
def api_regenerate_plugin_token(_: str = Depends(verify_admin_token)):
    """Regenerate plugin connection token"""
    data = load_data()
    if "settings" not in data:
        data["settings"] = {}
    
    new_token = secrets.token_urlsafe(32)
    data["settings"]["plugin_token"] = new_token
    save_data(data)
    
    return {"success": True, "token": new_token, "message": "插件 Token 已重新生成"}

@app.post("/api/cache/clear")
def api_clear_cache(_: str = Depends(verify_admin_token)):
    """Clear image cache"""
    count = 0
    if os.path.exists(IMAGES_DIR):
        for f in os.listdir(IMAGES_DIR):
            fp = os.path.join(IMAGES_DIR, f)
            if os.path.isfile(fp):
                os.remove(fp)
                count += 1
    return {"success": True, "message": f"已清除 {count} 个缓存文件"}

# =============================================================================
# Plugin API (for browser extension)
# =============================================================================

class PluginCookieUpdate(BaseModel):
    cookie_str: str
    psid: Optional[str] = None
    psidts: Optional[str] = None

@app.post("/api/plugin/update-cookie")
def api_plugin_update_cookie(req: PluginCookieUpdate, _: str = Depends(verify_plugin_token)):
    """
    Receive cookie update from browser extension.
    Updates existing cookie or adds new one.
    """
    if not req.cookie_str and not req.psid:
        raise HTTPException(status_code=400, detail="Missing cookie data")
    
    # Parse cookie string
    parsed = {}
    psid = req.psid or ""
    psidts = req.psidts or ""
    
    if req.cookie_str:
        for part in req.cookie_str.split(';'):
            part = part.strip()
            if '=' in part:
                key, val = part.split('=', 1)
                parsed[key.strip()] = val.strip()
                if key.strip() == "__Secure-1PSID":
                    psid = val.strip()
                if key.strip() == "__Secure-1PSIDTS":
                    psidts = val.strip()
    
    if not psid:
        raise HTTPException(status_code=400, detail="Cookie中未找到 __Secure-1PSID")
    
    data = load_data()
    
    # Check if this PSID already exists
    existing_id = None
    for cid, cdata in data["cookies"].items():
        if cdata.get("psid") == psid:
            existing_id = cid
            break
    
    if existing_id:
        # Update existing cookie
        data["cookies"][existing_id]["psidts"] = psidts
        data["cookies"][existing_id]["parsed"] = parsed
        data["cookies"][existing_id]["status"] = "正常"
        data["cookies"][existing_id]["note"] = f"插件更新 {time.strftime('%Y-%m-%d %H:%M')}"
        save_data(data)
        return {"success": True, "message": "Cookie 已更新", "action": "updated", "cookie_id": existing_id}
    else:
        # Add new cookie
        cookie_id = hashlib.md5(psid.encode()).hexdigest()[:16]
        data["cookies"][cookie_id] = {
            "psid": psid,
            "psidts": psidts,
            "parsed": parsed,
            "status": "正常",
            "note": f"插件添加 {time.strftime('%Y-%m-%d %H:%M')}",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "use_count": 0
        }
        save_data(data)
        return {"success": True, "message": "Cookie 已添加", "action": "added", "cookie_id": cookie_id}

# =============================================================================
# Legacy Endpoints (for backward compatibility)
# =============================================================================

@app.get("/api/config")
def get_config():
    settings = get_settings()
    # Return basic config for index.html
    return {
        "api_key": settings.get("api_key", ""),
        "image_mode": settings.get("image_mode", "url")
    }

# =============================================================================
# OpenAI Compatible API
# =============================================================================

class Message(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]  # Can be string or array of content parts

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Message]
    stream: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None

class ChatCompletionResponseChoice(BaseModel):
    index: int
    message: Dict[str, str]
    finish_reason: str

class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionResponseChoice]
    usage: Dict[str, int]

def save_image_locally(url: str, cookies: Dict[str, str]) -> Optional[str]:
    """Downloads image and returns local filename"""
    try:
        if "=s" not in url:
            url += "=s2048"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        resp = requests.get(url, cookies=cookies, headers=headers, timeout=30)
        if resp.status_code == 200:
            filename = f"img_{uuid.uuid4().hex}.png"
            path = os.path.join(IMAGES_DIR, filename)
            with open(path, "wb") as f:
                f.write(resp.content)
            return filename
    except Exception as e:
        print(f"Image download failed: {e}")
    return None

def image_to_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode('utf-8')

def extract_content_and_images(content: Union[str, List[Dict[str, Any]]]) -> tuple:
    """Extract text and image files from OpenAI-style multimodal content.
    Returns (text_prompt, list_of_temp_image_paths)
    """
    if isinstance(content, str):
        return content, []
    
    text_parts = []
    image_paths = []
    
    for part in content:
        if part.get("type") == "text":
            text_parts.append(part.get("text", ""))
        elif part.get("type") == "image_url":
            image_url_obj = part.get("image_url", {})
            url = image_url_obj.get("url", "")
            
            if url.startswith("data:"):
                # Base64 encoded image
                try:
                    # data:image/jpeg;base64,/9j/4AAQSkZJRg...
                    header, b64_data = url.split(",", 1)
                    img_data = base64.b64decode(b64_data)
                    
                    # Determine extension
                    ext = "png"
                    if "jpeg" in header or "jpg" in header:
                        ext = "jpg"
                    elif "webp" in header:
                        ext = "webp"
                    
                    # Save to temp file
                    temp_path = os.path.join(IMAGES_DIR, f"upload_{uuid.uuid4().hex}.{ext}")
                    with open(temp_path, "wb") as f:
                        f.write(img_data)
                    image_paths.append(temp_path)
                except Exception as e:
                    print(f"Failed to decode base64 image: {e}")
            elif url.startswith("http"):
                # URL - download it
                try:
                    resp = requests.get(url, timeout=30)
                    if resp.status_code == 200:
                        ext = "png"
                        if "jpeg" in url or "jpg" in url:
                            ext = "jpg"
                        temp_path = os.path.join(IMAGES_DIR, f"upload_{uuid.uuid4().hex}.{ext}")
                        with open(temp_path, "wb") as f:
                            f.write(resp.content)
                        image_paths.append(temp_path)
                except Exception as e:
                    print(f"Failed to download image from URL: {e}")
    
    return " ".join(text_parts), image_paths

@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def chat_completions(req: ChatCompletionRequest, request: Request):
    if not req.messages:
        raise HTTPException(status_code=400, detail="No messages provided")
    
    last_msg = req.messages[-1]
    
    # Extract text and images from multimodal content
    prompt, image_files = extract_content_and_images(last_msg.content)
    temp_files = image_files  # Track for cleanup
    
    # Get settings for proxy
    settings = get_settings()
    proxy = settings.get("proxy_url", "") or None
    image_mode = settings.get("image_mode", "url")
    
    # Retry logic: try up to 3 times with different cookies
    max_retries = 3
    last_error = None
    tried_cookie_ids = set()
    
    for attempt in range(max_retries):
        # Get an active cookie (exclude already tried ones)
        cookies_dict = get_cookies()
        active_cookies = [
            (cid, cdata) for cid, cdata in cookies_dict.items()
            if cdata.get("status") == "正常" and cid not in tried_cookie_ids
        ]
        
        if not active_cookies:
            # No more cookies to try
            break
        
        # Pick a random active cookie
        cookie_id, cookie_data = random.choice(active_cookies)
        tried_cookie_ids.add(cookie_id)
        
        try:
            # Build full cookies dict from parsed cookies (contains all original cookies like NID, SID, etc.)
            full_cookies = {}
            if cookie_data.get("parsed"):
                full_cookies.update(cookie_data["parsed"])
            
            # Create client with full cookie set for image operations
            client = GeminiClient(
                cookie_data["psid"], 
                cookie_data.get("psidts", ""),
                proxy=proxy,
                full_cookies=full_cookies if image_files else None  # Only pass full cookies for image operations
            )
            client.init()
            
            # Use generate_content for multimodal, otherwise simple chat
            if image_files:
                output = client.generate_content(prompt, image_files, req.model, None, None)
            else:
                chat = client.start_chat(model=req.model)
                output = chat.send_message(prompt)
            
            # Increment usage on success
            increment_cookie_usage(cookie_id)
            
            content = output.text
            
            if output.candidates:
                candidate = output.candidates[output.chosen]
                for img in candidate.generated_images:
                    local_name = save_image_locally(img.image.url, client.cookies)
                    
                    final_url = img.image.url
                    
                    if local_name:
                        local_path = os.path.join(IMAGES_DIR, local_name)
                        
                        if image_mode == "base64":
                            try:
                                b64_str = image_to_base64(local_path)
                                final_url = f"data:image/png;base64,{b64_str}"
                            except:
                                pass
                        else:
                            final_url = f"{request.base_url}static/images/{local_name}"
                    
                    content += f"\n\n![Generated Image]({final_url})"
            
            # Handle Streaming Request
            if req.stream:
                from fastapi.responses import StreamingResponse
                
                async def generate_stream():
                    chunk_id = f"chatcmpl-{uuid.uuid4()}"
                    created = int(time.time())
                    
                    # Yield single chunk with full content (simulated stream)
                    # Clients usually expect small chunks, but one big chunk is valid SSE
                    chunk_data = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": req.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": content},
                                "finish_reason": None
                            }
                        ]
                    }
                    yield f"data: {json.dumps(chunk_data)}\n\n"
                    
                    # Stop chunk
                    stop_data = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": req.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop"
                            }
                        ]
                    }
                    yield f"data: {json.dumps(stop_data)}\n\n"
                    yield "data: [DONE]\n\n"
                    
                return StreamingResponse(generate_stream(), media_type="text/event-stream")

            return ChatCompletionResponse(
                id=f"chatcmpl-{uuid.uuid4()}",
                created=int(time.time()),
                model=req.model,
                choices=[
                    ChatCompletionResponseChoice(
                        index=0,
                        message={"role": "assistant", "content": content},
                        finish_reason="stop"
                    )
                ],
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            )
            
        except Exception as e:
            last_error = e
            error_msg = str(e).lower()
            
            # Check if this is a retryable error (auth/cookie related)
            is_retryable = any(keyword in error_msg for keyword in [
                "503", "401", "unauthorized", "auth", "cookie", "token", "psid", "expired"
            ])
            
            if is_retryable:
                print(f"Attempt {attempt + 1}/{max_retries} failed with cookie {cookie_id[:8]}...: {e}")
                # Mark cookie as potentially problematic (don't mark as failed yet, just increment failure count)
                if attempt == max_retries - 1:
                    # Only mark as failed on last retry
                    mark_cookie_failed(cookie_id)
            else:
                # Non-retryable error, mark cookie as failed and raise immediately
                mark_cookie_failed(cookie_id)
                raise HTTPException(status_code=500, detail=str(e))
        
        finally:
            # Cleanup temp uploaded files only on last attempt or success
            if attempt == max_retries - 1 or 'output' in locals():
                for f in temp_files:
                    try:
                        if os.path.exists(f):
                            os.remove(f)
                    except:
                        pass
    
    # All retries exhausted
    raise HTTPException(status_code=503, detail=f"All cookies failed. Last error: {last_error}")

@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {"id": "gemini-3.0-flash", "object": "model", "created": 1704067200, "owned_by": "google"},
            {"id": "gemini-3.0-flash-thinking", "object": "model", "created": 1704067200, "owned_by": "google"},
            {"id": "gemini-3.0-pro", "object": "model", "created": 1704067200, "owned_by": "google"},
        ]
    }

# =============================================================================
# Background Tasks
# =============================================================================

async def cookie_refresh_loop():
    """Periodically refresh cookies using full cookie set"""
    from .auth import rotate_1psidts
    
    while True:
        await asyncio.sleep(COOKIE_REFRESH_INTERVAL)
        print("Starting cookie refresh cycle...")
        
        data = load_data()
        cookies = data.get("cookies", {})
        updated = False
        
        for cookie_id, cookie_data in cookies.items():
            if cookie_data.get("status") != "正常":
                continue
            
            try:
                # Build FULL cookies dict from parsed cookies (contains all original cookies)
                full_cookies = {}
                if cookie_data.get("parsed"):
                    full_cookies.update(cookie_data["parsed"])
                
                # Ensure PSID and PSIDTS are present
                full_cookies["__Secure-1PSID"] = cookie_data["psid"]
                if cookie_data.get("psidts"):
                    full_cookies["__Secure-1PSIDTS"] = cookie_data["psidts"]
                
                print(f"Refreshing cookie {cookie_id[:8]}... with {len(full_cookies)} cookies")
                
                # Get proxy from settings
                settings = get_settings()
                proxy = settings.get("proxy_url", "")
                
                # Call rotate directly with full cookies
                new_psidts = rotate_1psidts(full_cookies, proxy if proxy else None)
                
                if new_psidts:
                    if new_psidts != cookie_data.get("psidts"):
                        # Update in data
                        data["cookies"][cookie_id]["psidts"] = new_psidts
                        if "parsed" in data["cookies"][cookie_id]:
                            data["cookies"][cookie_id]["parsed"]["__Secure-1PSIDTS"] = new_psidts
                        updated = True
                        print(f"Cookie {cookie_id[:8]}... refreshed with new PSIDTS")
                    else:
                        print(f"Cookie {cookie_id[:8]}... PSIDTS unchanged")
                else:
                    print(f"Cookie {cookie_id[:8]}... refresh returned None (may need re-login)")
                    
            except Exception as e:
                print(f"Failed to refresh cookie {cookie_id[:8]}...: {e}")
        
        # Save if any cookies were updated
        if updated:
            save_data(data)
            print("Cookie data saved to disk")
        
        print("Cookie refresh cycle complete")

@app.on_event("startup")
async def startup():
    # Ensure data file exists
    if not os.path.exists(DATA_FILE):
        save_data({"cookies": {}, "settings": {"admin_username": "admin", "admin_password": "admin", "api_key": "sk-123456", "image_mode": "url", "base_url": ""}})
    
    print("GeminiWeb2API started. Visit /admin to configure.")
    asyncio.create_task(cookie_refresh_loop())
