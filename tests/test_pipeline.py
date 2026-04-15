from src.mutsumi_sync.processor.pipeline import ModelPipeline


def test_pipeline_init():
    pipeline = ModelPipeline(model_name="gpt-4", temperature=0.7)
    assert pipeline.model_name == "gpt-4"
    assert pipeline.temperature == 0.7


def test_pipeline_no_client_without_deps():
    pipeline = ModelPipeline()
    client = pipeline._get_client()
    assert client is None
