from typing import List, Optional, Union, Dict, Any
from fastapi import FastAPI, HTTPException, Depends, Header, Request, status, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse, Response
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
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)
COOKIE_REFRESH_INTERVAL = 900  # 15 minutes
COOKIE_HEALTHCHECK_INTERVAL = 21600  # 6 hours
COOKIE_COOLDOWN_SECONDS = 300
DATA_CACHE_TTL_SECONDS = 5
INLINE_FILE_MAX_BYTES = 5 * 1024 * 1024
data_lock = threading.Lock()
data_cache: Dict[str, Any] = {"value": None, "expires_at": 0.0}

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

def clone_data(data: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(data, ensure_ascii=False))

def invalidate_data_cache():
    data_cache["value"] = None
    data_cache["expires_at"] = 0.0

def load_data(force_refresh: bool = False) -> Dict:
    """Load data from JSON file, migrating old format if needed"""
    default = {
        "cookies": {},
        "files": {},
        "settings": {
            "admin_username": "admin",
            "admin_password": "admin",
            "api_key": "sk-123456",
            "image_mode": "url",
            "base_url": "",
            "plugin_token": ""
        }
    }
    now = time.time()

    if not force_refresh and data_cache["value"] is not None and data_cache["expires_at"] > now:
        return clone_data(data_cache["value"])

    if redis_storage_enabled():
        try:
            raw_data = redis_get_json()
            if raw_data:
                data = raw_data
            else:
                data = apply_env_bootstrap(default)
            finalized = finalize_loaded_data(data, default)
            data_cache["value"] = clone_data(finalized)
            data_cache["expires_at"] = time.time() + DATA_CACHE_TTL_SECONDS
            return clone_data(finalized)
        except Exception as e:
            print(f"Error loading data from Redis URL: {e}")

    if upstash_enabled():
        try:
            raw_data = upstash_get_json()
            if raw_data:
                data = raw_data
            else:
                data = apply_env_bootstrap(default)
            finalized = finalize_loaded_data(data, default)
            data_cache["value"] = clone_data(finalized)
            data_cache["expires_at"] = time.time() + DATA_CACHE_TTL_SECONDS
            return clone_data(finalized)
        except Exception as e:
            print(f"Error loading data from Upstash: {e}")

    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
            finalized = finalize_loaded_data(data, default)
            data_cache["value"] = clone_data(finalized)
            data_cache["expires_at"] = time.time() + DATA_CACHE_TTL_SECONDS
            return clone_data(finalized)
        except Exception as e:
            print(f"Error loading data: {e}")
    
    # Generate token for new install
    default["settings"]["plugin_token"] = secrets.token_urlsafe(32)
    finalized = apply_env_bootstrap(default)
    data_cache["value"] = clone_data(finalized)
    data_cache["expires_at"] = time.time() + DATA_CACHE_TTL_SECONDS
    return clone_data(finalized)

def save_data(data: Dict):
    """Save data to JSON file"""
    if redis_storage_enabled():
        redis_set_json(data)
    elif upstash_enabled():
        upstash_set_json(data)
    else:
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    data_cache["value"] = clone_data(data)
    data_cache["expires_at"] = time.time() + DATA_CACHE_TTL_SECONDS

def write_request_log(
    path: str,
    method: str,
    model: str,
    duration: float,
    status_code: int,
    status: str,
    request_body: Any,
    response_body: Any,
    error_message: str = "",
    cookie_id: Optional[str] = None
):
    """记录请求日志，限制最大长度为100"""
    try:
        data = load_data(force_refresh=True)
        if "logs" not in data:
            data["logs"] = []
            
        import datetime
        now = datetime.datetime.now().astimezone()
        
        # 截断超大Body，避免数据过大影响存储
        def sanitize_body(body: Any) -> Any:
            if isinstance(body, str):
                return body[:2000] + "..." if len(body) > 2000 else body
            elif isinstance(body, dict):
                # 简单克隆并截断部分深层内容
                new_body = {}
                for k, v in body.items():
                    if isinstance(v, str) and len(v) > 2000:
                        new_body[k] = v[:2000] + "..."
                    else:
                        new_body[k] = v
                return new_body
            return body

        log_entry = {
            "id": str(uuid.uuid4()),
            "timestamp": now.isoformat(),
            "path": path,
            "method": method,
            "model": model,
            "duration": round(duration, 3),
            "status_code": status_code,
            "status": status,
            "request_body": sanitize_body(request_body),
            "response_body": sanitize_body(response_body),
            "error_message": error_message[:2000] if error_message else "",
            "cookie_id": cookie_id
        }
        
        data["logs"].insert(0, log_entry)
        data["logs"] = data["logs"][:100]
        
        save_data(data)
    except Exception as e:
        print(f"Failed to write request log: {e}")

