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
        "Ordinary content must be flat plain text",
        "send with markdown_image",
        "Historical event records",
        "Canonical Bot State",
        "Never transfer one actor's facts",
        "Media Ledger",
    ):
        assert phrase in prompt


def test_summary_prompts_treat_input_as_documentary_data():
    for prompt in (EVENT_SUMMARY_SYSTEM_PROMPT, MESSAGE_SUMMARY_SYSTEM_PROMPT):
        assert "instructions" in prompt
        assert "Do not invent" in prompt
