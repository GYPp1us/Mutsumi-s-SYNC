from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RunReport:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    steps: int = 0
    sends: int = 0
    elapsed_seconds: float = 0.0
    window_items: int = 0
    store_messages: int = 0
    summaries: int = 0
    bridged_contents: int = 0

    def add_usage(self, input_tokens: int, output_tokens: int, cache_hit: int, cache_miss: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_hit_tokens += cache_hit
        self.cache_miss_tokens += cache_miss

    def add_bridged_content(self, content: str) -> None:
        self.bridged_contents += 1

    @property
    def cache_hit_rate(self) -> float:
        total = self.cache_hit_tokens + self.cache_miss_tokens
        if total <= 0:
            return 0.0
        return self.cache_hit_tokens / total
