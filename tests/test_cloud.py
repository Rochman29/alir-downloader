import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, validate_origin
from app.gateway import create_gateway_app
from app.main import create_app
from app.providers.base import ProviderError
from app.security import signature
from app.service import DownloadService
from test_downloader import FakeYouTube

# Test credential is intentionally public fixture data, never an actual deployment secret.
TOKEN = "unit-test-worker-credential-000000000000000000"


def worker_settings(tmp_path):
    return Settings(mode="worker", storage=tmp_path, worker_token=TOKEN,
                    public_worker_url="https://worker.example", allowed_hosts=("worker.example", "testserver"))


def wait_complete(session):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        jobs = session.get("/api/downloads").json()
        if jobs and jobs[0]["status"] in ("completed", "failed"):
            return jobs[0]
        time.sleep(0.02)
    pytest.fail("Worker did not finish")


class ObservedTransport(httpx.ASGITransport):
    def __init__(self, app):
        super().__init__(app=app)
        self.seen = []

    async def handle_async_request(self, request):
        self.seen.append(request.url.path)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return await super().handle_async_request(request)


class LargeVideo(FakeYouTube):
    def download(self, video, quality, directory, progress):
        output = directory / "large.mp4"
        # More than Vercel's buffered-response limit; transfer must bypass the gateway.
        output.write_bytes(b"large-file-test-" * (400 * 1024))
        return output


def test_gateway_to_worker_then_direct_large_download_and_range(tmp_path):
    worker = create_app(providers=[LargeVideo()], settings=worker_settings(tmp_path))
    transport = ObservedTransport(worker)
    gateway = create_gateway_app(backend_url="https://worker.example", worker_token=TOKEN, transport=transport)
    other_gateway = create_gateway_app(backend_url="https://worker.example", worker_token=TOKEN,
                                       transport=ObservedTransport(worker))
    with TestClient(worker, base_url="https://worker.example") as origin:
        with TestClient(gateway, base_url="https://alir.example") as client, \
             TestClient(other_gateway, base_url="https://alir.example") as other:
            health = client.get("/api/health")
            assert health.json()["mode"] == "server"
            assert "HttpOnly" in health.headers["set-cookie"]
            assert "Secure" in health.headers["set-cookie"]
            inspected = client.post("/api/inspect", json={"url": "https://youtu.be/jNQXAC9IVRw"}).json()
            payload = {"inspection_id": inspected["inspection_id"], "quality_id": "137"}
            assert other.post("/api/downloads", json=payload).status_code == 400
            assert client.post("/api/downloads", json=payload).status_code == 202
            job = wait_complete(client)
            assert job["status"] == "completed"
            assert "owner" not in job and "video_json" not in job
            assert other.get("/api/downloads").json() == []
            assert other.get(job["file_url"], follow_redirects=False).status_code == 404
            redirect = client.get(job["file_url"], follow_redirects=False)
            assert redirect.status_code == 303
            link = redirect.headers["location"]
            assert link.startswith(f"https://worker.example/files/{job['id']}?")
            assert "no-store" in redirect.headers["cache-control"]
            assert not any(path.startswith("/files/") for path in transport.seen)
            file = origin.get(link)
            assert file.status_code == 200
            assert len(file.content) > 4.5 * 1024 ** 2
            assert "attachment" in file.headers["content-disposition"]
            partial = origin.get(link, headers={"Range": "bytes=1048576-2097151"})
            assert partial.status_code == 206
            assert partial.content == file.content[1048576:2097152]
            assert origin.head(link).status_code == 200
            assert origin.get(link + "tampered").status_code == 403
            expired = signature(TOKEN, "download", f"{job['id']}:1")
            assert origin.get(f"/files/{job['id']}?expires=1&signature={expired}").status_code == 403
            assert origin.get("/api/downloads").status_code == 401
            assert origin.get("/healthz").status_code == 200
            assert TOKEN not in json.dumps(job) and TOKEN not in link


def test_gateway_without_worker_still_serves_frontend(tmp_path):
    gateway = create_gateway_app(backend_url="", worker_token="")
    with TestClient(gateway) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/health").status_code == 503
    assert list(tmp_path.iterdir()) == []


