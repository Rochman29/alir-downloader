import hashlib
import hmac
import re
import time
from uuid import uuid4

OWNER_PATTERN = re.compile(r"^[a-f0-9]{32}$")


def signature(secret: str, purpose: str, value: str) -> str:
    return hmac.new(secret.encode(), f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()


def browser_session(cookie: str, secret: str) -> tuple[str, str]:
    owner, _, supplied = cookie.partition(".")
    if not OWNER_PATTERN.fullmatch(owner) or not hmac.compare_digest(
            supplied.encode(), signature(secret, "browser", owner).encode()):
        owner = uuid4().hex
    return owner, f"{owner}.{signature(secret, 'browser', owner)}"


def sign_file(job_id: str, secret: str, ttl: int) -> tuple[int, str]:
    expires = int(time.time()) + ttl
    return expires, signature(secret, "download", f"{job_id}:{expires}")


def valid_file_signature(job_id: str, expires: int, supplied: str, secret: str) -> bool:
    return expires >= time.time() and hmac.compare_digest(
        supplied.encode(), signature(secret, "download", f"{job_id}:{expires}").encode())
