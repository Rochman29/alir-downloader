import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from app.runtime import ffmpeg_path, js_runtimes
from .base import ProviderError, Quality, Video

logger = logging.getLogger(__name__)
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def human_error(error: Exception) -> str:
    message = str(error).lower()
    if any(word in message for word in ("503", "502", "service unavailable")):
        return "Server media YouTube sementara tidak tersedia. Coba unduh lagi nanti atau pilih kualitas lain."
    if any(word in message for word in ("sign in", "confirm you", "bot", "429")):
        return "YouTube membatasi akses dari jaringan ini atau meminta login. Coba lagi nanti atau gunakan jaringan lain."
    if any(word in message for word in ("private", "unavailable", "removed", "not available")):
        return "Video tidak tersedia, bersifat privat, atau kualitasnya sudah berubah. Coba periksa ulang link."
    if any(word in message for word in ("timed out", "resolve", "connection", "network")):
        return "Tidak bisa terhubung ke YouTube. Periksa koneksi internet, lalu coba lagi."
    if "403" in message:
        return "YouTube menolak akses unduhan. Periksa video lagi; jika berulang, perbarui yt-dlp sesuai README."
    if "space" in message:
        return "Ruang penyimpanan tidak cukup untuk menyelesaikan unduhan."
    return "Video gagal diproses. Coba periksa link lagi atau perbarui yt-dlp sesuai README."


def build_qualities(formats: list[dict], duration: float | None) -> list[Quality]:
    usable = [f for f in formats if f.get("url") and not f.get("has_drm")
              and f.get("protocol") in ("https", "http", "http_dash_segments", "m3u8_native", "m3u8")]
    audios = [f for f in usable if f.get("vcodec") == "none"
              and f.get("acodec") not in (None, "none")]
    choices: dict[tuple, tuple] = {}
    for video in usable:
        if video.get("vcodec") in (None, "none") or not video.get("height"):
            continue
        has_audio = video.get("acodec") not in (None, "none")
        audio = None
        if not has_audio:
            if not audios:
                continue  # Never offer a silent video as a complete download.
            audio = max(audios, key=lambda f: (
                video.get("ext") == "mp4" and f.get("ext") == "m4a",
                f.get("language_preference") or -1,
                f.get("protocol") in ("http", "https"),
                "drc" not in str(f["format_id"]), f.get("abr") or f.get("tbr") or 0))
        extension = video.get("ext", "mkv")
        if audio and not (extension == "mp4" and audio.get("ext") == "m4a"):
            extension = "webm" if extension == audio.get("ext") == "webm" else "mkv"
        selector = str(video["format_id"]) + (f"+{audio['format_id']}" if audio else "")
        height, fps = int(video["height"]), float(video.get("fps") or 0)
        size = 0
        for part in (video, audio):
            if part:
                size += part.get("filesize") or part.get("filesize_approx") or (
                    (part.get("tbr") or 0) * 1000 / 8 * (duration or 0))
        label = f"{height}p" + (f" · {fps:g} fps" if fps > 30 else "")
        quality = Quality(str(video["format_id"]), label, height, fps, extension,
                          int(size) or None, selector)
        key = (height, round(fps))
        rank = (extension == "mp4", video.get("protocol") in ("http", "https"),
                video.get("source_preference") or -1,
                video.get("tbr") or 0)
        if key not in choices or rank > choices[key][0]:
            choices[key] = (rank, quality)
    return sorted((item[1] for item in choices.values()),
                  key=lambda q: (q.height, q.fps), reverse=True)


class YouTubeProvider:
    name = "YouTube"

    def normalize_url(self, url: str) -> str | None:
        try:
            parsed = urlparse(url.strip())
            if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
                return None
            if parsed.port not in (None, 80, 443):
                return None
            host = (parsed.hostname or "").lower()
            parts = parsed.path.strip("/").split("/")
            if host == "youtu.be":
                video_id = parts[0]
            elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
                if parsed.path == "/watch":
                    video_id = parse_qs(parsed.query).get("v", [""])[0]
                elif len(parts) == 2 and parts[0] in ("shorts", "embed", "live"):
                    video_id = parts[1]
                else:
                    return None
            else:
                return None
            return f"https://www.youtube.com/watch?v={video_id}" if VIDEO_ID.fullmatch(video_id) else None
        except ValueError:
            return None

    def options(self) -> dict:
        runtimes = js_runtimes()
        if not runtimes:
            raise ProviderError("Pasang Node.js 22+ atau Deno agar YouTube dapat diproses, lalu jalankan ulang aplikasi.")
        return {
            "quiet": True, "no_warnings": True, "logger": logger,
            "noplaylist": True, "socket_timeout": 25, "retries": 2,
            "fragment_retries": 2, "extractor_retries": 2,
            "skip_unavailable_fragments": False,
            "js_runtimes": runtimes, "ffmpeg_location": ffmpeg_path(),
            "cachedir": False, "color": "never",
        }

    def inspect(self, url: str) -> Video:
        try:
            with YoutubeDL(self.options()) as downloader:
                info = downloader.extract_info(url, download=False)
            if not info or info.get("_type") in ("playlist", "multi_video"):
                raise ProviderError("Masukkan link satu video, bukan playlist.")
            if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
                raise ProviderError("Live yang sedang berlangsung atau belum selesai diproses belum didukung.")
            qualities = build_qualities(info.get("formats", []), info.get("duration"))
            if not qualities:
                raise ProviderError("Tidak ditemukan kualitas video dengan audio yang bisa diunduh.")
            return Video(url, info.get("title") or "Video YouTube", info.get("uploader") or "YouTube",
                         f"https://i.ytimg.com/vi/{info['id']}/hqdefault.jpg",
                         info.get("duration"), self.name, qualities)
        except DownloadError as error:
            logger.exception("YouTube inspection failed")
            raise ProviderError(human_error(error)) from error

    def download(self, video: Video, quality: Quality, directory: Path, progress) -> Path:
        transferred: dict[str, int] = {}

        def hook(event):
            if event["status"] in ("downloading", "finished"):
                transferred[event.get("filename", "stream")] = event.get("downloaded_bytes") or 0
                downloaded = sum(transferred.values())
                percent = min(94, downloaded / quality.estimated_bytes * 94) if quality.estimated_bytes else 0
                progress(status="downloading", progress=percent, downloaded_bytes=downloaded,
                         speed=event.get("speed"), eta=event.get("eta"))

        def postprocess(event):
            if event["status"] in ("started", "processing"):
                progress(status="processing", progress=97, speed=None, eta=None)

        options = self.options() | {
            "format": quality.format_selector,
            "outtmpl": str(directory / "%(title).120B [%(id)s].%(ext)s"),
            "windowsfilenames": True, "overwrites": False,
            "merge_output_format": quality.extension,
            "progress_hooks": [hook], "postprocessor_hooks": [postprocess],
        }
        try:
            with YoutubeDL(options) as downloader:
                info = downloader.extract_info(video.url, download=True)
            output = Path(info.get("filepath") or downloader.prepare_filename(info))
            if not output.is_file():
                output = output.with_suffix(f".{quality.extension}")
            if not output.is_file():
                raise ProviderError("Unduhan selesai tetapi berkas hasil tidak ditemukan.")
            return output
        except DownloadError as error:
            logger.exception("YouTube download failed")
            raise ProviderError(human_error(error)) from error
