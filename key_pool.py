import itertools
import os
from typing import Iterator

class KeyPool:
    """Manages round-robin rotation across multiple free API keys."""
    def __init__(self, keys_str: str = ""):
        self.keys: list[str] = [k.strip() for k in keys_str.split(",") if k.strip()]
        self._iterator: Iterator[str] | None = itertools.cycle(self.keys) if self.keys else None

    def add_key(self, key: str):
        if key and key not in self.keys:
            self.keys.append(key)
            self._iterator = itertools.cycle(self.keys)

    def get_key(self) -> str | None:
        if not self._iterator:
            return None
        return next(self._iterator)

    @property
    def total_keys(self) -> int:
        return len(self.keys)
