from src.mutsumi_sync.config import Config
from src.mutsumi_sync.tui.lab import load_prompt_override


def test_prompt_override_falls_back_to_default(tmp_path):
    config = Config()
    prompt = load_prompt_override(config, str(tmp_path / 'missing.yaml'))
    assert prompt
    assert 'Mutsumi' in prompt
    assert 'send' in prompt
    assert '不要只写 content' in prompt
    assert 'content 不会发送给用户' in prompt
