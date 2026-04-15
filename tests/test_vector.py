import pytest
from src.mutsumi_sync.processor.vector import VectorMatcher


def test_vector_search():
    matcher = VectorMatcher(dimension=3)
    matcher.add("hello world", [1.0, 0.0, 0.0])
    matcher.add("hi there", [0.9, 0.1, 0.0])
    matcher.add("unrelated", [0.0, 0.0, 1.0])
    
    results = matcher.search([0.95, 0.05, 0.0], top_k=2)
    assert len(results) == 2
    assert results[0][0] == "hello world"
    assert results[0][1] > results[1][1]


def test_empty_matcher():
    matcher = VectorMatcher()
    results = matcher.search([1.0, 0.0, 0.0])
    assert results == []
