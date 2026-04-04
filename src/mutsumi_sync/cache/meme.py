import json
import hashlib
from pathlib import Path
from typing import Optional


class MemeCache:
    def __init__(self, cache_file: str):
        self.cache_file = Path(cache_file)
        self._cache: dict = {}
        self._load()

    def _load(self):
        if self.cache_file.exists():
            with open(self.cache_file) as f:
                self._cache = json.load(f)

    def _save(self):
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cache_file, "w") as f:
            json.dump(self._cache, f, ensure_ascii=False, indent=2)

    def get(self, md5: str) -> Optional[str]:
        return self._cache.get(md5)

    def set(self, md5: str, description: str):
        self._cache[md5] = description
        self._save()

    def compute_md5(self, image_data: bytes) -> str:
        return hashlib.md5(image_data).hexdigest()
