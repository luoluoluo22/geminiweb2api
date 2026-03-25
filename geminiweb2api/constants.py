from enum import IntEnum

class ErrorCode(IntEnum):
    UsageLimitExceeded = 1037
    ModelInconsistent = 1050
    ModelHeaderInvalid = 1052
    IPTemporarilyBlocked = 1060

class Endpoints:
    Google = "https://www.google.com"
    Init = "https://gemini.google.com/app"
    Generate = "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
    RotateCookies = "https://accounts.google.com/RotateCookies"
    Upload = "https://content-push.googleapis.com/upload"

class Headers:
    Gemini = {
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "Host": "gemini.google.com",
        "Origin": "https://gemini.google.com",
        "Referer": "https://gemini.google.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "X-Same-Domain": "1",
    }
    
    RotateCookies = {
        "Content-Type": "application/json",
    }
    
    Upload = {
        "Push-ID": "feeds/mcudyrk2a4khkz",
    }
