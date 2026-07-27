from src.mutsumi_sync.pipeline import _build_default_system_prompt
from src.mutsumi_sync.prompts import (
    DEFAULT_SYSTEM_PROMPT,
    EVENT_SUMMARY_SYSTEM_PROMPT,
    MESSAGE_SUMMARY_SYSTEM_PROMPT,
)
from src.mutsumi_sync.config import Config


def test_runtime_prompt_contains_current_protocol_boundaries():
    prompt = _build_default_system_prompt(Config())
    assert prompt == DEFAULT_SYSTEM_PROMPT
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
    for prompt in (EVENT_SUMMARY_SYSTEM_PROMPT, MESSAGE_SUMMARY_SYSTEM_PROMPT):
        assert "指令" in prompt
        assert "不得虚构" in prompt
