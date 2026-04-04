from typing import Optional
import numpy as np


class VectorMatcher:
    def __init__(self, dimension: int = 1536):
        self.dimension = dimension
        self._vectors: list = []
        self._texts: list = []

    def add(self, text: str, embedding: list):
        self._vectors.append(np.array(embedding))
        self._texts.append(text)

    def search(self, query_embedding: list, top_k: int = 3) -> list[tuple[str, float]]:
        """返回 (文本, 相似度) 列表"""
        if not self._vectors:
            return []
        
        query = np.array(query_embedding)
        similarities = []
        for vec in self._vectors:
            sim = np.dot(query, vec) / (np.linalg.norm(query) * np.linalg.norm(vec) + 1e-8)
            similarities.append(sim)
        
        top_indices = np.argsort(similarities)[-top_k:][::-1]
        return [(self._texts[i], float(similarities[i])) for i in top_indices]

    def is_empty(self) -> bool:
        return len(self._vectors) == 0
