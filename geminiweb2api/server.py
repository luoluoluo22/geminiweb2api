from typing import List, Optional, Union, Dict, Any
from fastapi import FastAPI, HTTPException, Depends, Header, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import time
import uuid
import os
import asyncio
import threading
import json
import base64
import hashlib
import secrets
import requests
import random
from urllib.parse import urlsplit, urlparse
from redis import Redis

from .client import GeminiClient
from .conversation import ChatSession
from .auth import rotate_1psidts

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
IS_VERCEL = os.environ.get("VERCEL") == "1"
IMAGE_CACHE_DIR = os.environ.get(
    "IMAGE_CACHE_DIR",
    "/tmp/geminiweb2api-images" if IS_VERCEL else os.path.join(STATIC_DIR, "images")
)

os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)

TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
HAS_TEMPLATES = os.path.isdir(TEMPLATES_DIR)
HAS_STATIC = os.path.isdir(STATIC_DIR)

templates = Jinja2Templates(directory=TEMPLATES_DIR) if HAS_TEMPLATES else None
if HAS_STATIC:
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

security = HTTPBearer(auto_error=False)

# Data file path - use /app/data for Docker, or local for development
DATA_DIR = os.environ.get("DATA_DIR", ".")
if IS_VERCEL and DATA_DIR == ".":
    DATA_DIR = "/tmp/geminiweb2api-data"
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "cookies.json")
COOKIE_REFRESH_INTERVAL = 900  # 15 minutes
COOKIE_HEALTHCHECK_INTERVAL = 21600  # 6 hours
COOKIE_COOLDOWN_SECONDS = 300
data_lock = threading.Lock()

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

    if redis_storage_enabled():
        try:
            raw_data = redis_get_json()
            if raw_data:
                data = raw_data
            else:
                data = apply_env_bootstrap(default)
            return finalize_loaded_data(data, default)
        except Exception as e:
            print(f"Error loading data from Redis URL: {e}")

    if upstash_enabled():
        try:
            raw_data = upstash_get_json()
            if raw_data:
                data = raw_data
            else:
                data = apply_env_bootstrap(default)
            return finalize_loaded_data(data, default)
        except Exception as e:
            print(f"Error loading data from Upstash: {e}")

    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
            return finalize_loaded_data(data, default)
        except Exception as e:
            print(f"Error loading data: {e}")
    
    # Generate token for new install
    default["settings"]["plugin_token"] = secrets.token_urlsafe(32)
    return apply_env_bootstrap(default)

def save_data(data: Dict):
    """Save data to JSON file"""
    if redis_storage_enabled():
        redis_set_json(data)
        return
    if upstash_enabled():
        upstash_set_json(data)
        return
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_settings() -> Dict:
    return load_data().get("settings", {})

def get_cookies() -> Dict:
    return load_data().get("cookies", {})