def get_settings() -> Dict:
    return load_data().get("settings", {})

def get_cookies() -> Dict:
    return load_data().get("cookies", {})

def get_files() -> Dict:
    return load_data().get("files", {})

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
    cookie_data.setdefault("email", "未知账户")
    cookie_data.setdefault("tier", "未知")
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

def normalize_file_record(file_id: str, file_data: Dict[str, Any]) -> Dict[str, Any]:
    file_data.setdefault("id", file_id)
    file_data.setdefault("object", "file")
    file_data.setdefault("bytes", 0)
    file_data.setdefault("created_at", int(time.time()))
    file_data.setdefault("filename", "")
    file_data.setdefault("purpose", "assistants")
    file_data.setdefault("content_type", "application/octet-stream")
    file_data.setdefault("path", "")
    file_data.setdefault("content_b64", "")
    return file_data

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

def update_cookie_after_success(cookie_id: str, cookies: Dict[str, str], extra_updates: Optional[Dict[str, Any]] = None):
    with data_lock:
        data = load_data()
        cookie_data = data["cookies"].get(cookie_id)
        if not cookie_data:
            return

        normalize_cookie_record(cookie_id, cookie_data)
        cookie_data["parsed"] = cookies.copy()
        cookie_data["psid"] = cookies.get("__Secure-1PSID", cookie_data.get("psid", ""))
        cookie_data["psidts"] = cookies.get("__Secure-1PSIDTS", cookie_data.get("psidts", ""))
        cookie_data["status"] = "正常"
        cookie_data["failure_count"] = 0
        cookie_data["cooldown_until"] = 0
        cookie_data["last_error"] = ""
        cookie_data["last_check_time"] = int(time.time())
        cookie_data["last_refresh_time"] = int(time.time())
        cookie_data["use_count"] = cookie_data.get("use_count", 0) + 1
        if extra_updates:
            cookie_data.update(extra_updates)
        save_data(data)

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
import hmac
import hashlib

def generate_signed_token() -> str:
    expires_at = int(time.time()) + 3600
    secret = get_settings().get("admin_password", "admin")
    msg = f"{expires_at}:{secret}"
    signature = hashlib.md5(msg.encode()).hexdigest()
    return f"{expires_at}.{signature}"

def verify_signed_token(token: str) -> bool:
    try:
        if "." not in token:
            return False
        expires_at_str, signature = token.split(".", 1)
        expires_at = int(expires_at_str)
        if expires_at < time.time():
            return False
        secret = get_settings().get("admin_password", "admin")
        msg = f"{expires_at}:{secret}"
        expected_sig = hashlib.md5(msg.encode()).hexdigest()
        return hmac.compare_digest(expected_sig, signature)
    except Exception:
        return False

def verify_admin_token(creds: HTTPAuthorizationCredentials = Depends(security)):
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = creds.credentials
    if not verify_signed_token(token):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
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
    if "files" not in data:
        data["files"] = {}
    if "logs" not in data:
        data["logs"] = []
    if "settings" not in data:
        data["settings"] = default["settings"]

    for cookie_id, cookie_data in data["cookies"].items():
        normalize_cookie_record(cookie_id, cookie_data)
    for file_id, file_data in data["files"].items():
        normalize_file_record(file_id, file_data)

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

def sanitize_filename(name: str) -> str:
    safe = os.path.basename(name or "").strip()
    return safe or "upload.bin"

def get_file_extension(filename: str, content_type: str = "") -> str:
    _, ext = os.path.splitext(filename)
    if ext:
        return ext
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
        "text/plain": ".txt",
    }
    return mapping.get(content_type, ".bin")

