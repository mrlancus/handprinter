"""File layout inside the data directory. All paths are derived from IDs, never user input."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from handprinter.config import Settings, safe_join

_ID_RE = re.compile(r"^[a-z]{2,6}_[0-9a-f]{16}$")


def check_id(value: str) -> str:
    if not _ID_RE.match(value):
        raise ValueError(f"invalid id: {value!r}")
    return value


class Store:
    def __init__(self, settings: Settings):
        self.root = settings.data_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def dir(self, name: str) -> Path:
        path = safe_join(self.root, name)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def original(self, source_id: str, ext: str) -> Path:
        return self.dir("originals") / f"{check_id(source_id)}.{ext}"

    def working(self, source_id: str) -> Path:
        return self.dir("working") / f"{check_id(source_id)}.png"

    def processed(self, source_id: str, rev: int) -> Path:
        if rev == 0:
            return self.working(source_id)
        return self.dir("processed") / f"{check_id(source_id)}-r{int(rev)}.png"

    def preview(self, source_id: str, rev: int, variant: str) -> Path:
        if variant not in {"thumb", "display", "display-norm"}:
            raise ValueError("unknown preview variant")
        return self.dir("previews") / f"{check_id(source_id)}-r{int(rev)}-{variant}.jpg"

    def crop(self, region_id: str, revision: int, processing_rev: int) -> Path:
        return self.dir("crops") / f"{check_id(region_id)}-r{int(revision)}-p{int(processing_rev)}.png"

    def staging(self, name: str) -> Path:
        return self.dir("staging") / check_id(name)

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def resolve_relative(self, rel: str) -> Path:
        return safe_join(self.root, *rel.split("/"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