def normalize_cookie_record(cookie_id: str, cookie_data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize cookie metadata to support server-side keepalive."""
    cookie_data.setdefault("parsed", {})
    cookie_data.setdefault("status", "正常")
    cookie_data.setdefault("use_count", 0)
    cookie_data.setdefault("note", "")
    cookie_data.setdefault("failure_count", 0)
    cookie_data.setdefault("last_error", "")
    cookie_data.setdefault("last_refresh_time", 0)
    cookie_data.setdefault("last_check_time", 0)
    cookie_data.setdefault("cooldown_until", 0)
    if "created_time" not in cookie_data:
        created_at = cookie_data.get("created_at")
        if created_at:
            try:
                cookie_data["created_time"] = int(time.mktime(time.strptime(created_at, "%Y-%m-%d %H:%M:%S")))
            except Exception:
                cookie_data["created_time"] = int(time.time())
        else:
            cookie_data["created_time"] = int(time.time())
    if cookie_data.get("psid"):
        cookie_data["cookie_id"] = cookie_id
    return cookie_data

def build_full_cookie_dict(cookie_data: Dict[str, Any]) -> Dict[str, str]:
    full_cookies: Dict[str, str] = {}
    if cookie_data.get("parsed"):
        full_cookies.update(cookie_data["parsed"])
    if cookie_data.get("psid"):
        full_cookies["__Secure-1PSID"] = cookie_data["psid"]
    if cookie_data.get("psidts"):
        full_cookies["__Secure-1PSIDTS"] = cookie_data["psidts"]
    return full_cookies

def update_cookie_record(cookie_id: str, **updates: Any) -> bool:
    with data_lock:
        data = load_data()
        if cookie_id not in data["cookies"]:
            return False
        normalize_cookie_record(cookie_id, data["cookies"][cookie_id])
        data["cookies"][cookie_id].update(updates)
        save_data(data)
        return True

def persist_cookie_state(cookie_id: str, cookies: Dict[str, str], status: str = "正常"):
    with data_lock:
        data = load_data()
        cookie_data = data["cookies"].get(cookie_id)
        if not cookie_data:
            return
        normalize_cookie_record(cookie_id, cookie_data)
        cookie_data["parsed"] = cookies.copy()
        cookie_data["psid"] = cookies.get("__Secure-1PSID", cookie_data.get("psid", ""))
        cookie_data["psidts"] = cookies.get("__Secure-1PSIDTS", cookie_data.get("psidts", ""))
        cookie_data["status"] = status
        cookie_data["failure_count"] = 0
        cookie_data["last_error"] = ""
        cookie_data["last_check_time"] = int(time.time())
        if cookie_data.get("psidts"):
            cookie_data["last_refresh_time"] = int(time.time())
        save_data(data)

def get_request_timeout() -> int:
    settings = get_settings()
    timeout = settings.get("timeout", 120)
    try:
        return max(30, int(timeout))
    except (TypeError, ValueError):
        return 120

def get_effective_image_mode(settings: Dict[str, Any]) -> str:
    configured = settings.get("image_mode", "url")
    if IS_VERCEL and configured == "url":
        return "base64"
    return configured

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

def apply_env_bootstrap(data: Dict[str, Any]) -> Dict[str, Any]:
    """Initialize settings/cookies from environment variables for serverless deploys."""
    settings = data.setdefault("settings", {})
    settings["admin_username"] = os.environ.get("BOOTSTRAP_ADMIN_USERNAME", settings.get("admin_username", "admin")).strip()
    settings["admin_password"] = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", settings.get("admin_password", "admin")).strip()
    settings["api_key"] = os.environ.get("BOOTSTRAP_API_KEY", settings.get("api_key", "sk-123456")).strip()
    settings["image_mode"] = os.environ.get("BOOTSTRAP_IMAGE_MODE", settings.get("image_mode", "url")).strip()
    settings["base_url"] = os.environ.get("BOOTSTRAP_BASE_URL", settings.get("base_url", "")).strip()
    settings["proxy_url"] = os.environ.get("BOOTSTRAP_PROXY_URL", settings.get("proxy_url", "")).strip()

    timeout = os.environ.get("BOOTSTRAP_TIMEOUT")
    if timeout:
        try:
            settings["timeout"] = max(30, int(timeout))
        except ValueError:
            pass

    cookie_str = os.environ.get("BOOTSTRAP_COOKIE_STRING", "").strip()
    if cookie_str and not data.get("cookies"):
        parsed = parse_cookie_string(cookie_str)
        psid = parsed.get("__Secure-1PSID", "")
        if psid:
            cookie_id = hashlib.md5(psid.encode()).hexdigest()[:16]
            data["cookies"] = {
                cookie_id: {
                    "psid": psid,
                    "psidts": parsed.get("__Secure-1PSIDTS", ""),
                    "parsed": parsed,
                    "status": "正常",
                    "use_count": 0,
                    "failure_count": 0,
                    "last_error": "",
                    "last_refresh_time": int(time.time()),
                    "last_check_time": int(time.time()),
                    "cooldown_until": 0,
                    "note": "Bootstrapped from environment",
                    "created_time": int(time.time())
                }
            }
    return data

def finalize_loaded_data(data: Dict[str, Any], default: Dict[str, Any]) -> Dict[str, Any]:
    if "psid" in data and "cookies" not in data:
        print("Migrating old cookies.json format to new multi-cookie format...")
        old_psid = data.get("psid", "")
        old_psidts = data.get("psidts", "")
        old_api_key = data.get("api_key", "")
        old_image_mode = data.get("image_mode", "url")

        data = {
            "cookies": {},
            "settings": {
                "admin_username": "admin",
                "admin_password": "admin",
                "api_key": old_api_key,
                "image_mode": old_image_mode,
                "base_url": ""
            }
        }

        if old_psid:
            cookie_id = hashlib.md5(old_psid.encode()).hexdigest()[:16]
            data["cookies"][cookie_id] = {
                "psid": old_psid,
                "psidts": old_psidts,
                "parsed": {},
                "status": "正常",
                "use_count": 0,
                "note": "从旧配置迁移",
                "created_time": int(time.time())
            }

    if "cookies" not in data:
        data["cookies"] = {}
    if "settings" not in data:
        data["settings"] = default["settings"]

    for cookie_id, cookie_data in data["cookies"].items():
        normalize_cookie_record(cookie_id, cookie_data)

    data = apply_env_bootstrap(data)

    if "plugin_token" not in data["settings"] or not data["settings"]["plugin_token"]:
        data["settings"]["plugin_token"] = secrets.token_urlsafe(32)
        save_data(data)

    return data

def get_upstash_rest_url() -> str:
    return os.environ.get("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")

def get_upstash_rest_token() -> str:
    return os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()

def get_redis_url() -> str:
    for key in ("UPSTASH_REDIS_URL", "REDIS_URL"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""

def get_upstash_data_key() -> str:
    return os.environ.get("UPSTASH_REDIS_KEY", "geminiweb2api:data").strip() or "geminiweb2api:data"

def redis_storage_enabled() -> bool:
    return bool(get_redis_url())

def upstash_enabled() -> bool:
    return bool(get_upstash_rest_url())

def get_redis_client() -> Redis:
    redis_url = get_redis_url()
    parsed = urlparse(redis_url)
    if parsed.scheme == "redis" and parsed.hostname and parsed.hostname.endswith("upstash.io"):
        redis_url = redis_url.replace("redis://", "rediss://", 1)

    return Redis.from_url(
        redis_url,
        decode_responses=True,
        health_check_interval=30,
        socket_timeout=15,
        socket_connect_timeout=15
    )

def redis_get_json() -> Optional[Dict[str, Any]]:
    raw = get_redis_client().get(get_upstash_data_key())
    if not raw:
        return None
    return json.loads(raw)

def redis_set_json(data: Dict[str, Any]):
    get_redis_client().set(
        get_upstash_data_key(),
        json.dumps(data, ensure_ascii=False)
    )

def upstash_has_token_in_url(url: str) -> bool:
    return "_token=" in urlsplit(url).query

def execute_upstash_command(command: List[Any]) -> Any:
    rest_url = get_upstash_rest_url()
    if not rest_url:
        raise RuntimeError("UPSTASH_REDIS_REST_URL is not configured")

    headers = {"Content-Type": "application/json"}
    token = get_upstash_rest_token()
    if token and not upstash_has_token_in_url(rest_url):
        headers["Authorization"] = f"Bearer {token}"

    response = requests.post(
        rest_url,
        data=json.dumps(command, ensure_ascii=False),
        headers=headers,
        timeout=15
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(payload["error"])
    return payload.get("result")

def upstash_get_json() -> Optional[Dict[str, Any]]:
    raw = execute_upstash_command(["GET", get_upstash_data_key()])
    if not raw:
        return None
    return json.loads(raw)

def upstash_set_json(data: Dict[str, Any]):
    execute_upstash_command(["SET", get_upstash_data_key(), json.dumps(data, ensure_ascii=False)])

def get_active_cookie() -> Optional[Dict]:
    """Get a random active cookie for API requests"""
    cookies = get_cookies()
    active = [c for c in cookies.values() if c.get("status") == "正常"]
    if not active:
        return None
    return random.choice(active)

def increment_cookie_usage(cookie_id: str):
    """Increment usage count for a cookie"""
    with data_lock:
        data = load_data()
        if cookie_id in data["cookies"]:
            normalize_cookie_record(cookie_id, data["cookies"][cookie_id])
            data["cookies"][cookie_id]["use_count"] = data["cookies"][cookie_id].get("use_count", 0) + 1
            data["cookies"][cookie_id]["last_check_time"] = int(time.time())
            save_data(data)

def mark_cookie_failed(cookie_id: str, error: str = ""):
    """Mark a cookie as failed"""
    with data_lock:
        data = load_data()
        if cookie_id in data["cookies"]:
            normalize_cookie_record(cookie_id, data["cookies"][cookie_id])
            data["cookies"][cookie_id]["status"] = "失效"
            data["cookies"][cookie_id]["failure_count"] = data["cookies"][cookie_id].get("failure_count", 0) + 1
            data["cookies"][cookie_id]["cooldown_until"] = int(time.time()) + COOKIE_COOLDOWN_SECONDS
            data["cookies"][cookie_id]["last_error"] = error[:500]
            data["cookies"][cookie_id]["last_check_time"] = int(time.time())
            save_data(data)

def mark_cookie_recovered(cookie_id: str):
    update_cookie_record(
        cookie_id,
        status="正常",
        failure_count=0,
        cooldown_until=0,
        last_error="",
        last_check_time=int(time.time())
    )

def get_candidate_cookies(excluded_ids: Optional[set] = None) -> List[tuple[str, Dict[str, Any]]]:
    excluded_ids = excluded_ids or set()
    now = int(time.time())
    candidates: List[tuple[str, Dict[str, Any]]] = []
    for cookie_id, cookie_data in get_cookies().items():
        normalize_cookie_record(cookie_id, cookie_data)
        if cookie_id in excluded_ids:
            continue
        if cookie_data.get("cooldown_until", 0) > now:
            continue
        if cookie_data.get("status") not in {"正常", "待恢复"}:
            continue
        candidates.append((cookie_id, cookie_data))
    random.shuffle(candidates)
    return sorted(
        candidates,
        key=lambda item: (
            item[1].get("failure_count", 0),
            item[1].get("last_check_time", 0),
            item[1].get("use_count", 0)
        )
    )

def should_refresh_cookie(cookie_data: Dict[str, Any], now: Optional[int] = None) -> bool:
    now = now or int(time.time())
    last_refresh = int(cookie_data.get("last_refresh_time", 0) or 0)
    last_check = int(cookie_data.get("last_check_time", 0) or 0)
    if not cookie_data.get("psidts"):
        return True
    if now - last_refresh >= COOKIE_REFRESH_INTERVAL:
        return True
    if now - last_check >= COOKIE_HEALTHCHECK_INTERVAL:
        return True
    return False

def refresh_cookie_state(
    cookie_id: str,
    cookie_data: Dict[str, Any],
    proxy: Optional[str],
    timeout: int,
    force: bool = False
) -> tuple[bool, str]:
    now = int(time.time())
    normalize_cookie_record(cookie_id, cookie_data)
    if not force and not should_refresh_cookie(cookie_data, now):
        return True, "skip"

    full_cookies = build_full_cookie_dict(cookie_data)
    if "__Secure-1PSID" not in full_cookies:
        return False, "missing __Secure-1PSID"

    new_psidts = rotate_1psidts(full_cookies, proxy, timeout=min(timeout, 15))
    if new_psidts:
        full_cookies["__Secure-1PSIDTS"] = new_psidts

    try:
        client = GeminiClient(
            full_cookies["__Secure-1PSID"],
            full_cookies.get("__Secure-1PSIDTS"),
            proxy=proxy,
            full_cookies=full_cookies,
            timeout=timeout
        )
        client.init(timeout=min(timeout, 30))
        valid_cookies = client.cookies.copy()
    except Exception as e:
        return False, str(e)

    persist_cookie_state(cookie_id, valid_cookies)
    update_cookie_record(cookie_id, last_refresh_time=now, last_check_time=now)
    return True, "ok"

# =============================================================================
# Page Routes
# =============================================================================

@app.get("/", response_class=HTMLResponse)
async def home():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin")

@app.get("/admin", response_class=HTMLResponse)
async def admin_login(request: Request):
    if not templates:
        return PlainTextResponse("Admin template is unavailable in this deployment.", status_code=503)
    return templates.TemplateResponse(request, "login.html", {"request": request})

@app.get("/manage", response_class=HTMLResponse)
async def manage_page(request: Request):
    if not templates:
        return PlainTextResponse("Admin template is unavailable in this deployment.", status_code=503)
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
        "failure_count": 0,
        "last_error": "",
        "last_refresh_time": int(time.time()),
        "last_check_time": int(time.time()),
        "cooldown_until": 0,
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
    if os.path.exists(IMAGE_CACHE_DIR):
        for f in os.listdir(IMAGE_CACHE_DIR):
            fp = os.path.join(IMAGE_CACHE_DIR, f)
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
    if os.path.exists(IMAGE_CACHE_DIR):
        for f in os.listdir(IMAGE_CACHE_DIR):
            fp = os.path.join(IMAGE_CACHE_DIR, f)
            if os.path.isfile(fp):
                os.remove(fp)
                count += 1
    return {"success": True, "message": f"已清除 {count} 个缓存文件"}

@app.get("/images/{filename}")
def get_cached_image(filename: str):
    safe_name = os.path.basename(filename)
    path = os.path.join(IMAGE_CACHE_DIR, safe_name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(path)

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
        data["cookies"][existing_id]["failure_count"] = 0
        data["cookies"][existing_id]["cooldown_until"] = 0
        data["cookies"][existing_id]["last_error"] = ""
        data["cookies"][existing_id]["last_refresh_time"] = int(time.time())
        data["cookies"][existing_id]["last_check_time"] = int(time.time())
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
            "use_count": 0,
            "failure_count": 0,
            "last_error": "",
            "last_refresh_time": int(time.time()),
            "last_check_time": int(time.time()),
            "cooldown_until": 0
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
            path = os.path.join(IMAGE_CACHE_DIR, filename)
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
                    temp_path = os.path.join(IMAGE_CACHE_DIR, f"upload_{uuid.uuid4().hex}.{ext}")
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
                        temp_path = os.path.join(IMAGE_CACHE_DIR, f"upload_{uuid.uuid4().hex}.{ext}")
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
    image_mode = get_effective_image_mode(settings)
    timeout = get_request_timeout()

    def run_completion() -> str:
        max_retries = 3
        last_error = None
        tried_cookie_ids = set()

        for attempt in range(max_retries):
            output = None
            candidates = get_candidate_cookies(tried_cookie_ids)
            if not candidates:
                break

            cookie_id, cookie_data = candidates[0]
            tried_cookie_ids.add(cookie_id)

            try:
                refresh_ok, refresh_reason = refresh_cookie_state(
                    cookie_id,
                    cookie_data,
                    proxy=proxy,
                    timeout=timeout
                )
                if not refresh_ok:
                    raise Exception(f"Cookie keepalive failed: {refresh_reason}")

                fresh_cookie_data = get_cookies().get(cookie_id, cookie_data)
                full_cookies = build_full_cookie_dict(fresh_cookie_data)

                client = GeminiClient(
                    fresh_cookie_data["psid"],
                    fresh_cookie_data.get("psidts", ""),
                    proxy=proxy,
                    full_cookies=full_cookies,
                    timeout=timeout,
                    on_cookies_updated=lambda updated, cid=cookie_id: persist_cookie_state(cid, updated)
                )
                client.init(timeout=min(timeout, 30))

                if image_files:
                    output = client.generate_content(prompt, image_files, req.model, None, None)
                else:
                    chat = client.start_chat(model=req.model)
                    output = chat.send_message(prompt)

                persist_cookie_state(cookie_id, client.cookies)
                increment_cookie_usage(cookie_id)
                mark_cookie_recovered(cookie_id)

                content = output.text

                if output.candidates:
                    candidate = output.candidates[output.chosen]
                    for img in candidate.generated_images:
                        local_name = save_image_locally(img.image.url, client.cookies)

                        final_url = img.image.url
                        if local_name:
                            local_path = os.path.join(IMAGE_CACHE_DIR, local_name)
                            if image_mode == "base64":
                                try:
                                    b64_str = image_to_base64(local_path)
                                    final_url = f"data:image/png;base64,{b64_str}"
                                except Exception:
                                    pass
                            else:
                                final_url = f"{request.base_url}images/{local_name}"

                        content += f"\n\n![Generated Image]({final_url})"

                return content

            except Exception as e:
                last_error = e
                error_msg = str(e).lower()
                is_retryable = any(keyword in error_msg for keyword in [
                    "503", "401", "unauthorized", "auth", "cookie", "token", "psid", "expired"
                ])

                if is_retryable:
                    print(f"Attempt {attempt + 1}/{max_retries} failed with cookie {cookie_id[:8]}...: {e}")
                    recovered, recovered_reason = refresh_cookie_state(
                        cookie_id,
                        get_cookies().get(cookie_id, cookie_data),
                        proxy=proxy,
                        timeout=timeout,
                        force=True
                    )
                    if recovered:
                        update_cookie_record(
                            cookie_id,
                            status="待恢复",
                            failure_count=max(0, cookie_data.get("failure_count", 0)),
                            cooldown_until=0,
                            last_error=f"Recovered after: {str(e)[:200]}",
                            last_check_time=int(time.time())
                        )
                    else:
                        mark_cookie_failed(cookie_id, f"{e} | refresh: {recovered_reason}")
                else:
                    mark_cookie_failed(cookie_id, str(e))
                    raise HTTPException(status_code=500, detail=str(e))

            finally:
                if attempt == max_retries - 1 or output is not None:
                    for f in temp_files:
                        try:
                            if os.path.exists(f):
                                os.remove(f)
                        except Exception:
                            pass

        raise HTTPException(status_code=503, detail=f"All cookies failed. Last error: {last_error}")

    if req.stream:
        from fastapi.responses import StreamingResponse

        async def generate_stream():
            chunk_id = f"chatcmpl-{uuid.uuid4()}"
            created = int(time.time())

            # Send an early SSE event so browsers/clients don't time out during image generation.
            yield ": keep-alive\n\n"

            content = run_completion()
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

    content = run_completion()
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
    """Periodically refresh and validate cookies from the server side."""
    while True:
        settings = get_settings()
        proxy = settings.get("proxy_url", "") or None
        timeout = get_request_timeout()
        print("Starting cookie refresh cycle...")

        for cookie_id, cookie_data in get_cookies().items():
            normalize_cookie_record(cookie_id, cookie_data)
            if cookie_data.get("status") == "失效":
                continue

            ok, reason = refresh_cookie_state(
                cookie_id,
                cookie_data,
                proxy=proxy,
                timeout=timeout
            )
            if ok:
                print(f"Cookie {cookie_id[:8]}... keepalive ok ({reason})")
                mark_cookie_recovered(cookie_id)
            else:
                print(f"Cookie {cookie_id[:8]}... keepalive failed: {reason}")
                mark_cookie_failed(cookie_id, reason)

        print("Cookie refresh cycle complete")
        await asyncio.sleep(COOKIE_REFRESH_INTERVAL)

@app.on_event("startup")
async def startup():
    # Ensure data file exists and all cookie records are normalized.
    with data_lock:
        data = load_data()
        save_data(data)
    
    print("GeminiWeb2API started. Visit /admin to configure.")
    if redis_storage_enabled():
        print(f"Using Redis URL for persistent storage: {get_upstash_data_key()}")
    if upstash_enabled():
        print(f"Using Upstash Redis for persistent storage: {get_upstash_data_key()}")
    if IS_VERCEL:
        print("Vercel environment detected. Background keepalive loop is disabled.")
    else:
        asyncio.create_task(cookie_refresh_loop())
