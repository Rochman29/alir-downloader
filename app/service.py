import logging
import math
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .providers.base import ProviderError

logger = logging.getLogger(__name__)


class DownloadService:
    def __init__(self, root: Path, providers: list):
        self.root = root.resolve()
        self.directory = self.root / "downloads"
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.root / "data").mkdir(exist_ok=True)
        self.database = self.root / "data" / "downloads.sqlite3"
        self.providers = providers
        self.lock = threading.RLock()
        self.inspections = {}
        self.inspect_slots = threading.BoundedSemaphore(2)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="download")
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, title TEXT, quality TEXT, provider TEXT,
                status TEXT, progress REAL DEFAULT 0, downloaded_bytes INTEGER DEFAULT 0,
                speed REAL, eta REAL, filename TEXT, error TEXT, created_at REAL)""")
            db.execute("UPDATE jobs SET status='failed', error=? WHERE status IN ('queued', 'downloading', 'processing')",
                       ("Aplikasi berhenti sebelum unduhan selesai. Silakan unduh ulang.",))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def inspect(self, url: str) -> dict:
        provider, normalized = next(((p, value) for p in self.providers
                                     if (value := p.normalize_url(url))), (None, None))
        if not provider:
            raise ProviderError("Masukkan link video YouTube yang valid (watch, youtu.be, atau Shorts).")
        if not self.inspect_slots.acquire(blocking=False):
            raise ProviderError("Sedang memeriksa beberapa video. Coba lagi sebentar.")
        try:
            video = provider.inspect(normalized)
        finally:
            self.inspect_slots.release()
        token = uuid4().hex
        with self.lock:
            now = time.monotonic()
            self.inspections = {k: v for k, v in self.inspections.items() if now - v[0] < 1800}
            while len(self.inspections) >= 64:
                self.inspections.pop(next(iter(self.inspections)))
            self.inspections[token] = (now, provider, video)
        result = asdict(video)
        result["inspection_id"] = token
        for quality in result["qualities"]:
            quality.pop("format_selector")
        return result

    def create(self, inspection_id: str, quality_id: str) -> dict:
        with self.lock:
            saved = self.inspections.get(inspection_id)
            if not saved or time.monotonic() - saved[0] >= 1800:
                raise ProviderError("Pilihan video kedaluwarsa. Periksa ulang link terlebih dahulu.")
            _, provider, video = saved
            quality = next((q for q in video.qualities if q.id == quality_id), None)
            if not quality:
                raise ProviderError("Kualitas ini tidak tersedia. Pilih kualitas dari daftar video.")
            with self.connect() as db:
                if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','downloading','processing')").fetchone()[0] >= 20:
                    raise ProviderError("Antrean penuh. Tunggu unduhan sebelumnya selesai.")
                job_id = uuid4().hex
                db.execute("INSERT INTO jobs (id,title,quality,provider,status,created_at) VALUES (?,?,?,?,?,?)",
                           (job_id, video.title, quality.label, provider.name, "queued", time.time()))
            self.executor.submit(self._run, job_id, provider, video, quality)
        return self.get(job_id)

    def update(self, job_id: str, **changes):
        # All keys are internal, never client supplied. SQLite transactions serialize writers.
        if "progress" in changes:
            value = changes["progress"]
            changes["progress"] = min(100, max(0, value)) if math.isfinite(value) else 0
        with self.connect() as db:
            parts = [f"{key}=MAX(progress, ?)" if key == "progress" else f"{key}=?" for key in changes]
            db.execute(f"UPDATE jobs SET {', '.join(parts)} WHERE id=?", (*changes.values(), job_id))

    def _run(self, job_id, provider, video, quality):
        directory = self.directory / job_id
        last_write = 0

        def progress(**changes):
            nonlocal last_write
            now = time.monotonic()
            if changes.get("status") == "processing" or now - last_write > 0.4:
                self.update(job_id, **changes)
                last_write = now
        try:
            directory.mkdir()
            self.update(job_id, status="downloading")
            output = provider.download(video, quality, directory, progress).resolve()
            if not output.is_relative_to(directory.resolve()) or not output.is_file():
                raise ProviderError("Lokasi berkas hasil unduhan tidak valid.")
            self.update(job_id, status="completed", progress=100, speed=None, eta=None,
                        downloaded_bytes=output.stat().st_size,
                        filename=str(output.relative_to(self.directory)))
        except Exception as error:
            logger.exception("Download %s failed", job_id)
            self.update(job_id, status="failed", speed=None, eta=None,
                        error=str(error) if isinstance(error, ProviderError) else "Unduhan gagal. Periksa koneksi dan ruang penyimpanan, lalu coba lagi.")

    def jobs(self) -> list[dict]:
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100").fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["file_url"] = f"/api/downloads/{item['id']}/file" if item["status"] == "completed" else None
            item["filename"] = Path(item["filename"]).name if item["filename"] else None
            results.append(item)
        return results

    def get(self, job_id: str) -> dict | None:
        return next((item for item in self.jobs() if item["id"] == job_id), None)

    def file(self, job_id: str) -> Path | None:
        with self.connect() as db:
            row = db.execute("SELECT filename FROM jobs WHERE id=? AND status='completed'", (job_id,)).fetchone()
        if not row or not row[0]:
            return None
        path = (self.directory / row[0]).resolve()
        return path if path.is_relative_to(self.directory) and path.is_file() else None

    def close(self):
        self.executor.shutdown(wait=True)
