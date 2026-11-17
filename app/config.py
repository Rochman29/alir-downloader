import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]", "testserver")


def validate_origin(value: str, *, production: bool = False) -> str:
    """Accept only an origin controlled by the operator, never a request URL."""
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("URL backend harus berupa origin HTTP(S), tanpa path atau kredensial.")
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme != "https" and (production or not local):
        raise ValueError("Gunakan HTTPS untuk URL backend publik.")
    # Also reject malformed ports now, rather than during the first request.
    parsed.port
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    mode: str = "local"
    storage: Path = ROOT
    worker_token: str = ""
    public_worker_url: str = ""
    allowed_hosts: tuple[str, ...] = LOCAL_HOSTS
    link_ttl: int = 3600
    min_free_bytes: int = 0

    @classmethod
    def from_env(cls):
        mode = os.getenv("ALIR_MODE", "local")
        if mode not in ("local", "worker"):
            raise ValueError("ALIR_MODE harus local atau worker.")
        hosts = tuple(v.strip() for v in os.getenv("ALIR_ALLOWED_HOSTS", "").split(",") if v.strip())
        token = os.getenv("ALIR_WORKER_TOKEN", "")
        public_url = os.getenv("ALIR_PUBLIC_WORKER_URL", "")
        if mode == "worker":
            if len(token) < 32:
                raise ValueError("ALIR_WORKER_TOKEN minimal 32 karakter diperlukan pada worker.")
            public_url = validate_origin(public_url)
            if not hosts or any("*" in host or "://" in host or "/" in host for host in hosts):
                raise ValueError("Isi ALIR_ALLOWED_HOSTS dengan domain worker yang tepat, tanpa wildcard.")
        ttl = int(os.getenv("ALIR_DOWNLOAD_LINK_TTL_SECONDS", "3600"))
        if not 60 <= ttl <= 86400:
            raise ValueError("Masa berlaku link harus antara 60 dan 86400 detik.")
        min_free = float(os.getenv("ALIR_MIN_FREE_GB", "1" if mode == "worker" else "0"))
        if not 0 <= min_free <= 100000:
            raise ValueError("ALIR_MIN_FREE_GB harus berupa angka positif.")
        return cls(mode, Path(os.getenv("ALIR_STORAGE_DIR", str(ROOT))).resolve(), token,
                   public_url, tuple(dict.fromkeys((*LOCAL_HOSTS, *hosts))), ttl,
                   int(min_free * 1024 ** 3))
