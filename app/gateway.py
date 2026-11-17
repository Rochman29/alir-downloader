"""Stateless Vercel gateway. No downloader, SQLite, or local file writes."""
import logging
import json
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .config import validate_origin
from .security import OWNER_PATTERN, browser_session
from .web import install_frontend, install_web

logger = logging.getLogger(__name__)
COOKIE_NAME = "alir_session"


def create_gateway_app(*, backend_url: str | None = None, worker_token: str | None = None,
                       transport=None):
    backend_url = backend_url if backend_url is not None else os.getenv("ALIR_BACKEND_URL", "")
    worker_token = worker_token if worker_token is not None else os.getenv("ALIR_WORKER_TOKEN", "")
    production = os.getenv("VERCEL") == "1"
    configured = bool(backend_url and len(worker_token) >= 32)
    if backend_url:
        backend_url = validate_origin(backend_url, production=production)

    @asynccontextmanager
    async def lifespan(app):
        # Network calls are bounded; the actual download runs independently on the worker.
        async with httpx.AsyncClient(transport=transport, follow_redirects=False,
                                     timeout=httpx.Timeout(90, connect=10)) as client:
            app.state.client = client
            yield

    app = FastAPI(title="Alir — Gateway", lifespan=lifespan, docs_url=None, redoc_url=None)
    install_web(app)
    install_frontend(app)

    @app.middleware("http")
    async def session_cookie(request: Request, call_next):
        if not request.url.path.startswith("/api/") or not configured:
            return await call_next(request)
        owner, cookie = browser_session(request.cookies.get(COOKIE_NAME, ""), worker_token)
        request.state.owner = owner
        response = await call_next(request)
        if cookie != request.cookies.get(COOKIE_NAME):
            response.set_cookie(COOKIE_NAME, cookie, max_age=90 * 86400, httponly=True,
                                secure=production or request.url.scheme == "https", samesite="lax")
        response.headers["Cache-Control"] = "no-store"
        return response

    async def upstream(request: Request, path: str):
        if not configured:
            return JSONResponse({"detail": "Layanan unduh belum tersedia. Coba lagi nanti.",
                                 "ready": False, "mode": "server"}, status_code=503)
        body = None
        if request.method == "POST":
            # The API accepts a short video link or a quality selection, never file uploads.
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "Content-Length tidak valid."}, status_code=400)
            if length > 8192:
                return JSONResponse({"detail": "Request terlalu besar."}, status_code=413)
            chunks = bytearray()
            async for chunk in request.stream():
                chunks.extend(chunk)
                if len(chunks) > 8192:
                    return JSONResponse({"detail": "Request terlalu besar."}, status_code=413)
            body = bytes(chunks)
        try:
            response = await app.state.client.request(
                request.method, f"{backend_url}{path}", content=body,
                headers={"Authorization": f"Bearer {worker_token}",
                         "X-Alir-Owner": request.state.owner, "Content-Type": "application/json"})
            data = response.json()
        except (httpx.HTTPError, ValueError):
            logger.warning("Worker unreachable or returned an invalid response")
            return JSONResponse({"detail": "Layanan unduh tidak terhubung. Coba lagi sebentar."}, status_code=502)
        if response.status_code >= 500:
            return JSONResponse({"detail": "Layanan unduh sedang bermasalah. Coba lagi sebentar."}, status_code=502)
        if path == "/api/health" and response.is_success:
            if not isinstance(data, dict):
                return JSONResponse({"detail": "Layanan unduh memberikan respons tidak valid."}, status_code=502)
            data = {**data, "mode": "server"}
        headers = {"Retry-After": response.headers["Retry-After"]} if "Retry-After" in response.headers else None
        return JSONResponse(data, status_code=response.status_code, headers=headers)

    @app.get("/api/health")
    async def health(request: Request):
        return await upstream(request, "/api/health")

    @app.post("/api/inspect")
    async def inspect(request: Request):
        return await upstream(request, "/api/inspect")

    @app.api_route("/api/downloads", methods=["GET", "POST"])
    async def downloads(request: Request):
        return await upstream(request, "/api/downloads")

    @app.get("/api/downloads/{job_id}/file")
    async def file(job_id: str, request: Request):
        if not OWNER_PATTERN.fullmatch(job_id):
            return JSONResponse({"detail": "Berkas tidak ditemukan."}, status_code=404)
        result = await upstream(request, f"/api/downloads/{job_id}/link")
        if result.status_code != 200:
            return result
        # A worker may use a private internal origin for API calls and a public HTTPS origin for files.
        try:
            url = json.loads(result.body)["url"]
            parsed = urlsplit(url)
            validate_origin(f"{parsed.scheme}://{parsed.netloc}", production=production)
            if parsed.path != f"/files/{job_id}" or not parsed.query:
                raise ValueError("Invalid file path")
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"detail": "Link unduhan belum tersedia."}, status_code=502)
        # The browser transfers bytes directly from the worker, bypassing Vercel payload/time limits.
        return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})

    return app


app = create_gateway_app()
