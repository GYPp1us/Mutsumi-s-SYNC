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


async def test_describe_image_calls_volcengine_ocr_provider():
    config = Config()
    config.vision.enabled = True
    config.vision.provider = "volcengine-ocr"
    config.vision.access_key_id = "AKID"
    config.vision.secret_access_key = "SECRET"

    captured = {}

    async def fake_post_form(url, *, headers, data, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["data"] = data
        captured["timeout"] = timeout
        return {
            "code": 10000,
            "data": {
                "line_texts": ["第一行", "second line"],
            },
        }

    result = await describe_image(
        image_file=None,
        image_url="https://example.com/a.png",
        config=config,
        post_form=fake_post_form,
    )

    assert result == "OCR text:\n第一行\nsecond line"
    assert captured["url"] == "https://visual.volcengineapi.com/?Action=OCRNormal&Version=2020-08-26"
    assert captured["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert captured["headers"]["Host"] == "visual.volcengineapi.com"
    assert captured["headers"]["Authorization"].startswith("HMAC-SHA256 Credential=AKID/")
    assert "SignedHeaders=content-type;host;x-content-sha256;x-date" in captured["headers"]["Authorization"]
    assert captured["data"] == "image_url=https%3A%2F%2Fexample.com%2Fa.png"


async def test_describe_image_volcengine_ocr_signs_session_token():
    config = Config()
    config.vision.enabled = True
    config.vision.provider = "volcengine-ocr"
    config.vision.access_key_id = "AKID"
    config.vision.secret_access_key = "SECRET"
    config.vision.session_token = "STS-TOKEN"

    captured = {}

    async def fake_post_form(url, *, headers, data, timeout):
        captured["headers"] = headers
        return {"code": 10000, "data": {"line_texts": ["token ok"]}}

    result = await describe_image(
        image_file=None,
        image_url="https://example.com/a.png",
        config=config,
        post_form=fake_post_form,
    )

    assert result == "OCR text:\ntoken ok"
    assert captured["headers"]["X-Security-Token"] == "STS-TOKEN"
    assert "SignedHeaders=content-type;host;x-content-sha256;x-date;x-security-token" in captured["headers"]["Authorization"]


async def test_describe_image_volcengine_ocr_requires_ak_sk():
    config = Config()
    config.vision.enabled = True
    config.vision.provider = "volcengine-ocr"

    result = await describe_image(image_file=None, image_url="https://example.com/a.png", config=config)

    assert result == "[Error: vision.access_key_id and vision.secret_access_key required for volcengine-ocr]"
