import requests
import re
import json
from urllib.parse import urlparse
from typing import Dict, Tuple, Optional
from .constants import Endpoints, Headers

class AuthError(Exception):
    pass

def get_access_token(cookies: Dict[str, str], proxy: Optional[str] = None) -> Tuple[str, Dict[str, str]]:
    """
    Retrieves the SNlM0e nonce (access token) and verifies cookies.
    Returns (token, valid_cookies).
    """
    proxies = {"http": proxy, "https": proxy} if proxy else None
    
    # 1. Prime Google cookies (optional but good practice)
    try:
        requests.get(Endpoints.Google, proxies=proxies, timeout=10)
    except Exception:
        pass

    # 2. Try with provided cookies
    required = ["__Secure-1PSID", "__Secure-1PSIDTS"]
    if not all(k in cookies for k in required):
         # If TS missing, maybe we can fetch it via Rotate? 
         # For now, just warn or proceed if user only gave PSID (might fail)
         pass

    # 3. Send Init Request to get SNlM0e
    session = requests.Session()
    session.headers.update(Headers.Gemini)
    session.proxies.update(proxies or {})
    
    # Apply cookies
    for k, v in cookies.items():
        session.cookies.set(k, v)

    try:
        resp = session.get(Endpoints.Init, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        raise AuthError(f"Init request failed: {e}")

    # Extract Token
    match = re.search(r'"SNlM0e":"([^"]+)"', resp.text)
    if not match:
        raise AuthError("Failed to retrieve SNlM0e token from response.")
    
    token = match.group(1)
    
    # Update cookies with any new ones from response
    valid_cookies = cookies.copy()
    valid_cookies.update(session.cookies.get_dict())
    
    return token, valid_cookies

def rotate_1psidts(cookies: Dict[str, str], proxy: Optional[str] = None) -> Optional[str]:
    """
    Rotates the __Secure-1PSIDTS cookie.
    Uses the same format as CLIProxyAPI.
    """
    if "__Secure-1PSID" not in cookies:
        return None

    proxies = {"http": proxy, "https": proxy} if proxy else None
    
    # Use exact request body format from CLIProxyAPI: [000,"-0000000000000000000"]
    # Note: 000 not 0 - this matches the Go implementation
    body = '[000,"-0000000000000000000"]'
    
    try:
        resp = requests.post(
            Endpoints.RotateCookies,
            data=body,  # Use data= not json= for raw body
            headers=Headers.RotateCookies,
            cookies=cookies,
            proxies=proxies,
            timeout=10
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"RotateCookies request failed: {e}")
        return None

    # Check response cookies for new PSIDTS
    new_ts = resp.cookies.get("__Secure-1PSIDTS")
    if new_ts:
        return new_ts
    
    return None
