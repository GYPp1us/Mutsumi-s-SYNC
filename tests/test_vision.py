import pytest

from src.mutsumi_sync.config import Config
from src.mutsumi_sync.vision import describe_image


async def test_describe_image_requires_enabled_config():
    config = Config()

    result = await describe_image(image_file=None, image_url="https://example.com/a.png", config=config)

    assert result.startswith("[Error:")
    assert "disabled" in result


async def test_describe_image_calls_openai_compatible_provider(monkeypatch):
    config = Config()
    config.vision.enabled = True
    config.vision.base_url = "https://vision.example/v1"
    config.vision.api_key = "sk-test"
    config.vision.model = "vision-model"

    captured = {}

    async def fake_post_json(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return {
            "choices": [
                {"message": {"content": "A rendered formula image."}}
            ]
        }

    result = await describe_image(
        image_file=None,
        image_url="https://example.com/a.png",
        config=config,
        post_json=fake_post_json,
    )

    assert result == "A rendered formula image."
    assert captured["url"] == "https://vision.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"]["model"] == "vision-model"
    content = captured["json"]["messages"][0]["content"]
    assert content[1]["image_url"]["url"] == "https://example.com/a.png"
