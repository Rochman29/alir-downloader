import threading
import time
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.providers.base import ProviderError, Quality, Video
from app.providers.youtube import YouTubeProvider, build_qualities, human_error
from app.runtime import ffmpeg_path
from app.service import DownloadService
from yt_dlp import YoutubeDL
from yt_dlp.postprocessor.ffmpeg import FFmpegMergerPP


class FakeYouTube(YouTubeProvider):
    def __init__(self):
        self.failure = False
        self.gate = threading.Event()
        self.gate.set()

    def inspect(self, url):
        return Video(url, "Video uji <script>", "Kanal uji", None, 10, self.name,
                     [Quality("137", "1080p", 1080, 30, "mp4", 1024, "137+140")])

    def download(self, video, quality, directory, progress):
        self.gate.wait(timeout=5)
        if self.failure:
            raise ProviderError("YouTube menolak akses unduhan.")
        progress(status="downloading", progress=50, downloaded_bytes=512)
        progress(status="downloading", progress=25, downloaded_bytes=600)
        progress(status="processing", progress=97)
        output = directory / "uji.mp4"
        output.write_bytes(b"test-video-content")
        return output


@pytest.fixture
def client(tmp_path):
    provider = FakeYouTube()
    app = create_app(tmp_path, [provider])
    with TestClient(app) as session:
        yield session, app.state.service, provider


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=jNQXAC9IVRw&list=example",
    "https://youtu.be/jNQXAC9IVRw?t=3",
    "https://www.youtube.com/shorts/jNQXAC9IVRw",
    "https://m.youtube.com/watch?v=jNQXAC9IVRw",
])
def test_normalize_video_links(url):
    assert YouTubeProvider().normalize_url(url) == "https://www.youtube.com/watch?v=jNQXAC9IVRw"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/private", "https://youtube.com.attacker.test/watch?v=jNQXAC9IVRw",
    "https://www.youtube.com/playlist?list=test", "file:///etc/passwd",
    "https://youtube.com@attacker.test/watch?v=jNQXAC9IVRw",
    "https://youtube.com:8080/watch?v=jNQXAC9IVRw", "https://youtu.be/invalid",
])
def test_reject_unsupported_and_unsafe_urls(url):
    assert YouTubeProvider().normalize_url(url) is None


def test_qualities_pair_audio_and_do_not_offer_silent_or_drm_video():
    def media(identifier, **values):
        return {"format_id": identifier, "url": "https://media.test/file", "protocol": "https", **values}
    formats = [
        media("137", vcodec="avc1", acodec="none", ext="mp4", height=1080, fps=30, filesize=1000),
        media("248", vcodec="vp9", acodec="none", ext="webm", height=1080, fps=30, filesize=900),
        media("140", vcodec="none", acodec="aac", ext="m4a", abr=128, filesize=200),
        media("18", vcodec="avc1", acodec="aac", ext="mp4", height=360, fps=30, filesize=500),
        media("999", vcodec="avc1", acodec="none", ext="mp4", height=2160, has_drm=True),
        {**media("hls", vcodec="avc1", acodec="none", ext="mp4", height=1080, fps=30, tbr=10000), "protocol": "m3u8_native"},
    ]
    qualities = build_qualities(formats, 10)
    assert [q.height for q in qualities] == [1080, 360]
    assert qualities[0].format_selector == "137+140"
    assert qualities[0].estimated_bytes == 1200
    assert qualities[0].extension == "mp4"
    assert build_qualities(formats[:2], 10) == []


def inspect_video(session):
    response = session.post("/api/inspect", json={"url": "https://youtu.be/jNQXAC9IVRw"})
    assert response.status_code == 200
    return response.json()


def wait_job(session):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = session.get("/api/downloads").json()[0]
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.02)
    pytest.fail("Download worker did not finish")


def test_complete_download_flow_and_persistent_history(client, tmp_path):
    session, service, provider = client
    assert session.get("/").status_code == 200
    assert session.get("/static/app.js").status_code == 200
    video = inspect_video(session)
    assert "format_selector" not in video["qualities"][0]
    response = session.post("/api/downloads", json={"inspection_id": video["inspection_id"], "quality_id": "137"})
    assert response.status_code == 202
    job = wait_job(session)
    assert job["status"] == "completed"
    assert job["progress"] == 100
    file = session.get(job["file_url"])
    assert file.content == b"test-video-content"
    assert "attachment" in file.headers["content-disposition"]
    second = DownloadService(tmp_path, [provider])
    try:
        assert second.jobs()[0]["status"] == "completed"
        assert second.file(job["id"]).read_bytes() == file.content
    finally:
        second.close()


