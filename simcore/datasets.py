from __future__ import annotations

import json
import threading
from pathlib import Path


class DatasetError(Exception):
    pass


class Dataset:
    """A directory of JSON files. Files are loaded on demand and cached until reload()."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._cache: dict[str, object] = {}
        self._lock = threading.Lock()
        if not self.directory.is_dir():
            raise DatasetError(f"dataset directory not found: {self.directory}")

    def load(self, name: str, default=None):
        with self._lock:
            if name in self._cache:
                return self._cache[name]
            p = self.directory / f"{name}.json"
            if not p.exists():
                if default is not None:
                    self._cache[name] = default
                    return default
                raise DatasetError(f"dataset file missing: {p}")
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
            self._cache[name] = data
            return data

    def reload(self):
        with self._lock:
            self._cache.clear()

    def files(self) -> list[str]:
        return sorted(p.name for p in self.directory.glob("*.json"))


class DatasetManager:
    """Holds the active dataset and allows switching between sibling dataset directories."""

    def __init__(self, root: Path, name: str):
        self.root = Path(root)
        self.name = name
        self.dataset = Dataset(self.root / name)

    def available(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith("."))

    def switch(self, name: str):
        ds = Dataset(self.root / name)
        self.name = name
        self.dataset = ds

    def reload(self):
        self.dataset = Dataset(self.root / self.name)
