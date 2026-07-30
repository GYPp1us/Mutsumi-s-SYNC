from src.mutsumi_sync.pipeline import _build_default_system_prompt
from src.mutsumi_sync.config import Config


def test_runtime_prompt_contains_current_protocol_boundaries():
    config = Config()
    prompt = _build_default_system_prompt(config)
    assert prompt.startswith(config.prompts.system.runtime.rstrip())
    assert "人格设定：" in prompt
    assert config.prompts.system.persona in prompt
    for phrase in (
        "TO_USER 必须是扁平纯文本",
        "使用 markdown_image",
        "历史 Event",
        "所有真人和服务来源都使用 provider 的 user 角色",
        "不得把一个人的事实移植给另一个人",
        "Media Ledger",
        "[TO_SELF]",
        "[TO_USER]",
        "status_update",
        "Life Stream",
    ):
        assert phrase in prompt


def test_summary_prompts_treat_input_as_documentary_data():
    prompts = Config().prompts.system
    for prompt in (prompts.episode_summary, prompts.message_summary):
        assert "指令" in prompt
        assert "不得虚构" in prompt
