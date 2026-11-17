import logging
import hmac
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from .providers.base import ProviderError
from .config import Settings
from .security import OWNER_PATTERN, sign_file, valid_file_signature
from .service import DownloadService
from .web import install_frontend, install_web

logger = logging.getLogger(__name__)


class InspectRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class DownloadRequest(BaseModel):
    inspection_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    quality_id: str = Field(min_length=1, max_length=100)


def create_app(root: Path | None = None, providers: list | None = None, *, settings: Settings | None = None):
    settings = settings or Settings.from_env()
    root = root if root is not None else settings.storage
    service = None
    limits = {}
    limit_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        nonlocal service
        from .providers import default_providers
        service = DownloadService(root, providers if providers is not None else default_providers(),
                                  min_free_bytes=settings.min_free_bytes)
        app.state.service = service
        try:
            yield
        finally:
            await run_in_threadpool(service.close)

    app = FastAPI(title="Alir — Video Downloader", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    install_web(app)

    @app.middleware("http")
    async def worker_access(request: Request, call_next):
        request.state.owner = ""
        if settings.mode == "worker" and request.url.path.startswith("/api/"):
            expected = f"Bearer {settings.worker_token}"
            if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected.encode()):
                return JSONResponse({"detail": "Akses tidak diizinkan."}, status_code=401)
            owner = request.headers.get("x-alir-owner", "")
            if not OWNER_PATTERN.fullmatch(owner):
                return JSONResponse({"detail": "Sesi tidak valid."}, status_code=400)
            request.state.owner = owner
            # Bound inspection load independently from the durable download queue.
            if request.method == "POST" and request.url.path == "/api/inspect":
                with limit_lock:
                    now = time.monotonic()
                    for key in list(limits):
                        limits[key] = [stamp for stamp in limits[key] if now - stamp < 60]
                        if not limits[key]:
                            del limits[key]
                    stamps = limits.get(owner, [])
                    if len(stamps) >= 6 or sum(map(len, limits.values())) >= 120:
                        return JSONResponse({"detail": "Terlalu banyak pemeriksaan video. Coba lagi sebentar."},
                                            status_code=429, headers={"Retry-After": "60"})
                    limits[owner] = [*stamps, now]
        return await call_next(request)

    @app.exception_handler(ProviderError)
    async def provider_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.get("/api/health")
    def health():
        from .runtime import ffmpeg_path, js_runtimes
        runtimes = js_runtimes()
        return {"status": "ok", "providers": [p.name for p in service.providers],
                "ffmpeg": bool(ffmpeg_path()), "javascript_runtimes": list(runtimes),
                "ready": bool(runtimes), "mode": "server" if settings.mode == "worker" else "local"}

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/api/inspect")
    def inspect(body: InspectRequest, request: Request):
        try:
            return service.inspect(body.url, request.state.owner)
        except ProviderError:
            raise
        except Exception:
            logger.exception("Inspection failed")
            raise HTTPException(502, "Tidak bisa memeriksa video. Coba lagi sebentar.")

    @app.post("/api/downloads", status_code=202)
    def download(body: DownloadRequest, request: Request):
        return service.create(body.inspection_id, body.quality_id, request.state.owner)

    @app.get("/api/downloads")
    def downloads(request: Request):
        return service.jobs(request.state.owner)

    @app.get("/api/downloads/{job_id}/file")
    def file(job_id: str, request: Request):
        path = service.file(job_id, request.state.owner)
        if not path:
            raise HTTPException(404, "Berkas tidak ditemukan atau unduhan belum selesai.")
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    if settings.mode == "worker":
        @app.get("/api/downloads/{job_id}/link")
        def download_link(job_id: str, request: Request):
            if not OWNER_PATTERN.fullmatch(job_id) or not service.file(job_id, request.state.owner):
                raise HTTPException(404, "Berkas tidak ditemukan atau unduhan belum selesai.")
            expires, signature = sign_file(job_id, settings.worker_token, settings.link_ttl)
            return {"url": f"{settings.public_worker_url}/files/{job_id}?expires={expires}&signature={signature}"}

        @app.api_route("/files/{job_id}", methods=["GET", "HEAD"])
        def signed_file(job_id: str, expires: int, signature: str):
            if not OWNER_PATTERN.fullmatch(job_id) or not valid_file_signature(
                    job_id, expires, signature, settings.worker_token):
                raise HTTPException(403, "Link unduhan kedaluwarsa. Klik Simpan berkas lagi dari aplikasi.")
            path = service.file(job_id, owner=None)
            if not path:
                raise HTTPException(404, "Berkas tidak ditemukan.")
            # FileResponse streams from disk and supports HTTP Range for large-file downloads.
            return FileResponse(path, filename=path.name, media_type="application/octet-stream")

        @app.get("/")
        def worker_index():
            return {"service": "Alir worker", "status": "ok"}
    else:
        install_frontend(app)

    return app


app = create_app()
