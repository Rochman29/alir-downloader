import logging
import math
import json
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .providers.base import ProviderError, Quality, Video

logger = logging.getLogger(__name__)


class DownloadService:
    def __init__(self, root: Path, providers: list, *, min_free_bytes: int = 0):
        self.root = root.resolve()
        self.directory = self.root / "downloads"
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.root / "data").mkdir(exist_ok=True)
        self.database = self.root / "data" / "downloads.sqlite3"
        self.providers = providers
        self.min_free_bytes = min_free_bytes
        self.stopping = threading.Event()
        self.lock = threading.RLock()
        self.inspections = {}
        self.inspect_slots = threading.BoundedSemaphore(2)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="download")
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, title TEXT, quality TEXT, provider TEXT,
                status TEXT, progress REAL DEFAULT 0, downloaded_bytes INTEGER DEFAULT 0,
                speed REAL, eta REAL, filename TEXT, error TEXT, created_at REAL)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            for name, definition in (("owner", "TEXT NOT NULL DEFAULT ''"),
                                     ("video_json", "TEXT"), ("quality_json", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_owner_created ON jobs(owner, created_at)")
            # Old local jobs have no persisted plan and cannot be reconstructed safely.
            db.execute("""UPDATE jobs SET status='failed', error=?
                WHERE status IN ('queued','downloading','processing') AND video_json IS NULL""",
                       ("Aplikasi berhenti sebelum unduhan selesai. Silakan unduh ulang.",))
            pending = db.execute("""SELECT id, provider, video_json, quality_json FROM jobs
                WHERE status IN ('queued','downloading','processing') ORDER BY created_at""").fetchall()
            db.execute("""UPDATE jobs SET status='queued', speed=NULL, eta=NULL
                WHERE status IN ('downloading','processing') AND video_json IS NOT NULL""")
        for job_id, provider_name, video_json, quality_json in pending:
            try:
                provider = next(p for p in providers if p.name == provider_name)
                values = json.loads(video_json)
                values["qualities"] = [Quality(**q) for q in values["qualities"]]
                self.executor.submit(self._run, job_id, provider, Video(**values), Quality(**json.loads(quality_json)))
            except (ValueError, TypeError, KeyError, StopIteration):
                self.update(job_id, status="failed", error="Antrean tidak bisa dipulihkan. Periksa ulang link.")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def inspect(self, url: str, owner: str = "") -> dict:
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
            self.inspections[token] = (now, provider, video, owner)
        result = asdict(video)
        result["inspection_id"] = token
        for quality in result["qualities"]:
            quality.pop("format_selector")
        return result

    def create(self, inspection_id: str, quality_id: str, owner: str = "") -> dict:
        with self.lock:
            saved = self.inspections.get(inspection_id)
            if not saved or time.monotonic() - saved[0] >= 1800 or saved[3] != owner:
                raise ProviderError("Pilihan video kedaluwarsa. Periksa ulang link terlebih dahulu.")
            _, provider, video, _ = saved
            quality = next((q for q in video.qualities if q.id == quality_id), None)
            if not quality:
                raise ProviderError("Kualitas ini tidak tersedia. Pilih kualitas dari daftar video.")
            required = self.min_free_bytes + 2 * (quality.estimated_bytes or 0)
            if shutil.disk_usage(self.root).free < required:
                raise ProviderError("Ruang penyimpanan belum cukup untuk kualitas ini. Pilih ukuran lebih kecil atau kosongkan penyimpanan.")
            with self.connect() as db:
                if db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','downloading','processing')").fetchone()[0] >= 20:
                    raise ProviderError("Antrean penuh. Tunggu unduhan sebelumnya selesai.")
                if owner and db.execute("""SELECT COUNT(*) FROM jobs WHERE owner=?
                    AND status IN ('queued','downloading','processing')""", (owner,)).fetchone()[0] >= 3:
                    raise ProviderError("Maksimal tiga unduhan aktif per sesi. Tunggu unduhan sebelumnya selesai.")
                job_id = uuid4().hex
                db.execute("""INSERT INTO jobs
                    (id,title,quality,provider,status,created_at,owner,video_json,quality_json)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                           (job_id, video.title, quality.label, provider.name, "queued", time.time(), owner,
                            json.dumps(asdict(video)), json.dumps(asdict(quality))))
            self.executor.submit(self._run, job_id, provider, video, quality)
        return self.get(job_id, owner)

    def update(self, job_id: str, **changes):
        # All keys are internal, never client supplied. SQLite transactions serialize writers.
        if "progress" in changes:
            value = changes["progress"]
            changes["progress"] = min(100, max(0, value)) if math.isfinite(value) else 0
        with self.connect() as db:
            parts = [f"{key}=MAX(progress, ?)" if key == "progress" else f"{key}=?" for key in changes]
            db.execute(f"UPDATE jobs SET {', '.join(parts)} WHERE id=?", (*changes.values(), job_id))

    def _run(self, job_id, provider, video, quality):
        if self.stopping.is_set():
            return
        directory = self.directory / job_id
        last_write = 0

        def progress(**changes):
            nonlocal last_write
            if self.stopping.is_set():
                raise ProviderError("Unduhan dijeda karena server berhenti.")
            now = time.monotonic()
            if changes.get("status") == "processing" or now - last_write > 0.4:
                if shutil.disk_usage(self.root).free < self.min_free_bytes:
                    raise ProviderError("Penyimpanan hampir penuh. Kosongkan ruang sebelum mengunduh ulang.")
                self.update(job_id, **changes)
                last_write = now
        try:
            directory.mkdir(exist_ok=True)
            self.update(job_id, status="downloading")
            output = provider.download(video, quality, directory, progress).resolve()
            if not output.is_relative_to(directory.resolve()) or not output.is_file():
                raise ProviderError("Lokasi berkas hasil unduhan tidak valid.")
            self.update(job_id, status="completed", progress=100, speed=None, eta=None,
                        downloaded_bytes=output.stat().st_size,
                        filename=str(output.relative_to(self.directory)))
        except Exception as error:
            if self.stopping.is_set():
                self.update(job_id, status="queued", speed=None, eta=None, error=None)
            else:
                logger.exception("Download %s failed", job_id)
                self.update(job_id, status="failed", speed=None, eta=None,
                            error=str(error) if isinstance(error, ProviderError) else "Unduhan gagal. Periksa koneksi dan ruang penyimpanan, lalu coba lagi.")

    def jobs(self, owner: str = "") -> list[dict]:
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM jobs WHERE owner=? ORDER BY created_at DESC LIMIT 100", (owner,)).fetchall()
        return [self.public_job(row) for row in rows]

    @staticmethod
    def public_job(row) -> dict:
        item = dict(row)
        for internal in ("owner", "video_json", "quality_json"):
            item.pop(internal, None)
        item["file_url"] = f"/api/downloads/{item['id']}/file" if item["status"] == "completed" else None
        item["filename"] = Path(item["filename"]).name if item["filename"] else None
        return item

    def get(self, job_id: str, owner: str = "") -> dict | None:
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, owner)).fetchone()
        return self.public_job(row) if row else None

    def file(self, job_id: str, owner: str | None = "") -> Path | None:
        with self.connect() as db:
            row = db.execute("""SELECT filename FROM jobs WHERE id=? AND status='completed'
                AND (? IS NULL OR owner=?)""", (job_id, owner, owner)).fetchone()
        if not row or not row[0]:
            return None
        path = (self.directory / row[0]).resolve()
        return path if path.is_relative_to(self.directory) and path.is_file() else None

    def close(self):
        self.stopping.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
