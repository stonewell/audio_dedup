from __future__ import annotations

import json
from pathlib import Path


class FingerprintCache:
    def __init__(self, cache_path: Path) -> None:
        self._path = cache_path
        self._data: dict[str, dict] = {}
        if cache_path.exists():
            try:
                self._data = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def get(self, file_path: str, mtime: float) -> list[int] | None:
        entry = self._data.get(file_path)
        if entry and abs(entry.get("mtime", 0) - mtime) < 1.0:
            return entry.get("fingerprint")
        return None

    def set(self, file_path: str, mtime: float, fingerprint: list[int]) -> None:
        self._data[file_path] = {"mtime": mtime, "fingerprint": fingerprint}

    def save(self) -> None:
        try:
            self._path.write_text(json.dumps(self._data), encoding="utf-8")
        except OSError:
            pass
