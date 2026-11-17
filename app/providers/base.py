from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


class ProviderError(Exception):
    """An actionable message safe to display to the user."""


@dataclass(frozen=True)
class Quality:
    id: str
    label: str
    height: int
    fps: float
    extension: str
    estimated_bytes: int | None
    format_selector: str


@dataclass(frozen=True)
class Video:
    url: str
    title: str
    uploader: str
    thumbnail: str | None
    duration: float | None
    provider: str
    qualities: list[Quality]


class Provider(Protocol):
    name: str

    def normalize_url(self, url: str) -> str | None: ...

    def inspect(self, url: str) -> Video: ...

    def download(
        self, video: Video, quality: Quality, directory: Path,
        progress: Callable[..., None],
    ) -> Path: ...
