from src.mutsumi_sync.token_estimate import estimate_tokens, split_text_by_token_budget


def test_estimate_tokens_does_not_treat_cjk_as_quarter_size() -> None:
    text = "长" * 100

    assert estimate_tokens(text) >= 100


def test_split_text_by_token_budget_preserves_source() -> None:
    text = "中文内容" * 20 + " and ascii text"
    chunks = split_text_by_token_budget(text, 10)

    assert "".join(chunks) == text
    assert all(estimate_tokens(chunk) <= 10 for chunk in chunks)