def create_file_record(filename: str, content_type: str, purpose: str, file_bytes: bytes) -> Dict[str, Any]:
    safe_name = sanitize_filename(filename)
    file_id = f"file-{uuid.uuid4().hex}"
    ext = get_file_extension(safe_name, content_type)
    stored_name = f"{file_id}{ext}"
    stored_path = os.path.join(UPLOADS_DIR, stored_name)

    with open(stored_path, "wb") as f:
        f.write(file_bytes)

    record = {
        "id": file_id,
        "object": "file",
        "bytes": len(file_bytes),
        "created_at": int(time.time()),
        "filename": safe_name,
        "purpose": purpose or "assistants",
        "content_type": content_type or "application/octet-stream",
        "path": stored_path,
        "content_b64": "",
    }

    if (IS_VERCEL or redis_storage_enabled() or upstash_enabled()) and len(file_bytes) <= INLINE_FILE_MAX_BYTES:
        record["content_b64"] = base64.b64encode(file_bytes).decode("ascii")

    with data_lock:
        data = load_data()
        data.setdefault("files", {})
        data["files"][file_id] = record
        save_data(data)

    return record

def get_file_record(file_id: str) -> Optional[Dict[str, Any]]:
    files = get_files()
    record = files.get(file_id)
    if not record:
        return None
    normalize_file_record(file_id, record)
    path = record.get("path", "")
    if (not path or not os.path.exists(path)) and record.get("content_b64"):
        ext = get_file_extension(record.get("filename", ""), record.get("content_type", ""))
        restored_path = os.path.join(UPLOADS_DIR, f"{file_id}{ext}")
        with open(restored_path, "wb") as f:
            f.write(base64.b64decode(record["content_b64"]))
        record["path"] = restored_path
    return record

def serialize_file_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": record.get("id", ""),
        "object": "file",
        "bytes": record.get("bytes", 0),
        "created_at": record.get("created_at", 0),
        "filename": record.get("filename", ""),
        "purpose": record.get("purpose", "assistants"),
    }

def read_file_bytes(record: Dict[str, Any]) -> bytes:
    path = record.get("path", "")
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    if record.get("content_b64"):
        return base64.b64decode(record["content_b64"])
    raise FileNotFoundError(record.get("id", "unknown"))

def delete_file_record(file_id: str) -> bool:
    with data_lock:
        data = load_data()
        record = data.get("files", {}).get(file_id)
        if not record:
            return False
        path = record.get("path", "")
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
        del data["files"][file_id]
        save_data(data)
        return True

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
        # 自动解冻限额已过期的 Cookie
        if cookie_data.get("status") == "额度超限" and cookie_data.get("cooldown_until", 0) <= now:
            cookie_data["status"] = "正常"
            update_cookie_record(cookie_id, status="正常", cooldown_until=0)
            
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
    update_cookie_record(
        cookie_id, 
        last_refresh_time=now, 
        last_check_time=now,
        email=client.email,
        tier=client.tier
    )
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
        token = generate_signed_token()
        return {"success": True, "token": token}
    return {"success": False, "message": "用户名或密码错误"}

@app.get("/api/cookies")
def api_list_cookies(_: str = Depends(verify_admin_token)):
    cookies = get_cookies()
    data = []
    for cookie_id, cookie_data in cookies.items():
        normalize_cookie_record(cookie_id, cookie_data)
        data.append({
            "cookie_id": cookie_id,
            "status": cookie_data.get("status", "正常"),
            "use_count": cookie_data.get("use_count", 0),
            "note": cookie_data.get("note", ""),
            "created_time": cookie_data.get("created_time", 0),
            "email": cookie_data.get("email", "未知账户"),
            "tier": cookie_data.get("tier", "未知"),
            "last_check_time": cookie_data.get("last_check_time", 0)
        })
    return {"success": True, "data": data}

@app.post("/api/cookies/sync")
async def api_sync_cookies(_: str = Depends(verify_admin_token)):
    cookies = get_cookies()
    settings = get_settings()
    proxy = settings.get("proxy_url", "") or None
    timeout = get_request_timeout()

    targets = []
    for cookie_id, cookie_data in cookies.items():
        normalize_cookie_record(cookie_id, cookie_data)
        if cookie_data.get("status") != "失效" and cookie_data.get("email", "未知账户") == "未知账户":
            targets.append((cookie_id, cookie_data))

    if not targets:
        return {"success": True, "message": "所有账户信息已是最新，无需同步"}

    async def sync_one(cid, cdata):
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: refresh_cookie_state(cid, cdata, proxy, timeout, force=True)
            )
        except Exception as e:
            print(f"Failed to sync cookie {cid[:8]} inside async task: {e}")

    await asyncio.gather(*(sync_one(cid, cdata) for cid, cdata in targets))
    return {"success": True, "message": f"成功同步了 {len(targets)} 个账户的信息"}

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

