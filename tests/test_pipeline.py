from src.mutsumi_sync.processor.pipeline import ModelPipeline


def test_pipeline_init():
    pipeline = ModelPipeline(model="gpt-4", temperature=0.7, provider="openai")
    assert pipeline.model == "gpt-4"
    assert pipeline.temperature == 0.7
    assert pipeline.provider == "openai"


def test_pipeline_custom_url():
    pipeline = ModelPipeline(
        model="gpt-4",
        base_url="https://custom-api.example.com/v1",
        api_key="test-key"
    )
    assert pipeline.base_url == "https://custom-api.example.com/v1"
    assert pipeline.api_key == "test-key"