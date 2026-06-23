from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass
class SlidingArchive(Generic[T]):
    max_size: float
    min_size: float
    items: list[T] = field(default_factory=list)
    size_of: Callable[[T], float] = field(default=lambda _: 1.0)

    @property
    def total(self) -> float:
        return sum(self.size_of(it) for it in self.items)

    @property
    def needs_recycle(self) -> bool:
        return self.total > self.max_size

    def add(self, item: T) -> None:
        self.items.append(item)

    def extend(self, items: list[T]) -> None:
        self.items.extend(items)

    def find_cutoff(self) -> int:
        t = self.total
        if t <= self.max_size:
            return 0
        acc = 0.0
        for i, item in enumerate(self.items):
            acc += self.size_of(item)
            if (t - acc) <= self.min_size:
                return i + 1
        return max(1, len(self.items) // 2)

    def pop_recyclable(self) -> tuple[list[T], list[T]]:
        if not self.needs_recycle:
            return [], list(self.items)
        idx = self.find_cutoff()
        return self.items[:idx], self.items[idx:]

    def commit(self, kept: list[T]) -> None:
        self.items = kept

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)
