import argparse
import uvicorn
from geminiweb2api.server import app

def main():
    parser = argparse.ArgumentParser(description="GeminiWeb2API - Gemini Web Proxy")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind")
    
    args = parser.parse_args()
    
    print(f"Starting server on {args.host}:{args.port}")
    print(f"Visit http://localhost:{args.port}/admin to configure")
    uvicorn.run(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