@app.get("/api/logs")
def api_get_logs(_: str = Depends(verify_admin_token)):
    data = load_data()
    logs = data.get("logs", [])
    return {"success": True, "data": logs}

@app.post("/api/logs/clear")
def api_clear_logs(_: str = Depends(verify_admin_token)):
    data = load_data(force_refresh=True)
    data["logs"] = []
    save_data(data)
    return {"success": True, "message": "请求日志已成功清空"}

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

    class Config:
        extra = "ignore"

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Message]
    stream: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None

    class Config:
        extra = "ignore"

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

class ImageGenerationRequest(BaseModel):
    model: str = "gemini-3.0-flash"
    prompt: str
    n: int = 1
    response_format: str = "url"
    size: Optional[str] = None

    class Config:
        extra = "ignore"

def run_gemini_request(
    model: str,
    prompt: str,
    input_files: List[str],
    request: Request,
    image_mode: str,
    proxy: Optional[str],
    timeout: int,
    cookie_info: Optional[Dict[str, Any]] = None
) -> tuple[str, List[str]]:
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
        if cookie_info is not None:
            cookie_info["cookie_id"] = cookie_id

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

            if input_files:
                output = client.generate_content(prompt, input_files, model, None, None)
            else:
                chat = client.start_chat(model=model)
                output = chat.send_message(prompt)

            update_cookie_after_success(cookie_id, client.cookies)

            content = output.text
            image_urls: List[str] = []

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

                    image_urls.append(final_url)
                    content += f"\n\n![Generated Image]({final_url})"

            return content, image_urls

        except Exception as e:
            last_error = e
            error_msg = str(e).lower()
            
            # 检测是否为额度超限错误
            is_limit_error = any(kw in error_msg for kw in ["1037", "limit exceeded", "too many requests", "429"])
            if is_limit_error:
                print(f"Cookie {cookie_id[:8]}... usage limit exceeded. Cool down for 5 hours.")
                update_cookie_record(
                    cookie_id,
                    status="额度超限",
                    cooldown_until=int(time.time()) + 18000,
                    last_error=f"Usage limit exceeded: {str(e)[:200]}",
                    last_check_time=int(time.time())
                )
            
            is_retryable = any(keyword in error_msg for keyword in [
                "503", "401", "unauthorized", "auth", "cookie", "token", "psid", "expired"
            ])

            if is_retryable or is_limit_error:
                print(f"Attempt {attempt + 1}/{max_retries} failed with cookie {cookie_id[:8]}...: {e}")
                if is_retryable:
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
                # 继续尝试下一个 Cookie 账号
                continue
            else:
                mark_cookie_failed(cookie_id, str(e))
                raise HTTPException(status_code=500, detail=str(e))

    raise HTTPException(status_code=503, detail=f"All cookies failed. Last error: {last_error}")

@app.post("/v1/files", dependencies=[Depends(verify_api_key)])
async def create_file(
    file: UploadFile = File(...),
    purpose: str = Form("assistants")
):
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")

    record = create_file_record(
        filename=file.filename or "upload.bin",
        content_type=file.content_type or "application/octet-stream",
        purpose=purpose,
        file_bytes=content
    )
    return serialize_file_record(record)