def test_reject_injected_quality_and_expired_inspection(client):
    session, service, _ = client
    video = inspect_video(session)
    payload = {"inspection_id": video["inspection_id"], "quality_id": "bestvideo+bestaudio"}
    assert session.post("/api/downloads", json=payload).status_code == 400
    payload["quality_id"] = "137"
    service.inspections.clear()
    response = session.post("/api/downloads", json=payload)
    assert response.status_code == 400
    assert "kedaluwarsa" in response.json()["detail"]


def test_download_failure_is_visible_and_has_no_file(client):
    session, _, provider = client
    provider.failure = True
    video = inspect_video(session)
    session.post("/api/downloads", json={"inspection_id": video["inspection_id"], "quality_id": "137"})
    job = wait_job(session)
    assert job["status"] == "failed"
    assert "menolak" in job["error"]
    assert job["file_url"] is None
    assert session.get(f"/api/downloads/{job['id']}/file").status_code == 404


def test_cross_origin_posts_and_untrusted_hosts_are_blocked(client):
    session, _, _ = client
    assert session.post("/api/inspect", json={"url": "https://youtu.be/jNQXAC9IVRw"},
                        headers={"Origin": "https://attacker.test"}).status_code == 403
    assert session.post("/api/inspect", content="bad", headers={"Content-Type": "text/plain"}).status_code == 415
    assert session.get("/", headers={"Host": "attacker.test"}).status_code == 400


def test_progress_never_goes_backwards_and_output_path_is_bounded(client, tmp_path):
    session, service, provider = client
    provider.gate.clear()
    video = inspect_video(session)
    response = session.post("/api/downloads", json={"inspection_id": video["inspection_id"], "quality_id": "137"})
    job_id = response.json()["id"]
    service.update(job_id, progress=75)
    service.update(job_id, progress=20)
    assert service.get(job_id)["progress"] == 75
    provider.gate.set()
    wait_job(session)
    outside = tmp_path / "private.txt"
    outside.write_text("secret")
    service.update(job_id, filename="../private.txt")
    assert session.get(f"/api/downloads/{job_id}/file").status_code == 404


def test_restart_marks_interrupted_jobs_as_failed(tmp_path):
    service = DownloadService(tmp_path, [])
    with service.connect() as db:
        db.execute("INSERT INTO jobs (id,status,created_at) VALUES ('unfinished','processing',0)")
    service.close()
    restarted = DownloadService(tmp_path, [])
    try:
        assert restarted.get("unfinished")["status"] == "failed"
        assert "berhenti" in restarted.get("unfinished")["error"]
    finally:
        restarted.close()


def test_media_server_outage_is_not_reported_as_a_private_video():
    assert "sementara" in human_error(Exception("HTTP Error 503: Service Unavailable"))


def test_bundled_ffmpeg_merges_real_video_and_audio(tmp_path):
    ffmpeg = ffmpeg_path()
    video, audio, merged = (tmp_path / name for name in ("video.mp4", "audio.m4a", "merged.mp4"))
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=green:s=160x90:r=15:d=1",
                    "-an", "-c:v", "libx264", str(video)], check=True, capture_output=True, timeout=20)
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-vn", "-c:a", "aac", str(audio)], check=True, capture_output=True, timeout=20)
    with YoutubeDL({"ffmpeg_location": ffmpeg, "quiet": True}) as downloader:
        FFmpegMergerPP(downloader).run({
            "filepath": str(merged), "ext": "mp4",
            "requested_formats": [
                {"filepath": str(video), "vcodec": "h264", "acodec": "none", "protocol": "https"},
                {"filepath": str(audio), "vcodec": "none", "acodec": "aac", "protocol": "https"},
            ],
            "__files_to_merge": [str(video), str(audio)],
        })
    # Explicit stream maps fail if either audio or video is absent; decoding catches corrupt output.
    subprocess.run([ffmpeg, "-v", "error", "-i", str(merged), "-map", "0:v:0", "-map", "0:a:0",
                    "-f", "null", "-"], check=True, capture_output=True, timeout=20)
    assert merged.stat().st_size > 0
