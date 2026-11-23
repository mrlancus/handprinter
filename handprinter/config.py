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
# Cloudflare's free plan refuses request bodies over 100 MB.
ONLINE_MAX_UPLOAD_BYTES = 95 * 1024 * 1024
# Initial online password, as the owner asked; change it with HANDPRINTER_PASSWORD in .env.
DEFAULT_ONLINE_PASSWORD = "6892"


def _load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():  # -sig: Notepad and PowerShell 5 may add a BOM
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
    online: bool = False
    password: str = ""
    relay_secret: str = ""  # shared with the workers.dev relay (docs/online.md); empty = no relay
    recognition_model: str = "haiku"
    headless_recognition: bool = False
    headless_concurrency: int = 12
    adapter_env: dict[str, str] = field(default_factory=dict)

    @property
    def lan_mode(self) -> bool:
        return self.host not in LOOPBACK_HOSTS

    @property
    def login_required(self) -> bool:
        """Online (Cloudflare Tunnel) or LAN: every visitor signs in. The tunnel arrives from
        loopback, so in online mode loopback is NOT trusted either."""
        return self.online or self.lan_mode

    @property
    def secret(self) -> str:
        """What a visitor types to sign in."""
        return self.password if self.online else self.access_token

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
    online = (merged.get("HANDPRINTER_ONLINE") or "0") == "1"
    return Settings(
        data_dir=data_dir,
        host=merged.get("HANDPRINTER_HOST") or "127.0.0.1",
        port=int(merged.get("HANDPRINTER_PORT") or 8765),
        access_token=merged.get("HANDPRINTER_ACCESS_TOKEN") or "",
        online=online,
        password=merged.get("HANDPRINTER_PASSWORD") or DEFAULT_ONLINE_PASSWORD,
        relay_secret=merged.get("HANDPRINTER_RELAY_SECRET") or "",
        recognition_model=merged.get("HANDPRINTER_RECOGNITION_MODEL") or "haiku",
        # Online, you are usually away from the PC, so recognition can be started from the browser
        # unless explicitly turned off.
        headless_recognition=(merged.get("HANDPRINTER_HEADLESS_RECOGNITION") or ("1" if online else "0")) == "1",
        headless_concurrency=max(1, min(12, int(merged.get("HANDPRINTER_HEADLESS_CONCURRENCY") or 12))),
        adapter_env={"HANDPRINTER_DIFFUSIONPEN_WEIGHTS": str(data_dir / "models" / "diffusionpen"),
                     **{k: v for k, v in merged.items() if k.startswith(("HANDPRINTER_ERUKU_", "HANDPRINTER_DIFFUSIONPEN_")) and v}},
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