def test_gateway_handles_outage_and_rejects_large_requests():
    def outage(request):
        raise httpx.ConnectError("offline")
    gateway = create_gateway_app(backend_url="https://worker.example", worker_token=TOKEN,
                                 transport=httpx.MockTransport(outage))
    with TestClient(gateway) as client:
        assert client.get("/api/downloads").status_code == 502
        assert client.post("/api/inspect", content="x" * 9000,
                           headers={"Content-Type": "application/json"}).status_code == 413
        assert client.post("/api/inspect", json={"url": "test"},
                           headers={"Origin": "https://attacker.example"}).status_code == 403


def test_worker_rate_limits_inspection_and_owner_queue(tmp_path):
    provider = FakeYouTube()
    provider.gate.clear()
    settings = worker_settings(tmp_path)
    worker = create_app(providers=[provider], settings=settings)
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Alir-Owner": "a" * 32}
    with TestClient(worker, base_url="https://worker.example", headers=headers) as client:
        try:
            for index in range(6):
                response = client.post("/api/inspect", json={"url": "https://youtu.be/jNQXAC9IVRw"})
                assert response.status_code == 200
                payload = {"inspection_id": response.json()["inspection_id"], "quality_id": "137"}
                if index < 3:
                    assert client.post("/api/downloads", json=payload).status_code == 202
            assert client.post("/api/downloads", json=payload).status_code == 400
            limited = client.post("/api/inspect", json={"url": "https://youtu.be/jNQXAC9IVRw"})
            assert limited.status_code == 429 and limited.headers["retry-after"] == "60"
        finally:
            provider.gate.set()


def test_pending_plan_recovers_existing_directory_after_restart(tmp_path):
    provider = FakeYouTube()
    video = provider.inspect("https://youtu.be/jNQXAC9IVRw")
    service = DownloadService(tmp_path, [provider])
    job_id = "b" * 32
    directory = service.directory / job_id
    directory.mkdir()
    (directory / "video.mp4.part").write_bytes(b"partial content retained")
    with service.connect() as db:
        db.execute("""INSERT INTO jobs (id,title,quality,provider,status,created_at,video_json,quality_json)
            VALUES (?,?,?,?,?,?,?,?)""", (job_id, video.title, "1080p", provider.name, "downloading",
            time.time(), json.dumps(asdict(video)), json.dumps(asdict(video.qualities[0]))))
    service.close()
    recovered = DownloadService(tmp_path, [provider])
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and recovered.get(job_id)["status"] != "completed":
            time.sleep(0.02)
        assert recovered.get(job_id)["status"] == "completed"
        assert recovered.file(job_id).is_file()
        assert (directory / "video.mp4.part").is_file()
    finally:
        recovered.close()


def test_graceful_stop_preserves_active_job_as_queued(tmp_path):
    class Interruptible(FakeYouTube):
        def __init__(self):
            super().__init__()
            self.started = threading.Event()

        def download(self, video, quality, directory, progress):
            self.started.set()
            while True:
                progress(status="downloading", progress=1)
                time.sleep(0.02)

    provider = Interruptible()
    service = DownloadService(tmp_path, [provider])
    inspected = service.inspect("https://youtu.be/jNQXAC9IVRw")
    job = service.create(inspected["inspection_id"], "137")
    assert provider.started.wait(3)
    service.close()
    assert service.get(job["id"])["status"] == "queued"


def test_reject_job_when_disk_has_no_room(tmp_path, monkeypatch):
    service = DownloadService(tmp_path, [FakeYouTube()], min_free_bytes=1024)
    try:
        video = service.inspect("https://youtu.be/jNQXAC9IVRw")
        monkeypatch.setattr(shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(10000, 9999, 1))
        with pytest.raises(ProviderError, match="penyimpanan"):
            service.create(video["inspection_id"], "137")
    finally:
        service.close()


@pytest.mark.parametrize("url", ["https://worker.example/path", "https://user:pass@worker.example", "http://worker.example", "file:///tmp", "https://worker.example?q=1"])
def test_backend_origin_validation(url):
    with pytest.raises(ValueError):
        validate_origin(url)


def test_gateway_entrypoint_imports_without_downloader_or_storage(tmp_path):
    root = Path(__file__).resolve().parent.parent
    code = """import sys
import index
assert 'yt_dlp' not in sys.modules
assert 'imageio_ffmpeg' not in sys.modules
assert 'app.service' not in sys.modules
print('gateway imports without worker dependencies')
"""
    environment = {**os.environ, "PYTHONPATH": str(root), "ALIR_BACKEND_URL": "", "ALIR_WORKER_TOKEN": ""}
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=environment,
                   check=True, capture_output=True, timeout=20)
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "downloads").exists()