@app.get("/v1/files/{file_id}", dependencies=[Depends(verify_api_key)])
def retrieve_file(file_id: str):
    record = get_file_record(file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    return serialize_file_record(record)

@app.get("/v1/files", dependencies=[Depends(verify_api_key)])
def list_files():
    files = [
        serialize_file_record(record)
        for _, record in sorted(get_files().items(), key=lambda item: item[1].get("created_at", 0), reverse=True)
    ]
    return {"object": "list", "data": files}

@app.get("/v1/files/{file_id}/content", dependencies=[Depends(verify_api_key)])
def retrieve_file_content(file_id: str):
    record = get_file_record(file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content = read_file_bytes(record)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File content not found")
    media_type = record.get("content_type", "application/octet-stream")
    headers = {
        "Content-Disposition": f"attachment; filename=\"{sanitize_filename(record.get('filename', file_id))}\""
    }
    return Response(content=content, media_type=media_type, headers=headers)

@app.delete("/v1/files/{file_id}", dependencies=[Depends(verify_api_key)])
def delete_file(file_id: str):
    if not delete_file_record(file_id):
        raise HTTPException(status_code=404, detail="File not found")
    return {"id": file_id, "object": "file.deleted", "deleted": True}

@app.post("/v1/images/generations", dependencies=[Depends(verify_api_key)])
async def image_generations(req: ImageGenerationRequest, request: Request):
    if req.n != 1:
        raise HTTPException(status_code=400, detail="Only n=1 is currently supported")
    if req.response_format not in {"url", "b64_json"}:
        raise HTTPException(status_code=400, detail="response_format must be 'url' or 'b64_json'")

    settings = get_settings()
    proxy = settings.get("proxy_url", "") or None
    image_mode = "base64" if req.response_format == "b64_json" else get_effective_image_mode(settings)
    timeout = get_request_timeout()

    start_time = time.time()
    cookie_info = {"cookie_id": None}
    status = "success"
    status_code = 200
    image_urls = []
    error_message = ""
    try:
        _, image_urls = run_gemini_request(
            model=req.model,
            prompt=req.prompt,
            input_files=[],
            request=request,
            image_mode=image_mode,
            proxy=proxy,
            timeout=timeout,
            cookie_info=cookie_info
        )

        if not image_urls:
            raise HTTPException(status_code=500, detail="Image generation returned no images")

        data = []
        for image_url in image_urls:
            if image_url.startswith("data:image/"):
                _, b64 = image_url.split(",", 1)
                data.append({"b64_json": b64})
            elif req.response_format == "b64_json":
                raise HTTPException(status_code=500, detail="Image could not be converted to base64")
            else:
                data.append({"url": image_url})

        return {"created": int(time.time()), "data": data}
    except Exception as e:
        status = "error"
        status_code = getattr(e, "status_code", 500)
        error_message = str(e)
        raise e
    finally:
        duration = time.time() - start_time
        write_request_log(
            path="/v1/images/generations",
            method="POST",
            model=req.model,
            duration=duration,
            status_code=status_code,
            status=status,
            request_body=req.dict(),
            response_body={"data": [{"url": url} for url in image_urls]} if status == "success" else None,
            error_message=error_message,
            cookie_id=cookie_info.get("cookie_id")
        )

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

def extract_content_and_files(content: Union[str, List[Dict[str, Any]]]) -> tuple:
    """Extract text and local file paths from OpenAI-style content."""
    if isinstance(content, str):
        return content, []
    
    text_parts = []
    file_paths = []
    
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
                    file_paths.append(temp_path)
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
                        file_paths.append(temp_path)
                except Exception as e:
                    print(f"Failed to download image from URL: {e}")
        elif part.get("type") == "input_file":
            file_id = part.get("file_id") or part.get("input_file", {}).get("file_id")
            if not file_id:
                continue
            record = get_file_record(file_id)
            if not record:
                raise HTTPException(status_code=404, detail=f"File not found: {file_id}")
            file_path = record.get("path", "")
            if not file_path or not os.path.exists(file_path):
                raise HTTPException(status_code=404, detail=f"Uploaded file content unavailable: {file_id}")
            file_paths.append(file_path)
            if record.get("filename"):
                text_parts.append(f"[Attached file: {record['filename']}]")
    
    return " ".join(text_parts).strip(), file_paths

@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def chat_completions(req: ChatCompletionRequest, request: Request):
    if not req.messages:
        raise HTTPException(status_code=400, detail="No messages provided")
    
    # 提取系统提示词和历史消息
    system_msgs = [m for m in req.messages if m.role == "system"]
    history_msgs = [m for m in req.messages[:-1] if m.role in ("user", "assistant")]
    
    last_msg = req.messages[-1]
    
    # Extract text and images from multimodal content
    prompt, input_files = extract_content_and_files(last_msg.content)
    temp_files = [p for p in input_files if os.path.dirname(p) == IMAGE_CACHE_DIR]
    
    # 组装完整的提示词以支持系统提示词和多轮对话上下文
    context_parts = []
    if system_msgs:
        system_contents = []
        for s_msg in system_msgs:
            s_text, _ = extract_content_and_files(s_msg.content)
            if s_text:
                system_contents.append(s_text)
        if system_contents:
            context_parts.append("[System Instruction]\n" + "\n".join(system_contents))
            
    if history_msgs:
        history_text_parts = []
        for msg in history_msgs:
            role_label = "User" if msg.role == "user" else "Model"
            h_text, _ = extract_content_and_files(msg.content)
            if h_text:
                history_text_parts.append(f"{role_label}: {h_text}")
        if history_text_parts:
            context_parts.append("[Conversation History]\n" + "\n".join(history_text_parts))
            
    if context_parts:
        prompt = "\n\n".join(context_parts) + f"\n\nUser: {prompt}"
    
    # Get settings for proxy
    settings = get_settings()
    proxy = settings.get("proxy_url", "") or None
    image_mode = get_effective_image_mode(settings)
    timeout = get_request_timeout()

    if req.stream:
        from fastapi.responses import StreamingResponse

        async def generate_stream():
            chunk_id = f"chatcmpl-{uuid.uuid4()}"
            created = int(time.time())

            # Send an early SSE event so browsers/clients don't time out during image generation.
            yield ": keep-alive\n\n"

            start_time = time.time()
            cookie_info = {"cookie_id": None}
            status = "success"
            status_code = 200
            content = ""
            error_message = ""

            try:
                content, _ = run_gemini_request(req.model, prompt, input_files, request, image_mode, proxy, timeout, cookie_info=cookie_info)
            except Exception as e:
                status = "error"
                status_code = getattr(e, "status_code", 500)
                error_message = str(e)
                raise e
            finally:
                for f in temp_files:
                    try:
                        if os.path.exists(f):
                            os.remove(f)
                    except Exception:
                        pass
                
                # 记录流式返回的最终日志
                duration = time.time() - start_time
                write_request_log(
                    path="/v1/chat/completions",
                    method="POST",
                    model=req.model,
                    duration=duration,
                    status_code=status_code,
                    status=status,
                    request_body=req.dict(),
                    response_body={"choices": [{"message": {"role": "assistant", "content": content}}]} if status == "success" else None,
                    error_message=error_message,
                    cookie_id=cookie_info.get("cookie_id")
                )

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

    start_time = time.time()
    cookie_info = {"cookie_id": None}
    status = "success"
    status_code = 200
    content = ""
    error_message = ""
    try:
        content, _ = run_gemini_request(req.model, prompt, input_files, request, image_mode, proxy, timeout, cookie_info=cookie_info)
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
        status = "error"
        status_code = getattr(e, "status_code", 500)
        error_message = str(e)
        raise e
    finally:
        for f in temp_files:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass
        
        # 记录非流式返回的日志
        duration = time.time() - start_time
        write_request_log(
            path="/v1/chat/completions",
            method="POST",
            model=req.model,
            duration=duration,
            status_code=status_code,
            status=status,
            request_body=req.dict(),
            response_body={"choices": [{"message": {"role": "assistant", "content": content}}]} if status == "success" else None,
            error_message=error_message,
            cookie_id=cookie_info.get("cookie_id")
        )

@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {"id": "gemini-3.1-flash-lite", "object": "model", "created": 1704067200, "owned_by": "google"},
            {"id": "gemini-3.1-flash-lite-thinking", "object": "model", "created": 1704067200, "owned_by": "google"},
            {"id": "gemini-3.5-flash", "object": "model", "created": 1704067200, "owned_by": "google"},
            {"id": "gemini-3.5-flash-thinking", "object": "model", "created": 1704067200, "owned_by": "google"},
            {"id": "gemini-3.1-pro", "object": "model", "created": 1704067200, "owned_by": "google"},
            {"id": "gemini-3.1-pro-thinking", "object": "model", "created": 1704067200, "owned_by": "google"},
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
    # Warm in-memory cache. Avoid unconditional writes on serverless cold starts.
    with data_lock:
        load_data(force_refresh=True)
    
    print("GeminiWeb2API started. Visit /admin to configure.")
    if redis_storage_enabled():
        print(f"Using Redis URL for persistent storage: {get_upstash_data_key()}")
    if upstash_enabled():
        print(f"Using Upstash Redis for persistent storage: {get_upstash_data_key()}")
    if IS_VERCEL:
        print("Vercel environment detected. Background keepalive loop is disabled.")
    else:
        asyncio.create_task(cookie_refresh_loop())
