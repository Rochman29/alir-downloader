import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from .providers import default_providers
from .providers.base import ProviderError
from .runtime import ROOT, ffmpeg_path, js_runtimes
from .service import DownloadService

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class InspectRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class DownloadRequest(BaseModel):
    inspection_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    quality_id: str = Field(min_length=1, max_length=100)


def create_app(root: Path = ROOT, providers: list | None = None):
    service = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal service
        service = DownloadService(root, providers if providers is not None else default_providers())
        app.state.service = service
        yield
        await run_in_threadpool(service.close)

    app = FastAPI(title="Alir — Video Downloader", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
                return JSONResponse({"detail": "Origin tidak diizinkan."}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "Gunakan application/json."}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' https://i.ytimg.com; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(ProviderError)
    async def provider_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.get("/api/health")
    def health():
        runtimes = js_runtimes()
        return {"status": "ok", "providers": [p.name for p in service.providers],
                "ffmpeg": bool(ffmpeg_path()), "javascript_runtimes": list(runtimes),
                "ready": bool(runtimes)}

    @app.post("/api/inspect")
    def inspect(body: InspectRequest):
        try:
            return service.inspect(body.url)
        except ProviderError:
            raise
        except Exception:
            logger.exception("Inspection failed")
            raise HTTPException(502, "Tidak bisa memeriksa video. Coba lagi sebentar.")

    @app.post("/api/downloads", status_code=202)
    def download(body: DownloadRequest):
        return service.create(body.inspection_id, body.quality_id)

    @app.get("/api/downloads")
    def downloads():
        return service.jobs()

    @app.get("/api/downloads/{job_id}/file")
    def file(job_id: str):
        path = service.file(job_id)
        if not path:
            raise HTTPException(404, "Berkas tidak ditemukan atau unduhan belum selesai.")
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
