from src.mutsumi_sync.pipeline import _unsupported_markdown_features


def test_output_gate_accepts_flat_text():
    assert _unsupported_markdown_features("你好，今天过得怎么样？") == []


def test_output_gate_detects_complex_markdown():
    features = _unsupported_markdown_features("# 标题\n\n$$x^2$$\n\n```python\nprint(1)\n```")
    assert "heading" in features
    assert "LaTeX" in features
    assert "code fence" in features


def test_output_gate_detects_tables_and_links():
    features = _unsupported_markdown_features("| a | b |\n|---|---|\n| 1 | 2 |\n\n[文档](https://example.com)")
    assert features == ["table", "link or image"]
