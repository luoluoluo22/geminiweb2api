try:
    from geminiweb2api.server import app
except Exception:  # pragma: no cover - Vercel runtime diagnostic fallback
    import traceback
    from fastapi import FastAPI, Request
    from fastapi.responses import PlainTextResponse

    app = FastAPI()
    startup_error = traceback.format_exc()

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def startup_failure(_: Request, path: str):
        return PlainTextResponse(
            f"Startup failed while importing geminiweb2api.server\n\nPath: /{path}\n\n{startup_error}",
            status_code=500
        )
