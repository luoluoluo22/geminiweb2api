import requests
import json
import time
import random
import string
import re
import os
from typing import List, Optional, Dict, Any, Union, Callable
from .constants import Endpoints, Headers, ErrorCode
from .models import ModelOutput, Candidate, WebImage, GeneratedImage, Image
from .auth import get_access_token, rotate_1psidts, AuthError
from .conversation import ChatSession

class GeminiClient:
    def __init__(self, secure_1psid: str, secure_1psidts: Optional[str] = None, proxy: Optional[str] = None, on_cookies_updated: Optional[Callable[[Dict[str, str]], None]] = None, full_cookies: Optional[Dict[str, str]] = None):
        # If full_cookies provided, use it as base (for image operations that need more cookies)
        if full_cookies:
            self.cookies = full_cookies.copy()
            # Ensure required cookies are present
            if secure_1psid:
                self.cookies["__Secure-1PSID"] = secure_1psid
            if secure_1psidts:
                self.cookies["__Secure-1PSIDTS"] = secure_1psidts
        else:
            self.cookies = {"__Secure-1PSID": secure_1psid}
            if secure_1psidts:
                self.cookies["__Secure-1PSIDTS"] = secure_1psidts
        
        self.proxy = proxy
        self.session = requests.Session()
        self.access_token = None
        self.running = False
        self.on_cookies_updated = on_cookies_updated
        
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def refresh_cookies(self) -> bool:
        """
        Attempts to rotate credentials and updates the session.
        Returns True if successful, False otherwise.
        """
        try:
            print("Refreshing cookies...")
            new_ts = rotate_1psidts(self.cookies, self.proxy)
            if new_ts:
                print(f"Cookie rotation successful. New 1PSIDTS found.")
                # Update local state
                self.cookies["__Secure-1PSIDTS"] = new_ts
                self.session.cookies.update(self.cookies)
                
                # Notify callback
                if self.on_cookies_updated:
                    self.on_cookies_updated(self.cookies)
                return True
            else:
                print("Cookie rotation returned no new TS.")
                return False
        except Exception as e:
            print(f"Cookie refresh failed: {e}")
            return False

    def init(self, timeout: int = 30):
        try:
            token, valid_cookies = get_access_token(self.cookies, self.proxy)
            self.access_token = token
            self.cookies = valid_cookies
            self.session.cookies.update(valid_cookies)
            self.running = True
        except AuthError as e:
            raise e 
        except Exception as e:
            raise AuthError(f"Initialization failed: {e}")

    def _ensure_running(self):
        if not self.running:
            self.init()

    def _upload_file(self, path: str) -> str:
        """Uploads a file to Gemini's content-push service and returns the ID."""
        if not os.path.exists(path):
            raise Exception(f"File not found: {path}")

        filename = os.path.basename(path)
        # Headers specifically for upload
        # Do NOT use Headers.Gemini as base because it has Host: gemini.google.com
        headers = Headers.Upload.copy()
        headers["User-Agent"] = Headers.Gemini["User-Agent"]
        
        with open(path, 'rb') as f:
            files = {
                'file': (filename, f, 'application/octet-stream')
            }
            try:
                # Use bare requests to avoid sending session cookies (matches Go implementation)
                proxies = self.session.proxies if self.proxy else None
                resp = requests.post(
                    Endpoints.Upload,
                    files=files,
                    headers=headers,
                    timeout=300,
                    proxies=proxies
                )
                if resp.status_code != 200:
                     raise Exception(f"Status {resp.status_code}: {resp.text}")
                return resp.text.strip()
            except Exception as e:
                raise Exception(f"File upload failed: {e}")

    def generate_content(self, prompt: str, files: List[str], model: str, gem_id: Optional[str], chat: Optional[ChatSession] = None) -> ModelOutput:
        self._ensure_running()
        
        # Upload files
        uploaded_files = [] 
        for f in files:
            fid = self._upload_file(f)
            fname = os.path.basename(f)
            # Structure matches Go: [[id], filename]
            uploaded_files.append([[fid], fname])

        # Construct Request JSON
        # Inner-most structure: [prompt, 0, nil, uploaded_files]
        if uploaded_files:
             item0 = [prompt, 0, None, uploaded_files]
        else:
             item0 = [prompt]
        
        item2 = chat.metadata if chat else None
        
        # Structure: [item0, nil, item2, ..., gem_id, ..., 14 (if nano)]
        inner = [item0, None, item2]
        
        if gem_id:
            # Pad to index 19 (16 nils + current length 3?) -> Go code pads 16 nils AFTER inner items?
            # Go: inner = []any{item0, nil, item2} (len 3)
            # append 16 nils -> len 19
            # append gemID -> len 20 (index 19)
            inner.extend([None] * 16)
            inner.append(gem_id)
            
        inner_json = json.dumps(inner)
        outer = [None, inner_json]
        outer_json = json.dumps(outer)
        
        params = {
            "at": self.access_token,
            "f.req": outer_json
        }
        
        headers = Headers.Gemini.copy()
        # Add model specific headers if needed (simplified here)
        
        try:
            resp = self.session.post(
                Endpoints.Generate, 
                data=params, 
                headers=headers, 
                timeout=120
            )
            resp.raise_for_status()
        except requests.HTTPError as e:
            if e.response.status_code == 429:
                raise Exception("Too many requests (429)")
            raise e

        return self._parse_response(resp.text)

    def _parse_response(self, text: str) -> ModelOutput:
        lines = text.splitlines()
        if len(lines) < 3:
             raise Exception("Invalid response format (too short)")
        
        # The data is usually on line 3 (index 2)
        try:
            data = json.loads(lines[2])
        except json.JSONDecodeError:
             # Fallback scan
             data = None
             for line in lines[3:]:
                 try:
                     t = json.loads(line)
                     if isinstance(t, list) and len(t) > 0:
                         data = t
                         break
                 except: continue
        
        if not data:
            raise Exception("No valid JSON data found in response")

        # Parse body from data
        # data[0][2] is stringified json
        # We need to find the part with the candidates
        
        # Simplified parser logic following the Go implementation's structure search
        response_json = data
        main_part = None

        for item in response_json:
            if isinstance(item, list) and len(item) > 2 and isinstance(item[2], str):
                try:
                    inner = json.loads(item[2])
                    if isinstance(inner, list) and len(inner) > 4 and inner[4] is not None:
                         main_part = inner
                         break
                except:
                    continue
        
        if not main_part:
            raise Exception("Failed to locate main response body")

        # Metadata
        metadata = []
        if len(main_part) > 1 and isinstance(main_part[1], list):
             for m in main_part[1]:
                 if isinstance(m, str):
                     metadata.append(m)

        # Candidates are at main_part[4]
        candidates_data = main_part[4]
        candidates = []
        
        if isinstance(candidates_data, list):
            for i, cand_raw in enumerate(candidates_data):
                if not isinstance(cand_raw, list): continue
                
                # Text: cand_raw[1][0]
                text = ""
                if len(cand_raw) > 1 and isinstance(cand_raw[1], list) and len(cand_raw[1]) > 0:
                    text = cand_raw[1][0]
                
                generated_images = []
                # Check for generated image placeholder
                if "googleusercontent.com/image_generation_content" in text:
                    # Find the separate image body in the response parts
                    img_body = None
                    for idx, item in enumerate(response_json):
                        # item must be list, len>2, item[2] is str
                        if isinstance(item, list) and len(item) > 2 and isinstance(item[2], str):
                             try:
                                 mp = json.loads(item[2])
                                 # Looking for structure [4][i][12][7][0]
                                 if isinstance(mp, list) and len(mp) > 4:
                                     tt = mp[4]
                                     if isinstance(tt, list) and len(tt) > i:
                                         sec = tt[i]
                                         if isinstance(sec, list) and len(sec) > 12:
                                             ss = sec[12]
                                             if isinstance(ss, list) and len(ss) > 7:
                                                 first = ss[7]
                                                 if isinstance(first, list) and len(first) > 0 and first[0] is not None:
                                                     img_body = mp
                                                     break
                             except: continue
                    
                    if img_body:
                         # Parse images from img_body
                         img_cand = img_body[4][i] # list
                         # Text update (remove placeholder)
                         if len(img_cand) > 1 and isinstance(img_cand[1], list) and len(img_cand[1]) > 0:
                             raw_text = img_cand[1][0]
                             text = re.sub(r'http://googleusercontent\.com/image_generation_content/\d+', '', raw_text).strip()
                         
                         # Images at [12][7][0]
                         if len(img_cand) > 12 and isinstance(img_cand[12], list) and len(img_cand[12]) > 7:
                             s2 = img_cand[12][7]
                             if isinstance(s2, list) and len(s2) > 0:
                                 s3 = s2[0]
                                 if isinstance(s3, list):
                                     for gi in s3:
                                         if not isinstance(gi, list) or len(gi) < 4: continue
                                         # URL: gi[0][3][3]
                                         url = ""
                                         if isinstance(gi[0], list) and len(gi[0]) > 3:
                                             b1 = gi[0][3]
                                             if isinstance(b1, list) and len(b1) > 3:
                                                 url = b1[3]
                                         
                                         title = "[Generated Image]"
                                         alt = ""
                                         
                                         generated_images.append(GeneratedImage(
                                             image=Image(url=url, title=title, alt=alt),
                                             cookies=self.cookies
                                         ))
                
                # Check for card content replacement (rcid logic from Go)
                # ... simplified for now
                
                # Web Images: cand_raw[12][1]
                web_images = []
                if len(cand_raw) > 12 and isinstance(cand_raw[12], list):
                     # ... parsing logic
                     pass

                candidates.append(Candidate(
                    rcid=str(cand_raw[0]) if len(cand_raw) > 0 else "",
                    text=text,
                    web_images=web_images,
                    generated_images=generated_images
                ))

        return ModelOutput(
            metadata=metadata,
            candidates=candidates,
            chosen=0
        )

    def start_chat(self, model: str = "gemini-3.0-pro", gem_id: Optional[str] = None) -> ChatSession:
        return ChatSession(self, model, gem_id)
