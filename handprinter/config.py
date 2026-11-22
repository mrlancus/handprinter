"""Settings from environment variables and an optional `.env` file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# Input bounds (brief §4, input safety).
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_IMAGE_PIXELS = 80_000_000
MAX_PDF_PAGES = 50
MAX_ARCHIVE_ENTRIES = 2_000
MAX_ARCHIVE_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024


def _load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    access_token: str = ""
    recognition_model: str = "haiku"
    headless_recognition: bool = False
    headless_concurrency: int = 2
    adapter_env: dict[str, str] = field(default_factory=dict)

    @property
    def lan_mode(self) -> bool:
        return self.host not in LOOPBACK_HOSTS

    @property
    def db_path(self) -> Path:
        return self.data_dir / "handprinter.sqlite3"

    def sub(self, name: str) -> Path:
        path = self.data_dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path


def load_settings(env: dict[str, str] | None = None, dotenv: Path | None = None) -> Settings:
    merged = _load_dotenv(dotenv or Path.cwd() / ".env")
    merged.update(os.environ if env is None else env)
    data_dir = Path(merged.get("HANDPRINTER_DATA_DIR") or "~/handprinter-data").expanduser().resolve()
    return Settings(
        data_dir=data_dir,
        host=merged.get("HANDPRINTER_HOST") or "127.0.0.1",
        port=int(merged.get("HANDPRINTER_PORT") or 8765),
        access_token=merged.get("HANDPRINTER_ACCESS_TOKEN") or "",
        recognition_model=merged.get("HANDPRINTER_RECOGNITION_MODEL") or "haiku",
        headless_recognition=(merged.get("HANDPRINTER_HEADLESS_RECOGNITION") or "0") == "1",
        headless_concurrency=max(1, min(4, int(merged.get("HANDPRINTER_HEADLESS_CONCURRENCY") or 2))),
        adapter_env={k: v for k, v in merged.items() if k.startswith(("HANDPRINTER_ERUKU_", "HANDPRINTER_DIFFUSIONPEN_"))},
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def safe_join(root: Path, *parts: str) -> Path:
    """Resolve `parts` under `root`, refusing anything that escapes it."""
    root = root.resolve()
    candidate = root.joinpath(*parts).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"path escapes data root: {'/'.join(parts)}")
    return candidate
