from src.mutsumi_sync.pipeline import _build_default_system_prompt
from src.mutsumi_sync.config import Config


def test_runtime_prompt_contains_current_protocol_boundaries():
    config = Config()
    prompt = _build_default_system_prompt(config)
    assert prompt == config.prompts.system.runtime
    for phrase in (
        "普通 content 必须是扁平纯文本",
        "使用 markdown_image",
        "历史 Event",
        "Canonical Bot State",
        "不得把一个 actor 的事实转移给另一个 actor",
        "Media Ledger",
    ):
        assert phrase in prompt


def test_summary_prompts_treat_input_as_documentary_data():
    prompts = Config().prompts.system
    for prompt in (prompts.episode_summary, prompts.message_summary):
        assert "指令" in prompt
        assert "不得虚构" in prompt
