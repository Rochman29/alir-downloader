import shutil
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@lru_cache
def ffmpeg_path() -> str:
    installed = shutil.which("ffmpeg")
    if installed:
        return installed
    import imageio_ffmpeg
    # yt-dlp expects an executable named ffmpeg. The wheel ships a versioned name.
    bundled = Path(imageio_ffmpeg.get_ffmpeg_exe())
    target = ROOT / ".tools" / ("ffmpeg.exe" if bundled.suffix == ".exe" else "ffmpeg")
    target.parent.mkdir(exist_ok=True)
    if not target.exists() or target.stat().st_size != bundled.stat().st_size:
        shutil.copy2(bundled, target)
    return str(target)


def js_runtimes() -> dict:
    return {name: {"path": path} for name in ("deno", "node", "bun")
            if (path := shutil.which(name))}
